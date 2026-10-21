"""Conversation storage. Every query is scoped to the authenticated owner."""
import time
import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from agent_system.db.session import get_db, db_session
from agent_system.db.models import Conversation, ConversationMessage
from agent_system.api.auth import require_user

router=APIRouter(prefix='/conversations',tags=['Conversations'])

def owned(db,conversation_id,user_id):
    row=db.query(Conversation).filter_by(id=conversation_id,user_id=user_id).first()
    if not row:raise HTTPException(404,'会话不存在。')
    return row

def summary(row):return {'id':row.id,'title':row.title,'model':row.model,'updated_at':row.updated_at,'running':row.active_run is not None}

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
def read(conversation_id:str,user=Depends(require_user),db=Depends(get_db)):
    row=owned(db,conversation_id,user.id)
    messages=db.query(ConversationMessage).filter_by(conversation_id=row.id).order_by(ConversationMessage.id).all()
    return {**summary(row),'messages':[message_view(m) for m in messages]}

@router.delete('/{conversation_id}')
def delete(conversation_id:str,user=Depends(require_user),db=Depends(get_db)):
    row=owned(db,conversation_id,user.id)
    if row.active_run:raise HTTPException(409,'请等待当前任务完成后再删除。')
    db.query(ConversationMessage).filter_by(conversation_id=row.id).delete()
    db.delete(row);db.commit()
    return {'success':True}


def reserve_turn(user_id,conversation_id,text,model):
    """Commit both the input and placeholder before starting any external call."""
    with db_session() as db:
        if conversation_id:
            row=owned(db,conversation_id,user_id)
        else:
            row=Conversation(user_id=user_id,title=text[:50],updated_at=time.time())
            db.add(row);db.flush()
        run_id=uuid.uuid4().hex
        updated=db.query(Conversation).filter_by(id=row.id,user_id=user_id,active_run=None).update({'active_run':run_id,'model':model,'updated_at':time.time()},synchronize_session=False)
        if not updated:raise HTTPException(409,'该会话有任务正在执行，请稍后重试。')
        prior=db.query(ConversationMessage).filter_by(conversation_id=row.id,status='completed').order_by(ConversationMessage.id.desc()).limit(50).all()
        history=[{'role':m.role,'content':m.content} for m in reversed(prior) if m.content]
        if row.title=='新会话':row.title=text[:50]
        user_message=ConversationMessage(conversation_id=row.id,role='user',content=text,model=model,created_at=time.time())
        assistant=ConversationMessage(conversation_id=row.id,role='assistant',content='',model=model,status='pending',created_at=time.time())
        db.add_all([user_message,assistant]);db.flush()
        return row.id,assistant.id,run_id,history


def finish_turn(conversation_id,message_id,run_id,result):
    with db_session() as db:
        message=db.get(ConversationMessage,message_id)
        if message is not None:
            message.content=result.get('answer') or result.get('error') or '任务未完成。'
            message.result=result
            message.status='completed' if result.get('success') else 'failed'
            if not result.get('success'):
                previous=db.query(ConversationMessage).filter(ConversationMessage.conversation_id==conversation_id,ConversationMessage.id<message_id,ConversationMessage.role=='user').order_by(ConversationMessage.id.desc()).first()
                if previous:previous.status='failed'
        db.query(Conversation).filter_by(id=conversation_id,active_run=run_id).update({'active_run':None,'updated_at':time.time()})


def recover_interrupted(db):
    # Single-process local deployment: runs cannot survive backend restart.
    for pending in db.query(ConversationMessage).filter_by(status='pending').all():
        previous=db.query(ConversationMessage).filter(ConversationMessage.conversation_id==pending.conversation_id,ConversationMessage.id<pending.id,ConversationMessage.role=='user').order_by(ConversationMessage.id.desc()).first()
        if previous:previous.status='interrupted'
    db.query(ConversationMessage).filter_by(status='pending').update({'status':'interrupted','content':'后端重启，任务已中断。请重新发送。'})
    db.query(Conversation).filter(Conversation.active_run.is_not(None)).update({'active_run':None})
    db.commit()
