"""Conversation storage. Every query is scoped to the authenticated owner."""
import time
import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from agent_system.db.session import get_db, db_session
from agent_system.db.models import Conversation, ConversationMessage, AgentRun, RequestDedup, RunCheckpoint, PlanRevision, StepCheckpoint, StepAttempt
from agent_system.api.auth import require_user
from agent_system.runtime.errors import ServiceError
from agent_system.config.settings import get_settings

router=APIRouter(prefix='/conversations',tags=['Conversations'])

def owned(db,conversation_id,user_id):
    row=db.query(Conversation).filter_by(id=conversation_id,user_id=user_id).first()
    if not row:raise HTTPException(404,'会话不存在。')
    return row

def bounded_history(messages):
    remaining=get_settings().CONTEXT_MAX_CHARS
    result=[]
    for message in reversed(messages[-50:]):
        if remaining<=0:break
        # Keep each recent message bounded so one large answer cannot exhaust context.
        content=message['content']
        limit=min(12000,remaining)
        if len(content)>limit:
            marker='[内容已截断]'
            content=(content[:max(0,limit-len(marker))]+marker)[:limit]
        remaining-=len(content)
        result.append({'role':message['role'],'content':content})
    return list(reversed(result))


def summary(row):return {'id':row.id,'title':row.title,'model':row.model,'updated_at':row.updated_at,'running':row.active_run is not None,'run_id':row.active_run}

def message_view(row):return {'id':row.id,'role':row.role,'content':row.content,'model':row.model,'status':row.status,'result':row.result,'created_at':row.created_at}

class ConversationCreate(BaseModel):
    title:str=Field(default='新会话',min_length=1,max_length=200)

@router.get('')
def list_conversations(user=Depends(require_user),db=Depends(get_db)):
    return {'conversations':[summary(c) for c in db.query(Conversation).filter_by(user_id=user.id).order_by(Conversation.updated_at.desc()).limit(200)]}

@router.post('')
def create(body:ConversationCreate,user=Depends(require_user),db=Depends(get_db)):
    row=Conversation(user_id=user.id,title=body.title,updated_at=time.time())
    db.add(row);db.commit();db.refresh(row)
    return summary(row)

@router.get('/{conversation_id}')
def read(conversation_id:str,before_id:Optional[int]=Query(default=None,ge=1),limit:int=Query(default=100,ge=1,le=200),user=Depends(require_user),db=Depends(get_db)):
    row=owned(db,conversation_id,user.id)
    query=db.query(ConversationMessage).filter_by(conversation_id=row.id)
    if before_id is not None:query=query.filter(ConversationMessage.id<before_id)
    rows=query.order_by(ConversationMessage.id.desc()).limit(limit+1).all()
    more=len(rows)>limit
    messages=list(reversed(rows[:limit]))
    ids=[m.id for m in messages]
    jobs={job.message_id:(job,root) for job,root in db.query(AgentRun,RunCheckpoint).join(RunCheckpoint,RunCheckpoint.run_id==AgentRun.id).filter(AgentRun.message_id.in_(ids)).all()} if ids else {}
    views=[]
    for message in messages:
        value=message_view(message)
        if message.id in jobs:
            job,root=jobs[message.id]
            value.update(run_id=job.id,run_state=root.status if root.status=='needs_resume' else job.state,checkpoint_available=True,checkpoint_generation=root.generation,plan_version=root.head_version)
        views.append(value)
    return {**summary(row),'messages':views,'has_more':more,'next_before_id':messages[0].id if more and messages else None}

@router.delete('/{conversation_id}')
def delete(conversation_id:str,user=Depends(require_user),db=Depends(get_db)):
    row=owned(db,conversation_id,user.id)
    if row.active_run:raise HTTPException(409,'请等待当前任务完成后再删除。')
    claimed=db.query(Conversation).filter_by(id=row.id,user_id=user.id,active_run=None).update({'active_run':'deleting'},synchronize_session=False)
    if not claimed:raise HTTPException(409,'会话正在运行，不能删除。')
    run_ids=db.query(AgentRun.id).filter_by(conversation_id=row.id)
    db.query(RequestDedup).filter(RequestDedup.run_id.in_(run_ids)).delete(synchronize_session=False)
    for table in (StepAttempt,StepCheckpoint,PlanRevision,RunCheckpoint):
        db.query(table).filter(table.run_id.in_(run_ids)).delete(synchronize_session=False)
    db.query(AgentRun).filter_by(conversation_id=row.id).delete()
    db.query(ConversationMessage).filter_by(conversation_id=row.id).delete()
    db.delete(row);db.commit()
    return {'success':True}


def _reserve_turn(user_id,conversation_id,text,model,client_request_id=None,fingerprint=None,checkpoint_spec=None):
    """Commit both the input and placeholder before starting any external call."""
    with db_session() as db:
        if client_request_id:
            prior_request=db.get(RequestDedup,(user_id,client_request_id))
            if prior_request:
                job=db.get(AgentRun,prior_request.run_id)
                if prior_request.expires_at<=time.time() and job and job.state not in ('queued','running'):
                    db.delete(prior_request);db.flush()
                else:
                    conflict=prior_request.fingerprint!=fingerprint
                    raise ServiceError('IDEMPOTENCY_CONFLICT' if conflict else 'DUPLICATE_REQUEST',
                        '请求编号已用于不同内容。' if conflict else '该请求已受理，请读取已有任务状态。',409,
                        details={'run_id':prior_request.run_id})
        if conversation_id:
            row=owned(db,conversation_id,user_id)
        else:
            row=Conversation(user_id=user_id,title=text[:50],updated_at=time.time())
            db.add(row);db.flush()
        run_id=uuid.uuid4().hex
        updated=db.query(Conversation).filter_by(id=row.id,user_id=user_id,active_run=None).update({'active_run':run_id,'model':model,'updated_at':time.time()},synchronize_session=False)
        if not updated:raise HTTPException(409,'该会话有任务正在执行，请稍后重试。')
        prior=db.query(ConversationMessage).filter_by(conversation_id=row.id,status='completed').order_by(ConversationMessage.id.desc()).limit(50).all()
        history=bounded_history([{'role':m.role,'content':m.content} for m in reversed(prior) if m.content])
        if row.title=='新会话':row.title=text[:50]
        user_message=ConversationMessage(conversation_id=row.id,role='user',content=text,model=model,created_at=time.time())
        assistant=ConversationMessage(conversation_id=row.id,role='assistant',content='',model=model,status='pending',created_at=time.time())
        db.add_all([user_message,assistant]);db.flush()
        db.add(AgentRun(id=run_id,user_id=user_id,conversation_id=row.id,message_id=assistant.id,state='queued',created_at=time.time()))
        db.flush()
        from agent_system.runtime.checkpoints import create_root
        create_root(db,run_id,user_id,text,history,model,**(checkpoint_spec or {}))
        if client_request_id:
            db.add(RequestDedup(user_id=user_id,request_id=client_request_id,fingerprint=fingerprint,run_id=run_id,expires_at=time.time()+86400))
        return row.id,assistant.id,run_id,history


def finish_turn(conversation_id,message_id,run_id,result,generation=1):
    with db_session() as db:
        root=db.get(RunCheckpoint,run_id)
        if root is not None:
            fenced=db.query(RunCheckpoint).filter(RunCheckpoint.run_id==run_id,RunCheckpoint.generation==generation,RunCheckpoint.status.in_(['queued','running'])).update({'status':result.get('run_state') or ('succeeded' if result.get('success') else 'failed'),'updated_at':time.time()},synchronize_session=False)
            if not fenced:return False
        # Acquire the write/fencing condition before touching any message. A stale
        # worker must not overwrite a newer run or another conversation.
        claimed=db.query(Conversation).filter_by(id=conversation_id,active_run=run_id).update({'active_run':None,'updated_at':time.time()},synchronize_session=False)
        if not claimed:
            db.rollback();return False
        message=db.query(ConversationMessage).filter_by(id=message_id,conversation_id=conversation_id).first()
        if message is None:raise RuntimeError('Run message does not belong to conversation')
        if message is not None:
            message.content=result.get('answer') or result.get('error') or '任务未完成。'
            message.result=result
            message.status='completed' if result.get('success') else 'failed'
            previous=db.query(ConversationMessage).filter(ConversationMessage.conversation_id==conversation_id,ConversationMessage.id<message_id,ConversationMessage.role=='user').order_by(ConversationMessage.id.desc()).first()
            if previous:previous.status='completed' if result.get('success') else 'failed'
        db.query(StepAttempt).filter_by(run_id=run_id,generation=generation,status='running').update({'status':'unknown','finished_at':time.time()})
        db.query(StepCheckpoint).filter_by(run_id=run_id,status='running').update({'status':'unknown','updated_at':time.time()})
        job=db.get(AgentRun,run_id)
        if job:
            job.state=result.get('run_state') or ('succeeded' if result.get('success') else 'failed')
            job.error_code=(result.get('error_info') or {}).get('code')
            job.finished_at=time.time()
        return True


def recover_interrupted(db):
    # Single-process local deployment: runs cannot survive backend restart.
    for pending in db.query(ConversationMessage).filter_by(status='pending').all():
        previous=db.query(ConversationMessage).filter(ConversationMessage.conversation_id==pending.conversation_id,ConversationMessage.id<pending.id,ConversationMessage.role=='user').order_by(ConversationMessage.id.desc()).first()
        if previous:previous.status='interrupted'
    db.query(ConversationMessage).filter_by(status='pending').update({'status':'interrupted','content':'后端重启，任务已中断。请重新发送。'})
    db.query(StepAttempt).filter_by(status='running').update({'status':'unknown','finished_at':time.time()})
    db.query(StepCheckpoint).filter_by(status='running').update({'status':'unknown','updated_at':time.time()})
    db.query(RunCheckpoint).filter(RunCheckpoint.status.in_(['queued','running'])).update({'status':'interrupted','generation':RunCheckpoint.generation+1,'updated_at':time.time()})
    db.query(AgentRun).filter(AgentRun.state.in_(['queued','running'])).update({'state':'interrupted','finished_at':time.time(),'error_code':'PROCESS_RESTARTED'})
    db.query(Conversation).filter(Conversation.active_run.is_not(None)).update({'active_run':None})
    db.commit()


def mark_running(run_id,generation=1):
    with db_session() as db:
        root=db.get(RunCheckpoint,run_id)
        if root is not None:
            claimed=db.query(RunCheckpoint).filter_by(run_id=run_id,generation=generation,status='queued').update({'status':'running','updated_at':time.time()},synchronize_session=False)
            if not claimed:return 0
        changed=db.query(AgentRun).filter_by(id=run_id,state='queued').update({'state':'running','started_at':time.time()})
        if not changed:db.rollback()
        return changed


def run_view(run):
    return {key:getattr(run,key) for key in ('id','conversation_id','message_id','state','created_at','started_at','finished_at','error_code')}


def reserve_turn(user_id,conversation_id,text,model,client_request_id=None,fingerprint=None,checkpoint_spec=None):
    from sqlalchemy.exc import IntegrityError
    try:
        return _reserve_turn(user_id,conversation_id,text,model,client_request_id,fingerprint,checkpoint_spec)
    except IntegrityError:
        if client_request_id:
            with db_session() as db:
                existing=db.get(RequestDedup,(user_id,client_request_id))
                if existing:
                    conflict=existing.fingerprint!=fingerprint
                    raise ServiceError('IDEMPOTENCY_CONFLICT' if conflict else 'DUPLICATE_REQUEST',
                        '请求编号已用于不同内容。' if conflict else '该请求已受理，请读取已有任务状态。',409,
                        details={'run_id':existing.run_id}) from None
        raise ServiceError('DATABASE_CONFLICT','数据库写入冲突，请稍后重试。',503,True,2) from None
