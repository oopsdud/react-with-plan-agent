"""Owner-scoped durable state, immutable graph revisions, and execution-generation fencing."""
import asyncio
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from fastapi.encoders import jsonable_encoder
from agent_system.config.settings import get_settings
from agent_system.db.session import db_session
from agent_system.db.models import (RunCheckpoint,PlanRevision,StepCheckpoint,StepAttempt,AgentRun,Conversation,ConversationMessage)
from agent_system.runtime.errors import ServiceError,public_error
from agent_system.core.planner import Plan


def encoded(value):
    value=jsonable_encoder(value)
    payload=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
    if len(payload.encode())>get_settings().CHECKPOINT_MAX_BYTES:
        raise ServiceError('CHECKPOINT_TOO_LARGE','检查点数据超过大小限制。',422)
    return value


def digest(value):
    return hashlib.sha256(json.dumps(encoded(value),ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def model_identity(settings,model):
    from agent_system.tools.registry import get_tool_registry
    registry=get_tool_registry()
    versions={name:getattr(registry.get(name),'checkpoint_version','1') for name in registry.list_tools()}
    return digest({'checkpoint_schema':1,'tool_versions':versions,'model':model,'base_url':settings.LLM_BASE_URL or 'https://api.openai.com/v1',
        'credential_hash':hashlib.sha256((settings.LLM_API_KEY or '').encode()).hexdigest(),
        'json_mode':settings.LLM_JSON_MODE,'search':settings.SEARCH_PROVIDER,
        'search_config':[settings.SEARXNG_BASE_URL,settings.DDGS_BACKEND,settings.DDGS_REGION,settings.SEARCH_SAFESEARCH],
        'proxy_hash':hashlib.sha256((settings.DDGS_PROXY or '').encode()).hexdigest()})


def validate_graph(plan,allowed_tools=None):
    if len(plan.steps)<1 or len(plan.steps)>get_settings().CHECKPOINT_MAX_STEPS:
        raise ServiceError('INVALID_GRAPH','计划步骤数不合法。',422)
    ids=[s.step_id for s in plan.steps]
    if any(i<1 for i in ids) or len(ids)!=len(set(ids)):
        raise ServiceError('INVALID_GRAPH','步骤 ID 必须为不同的正整数。',422)
    mapping={s.step_id:s for s in plan.steps}
    for step in plan.steps:
        if allowed_tools is not None and step.tool_name not in allowed_tools:
            raise ServiceError('INVALID_GRAPH','计划使用了未注册的工具。',422)
        if len(set(step.depends_on))!=len(step.depends_on) or any(d not in mapping for d in step.depends_on):
            raise ServiceError('INVALID_GRAPH','步骤依赖不存在或重复。',422)
    visiting=set();visited=set()
    def visit(number):
        if number in visiting:raise ServiceError('INVALID_GRAPH','依赖图存在循环。',422)
        if number in visited:return
        visiting.add(number)
        for parent in mapping[number].depends_on:visit(parent)
        visiting.remove(number);visited.add(number)
    for number in ids:visit(number)
    # A parameter reference must be backed by an explicit dependency path.
    ancestor_cache={}
    def ancestors(number):
        if number in ancestor_cache:return ancestor_cache[number]
        result=set()
        for parent in mapping[number].depends_on:result.add(parent);result.update(ancestors(parent))
        ancestor_cache[number]=result
        return result
    for step in plan.steps:
        references={int(i) for i in re.findall(r'\{\{\s*steps\.(\d+)\.result',json.dumps(step.params_template))}
        if not references.issubset(ancestors(step.step_id)):
            raise ServiceError('INVALID_GRAPH','参数引用的上游步骤必须声明为依赖。',422)
    encoded(plan.model_dump())


def descendants(plan,changed):
    result=set(changed)
    while True:
        added={s.step_id for s in plan.steps if any(d in result for d in s.depends_on)}-result
        if not added:return result
        result.update(added)


def _owned_root(db,run_id,user_id):
    root=db.query(RunCheckpoint).filter_by(run_id=run_id,user_id=user_id).first()
    if root is None:raise ServiceError('CHECKPOINT_NOT_FOUND','任务没有可用的持久检查点。',404)
    return root


def create_root(db,run_id,user_id,text,history,model,identity='',mode='chat',initial_plan=None):
    root=RunCheckpoint(run_id=run_id,user_id=user_id,generation=1,head_version=0,status='queued',resume_count=0,
        model_identity=identity,state=encoded({'user_input':text,'history':history,'model':model,'mode':mode,'schema_version':1}),updated_at=time.time())
    db.add(root)
    if initial_plan is not None:
        db.flush()
        _revision(db,root,Plan.model_validate(initial_plan),'initial graph',0)
    return root


def _write_fence(db,run_id,user_id,generation):
    changed=db.query(RunCheckpoint).filter_by(run_id=run_id,user_id=user_id,generation=generation,status='running').update({'updated_at':time.time()},synchronize_session=False)
    if not changed:raise ServiceError('STALE_EXECUTION','执行代次已失效，禁止写入检查点。',409)
    root=_owned_root(db,run_id,user_id)
    if not db.query(Conversation).join(AgentRun,AgentRun.conversation_id==Conversation.id).filter(AgentRun.id==run_id,Conversation.active_run==run_id,Conversation.user_id==user_id).first():
        raise ServiceError('STALE_EXECUTION','会话执行权已失效。',409)
    return root


def _revision(db,root,plan,reason,expected_version):
    validate_graph(plan)
    if root.head_version!=expected_version:raise ServiceError('PLAN_VERSION_CONFLICT','计划版本已更新，请重新读取。',409)
    if root.head_version>=get_settings().CHECKPOINT_MAX_VERSIONS:raise ServiceError('PLAN_BUDGET_EXHAUSTED','计划修订次数已达上限。',409)
    previous=db.get(PlanRevision,(root.run_id,root.head_version)) if root.head_version else None
    old=Plan.model_validate(previous.definition) if previous else None
    if old:plan=plan.model_copy(update={'plan_id':old.plan_id})
    old_steps={s.step_id:s.model_dump() for s in old.steps} if old else {}
    new_steps={s.step_id:s.model_dump() for s in plan.steps}
    changed={i for i in old_steps if old_steps[i]!=new_steps.get(i)}
    if old and old.extracted_slots!=plan.extracted_slots:changed.update(old_steps)
    invalid=descendants(plan,changed|{i for i in new_steps if i not in old_steps})
    if old:invalid.update(descendants(old,changed))
    version=root.head_version+1
    db.add(PlanRevision(run_id=root.run_id,version=version,definition=encoded(plan.model_dump()),reason=reason[:1000],invalidated_steps=sorted(invalid),created_at=time.time()))
    for number in new_steps:
        prior=db.get(StepCheckpoint,(root.run_id,root.head_version,f'step:{number}')) if old else None
        reused=prior is not None and prior.status=='succeeded' and number not in invalid
        db.add(StepCheckpoint(run_id=root.run_id,version=version,step_key=f'step:{number}',status='succeeded' if reused else 'pending',
            input_hash=prior.input_hash if reused else '',result=prior.result if reused else None,error=None,
            reused_from_version=root.head_version if reused else None,updated_at=time.time()))
    stages=db.query(StepCheckpoint).filter_by(run_id=root.run_id,version=0).all()
    for stage in stages:
        if stage.step_key=='stage:answer' or (changed and stage.step_key.startswith('stage:review:')):
            stage.status='stale';stage.result=None
    root.head_version=version;root.updated_at=time.time()
    return version


def revise_plan(run_id,user_id,plan,expected_version,reason,allowed_tools):
    validate_graph(plan,allowed_tools)
    with db_session() as db:
        # Compare-and-swap before reading and copying any result.
        claimed=db.query(RunCheckpoint).filter(RunCheckpoint.run_id==run_id,RunCheckpoint.user_id==user_id,
            RunCheckpoint.head_version==expected_version,RunCheckpoint.status.notin_(['queued','running'])).update({'status':'needs_resume','updated_at':time.time()},synchronize_session=False)
        if not claimed:
            _owned_root(db,run_id,user_id)
            raise ServiceError('PLAN_VERSION_CONFLICT','任务正在运行或计划版本已改变。',409)
        root=_owned_root(db,run_id,user_id)
        if root.state['mode']=='chat':
            ids=sorted(s.step_id for s in plan.steps)
            if ids!=list(range(1,len(ids)+1)) or len(ids)>3:
                raise ServiceError('INVALID_GRAPH','聊天检索计划仅允许连续的 1–3 号步骤。',422)
        return _revision(db,root,plan,reason,expected_version)


def claim_resume(run_id,user_id,expected_generation,identity):
    with db_session() as db:
        claimed=db.query(RunCheckpoint).filter(RunCheckpoint.run_id==run_id,RunCheckpoint.user_id==user_id,
            RunCheckpoint.generation==expected_generation,RunCheckpoint.status.notin_(['queued','running']),
            RunCheckpoint.resume_count<get_settings().CHECKPOINT_MAX_RESUMES).update(
                {'status':'queued','generation':RunCheckpoint.generation+1,'resume_count':RunCheckpoint.resume_count+1,'updated_at':time.time()},synchronize_session=False)
        if not claimed:
            _owned_root(db,run_id,user_id)
            raise ServiceError('RESUME_CONFLICT','任务正在运行、代次已改变或恢复次数已用尽。',409)
        root=_owned_root(db,run_id,user_id)
        if root.state.get('schema_version')!=1:raise ServiceError('CHECKPOINT_SCHEMA_CHANGED','检查点格式不兼容，请创建新任务。',409)
        if root.model_identity!=identity:raise ServiceError('MODEL_CONFIG_CHANGED','模型或检索配置已变化，请恢复原配置或创建新任务。',409)
        job=db.get(AgentRun,run_id)
        latest=db.query(ConversationMessage).filter_by(conversation_id=job.conversation_id,role='assistant').order_by(ConversationMessage.id.desc()).first()
        if latest is None or latest.id!=job.message_id:raise ServiceError('RESUME_NOT_LATEST_TURN','会话已有后续消息，请在新会话中继续。',409)
        occupied=db.query(Conversation).filter_by(id=job.conversation_id,user_id=user_id,active_run=None).update({'active_run':run_id,'updated_at':time.time()},synchronize_session=False)
        if not occupied:raise ServiceError('CONVERSATION_BUSY','会话已有其他任务。',409)
        job.state='queued';job.started_at=None;job.finished_at=None;job.error_code=None
        message=db.get(ConversationMessage,job.message_id)
        message.status='pending'
        return (job.conversation_id,job.message_id,run_id,root.state['history']),root.generation,root.state


def inspect_checkpoint(run_id,user_id,include_results=True):
    with db_session() as db:
        root=_owned_root(db,run_id,user_id)
        revisions=db.query(PlanRevision).filter_by(run_id=run_id).order_by(PlanRevision.version).all()
        nodes=db.query(StepCheckpoint).filter(StepCheckpoint.run_id==run_id,StepCheckpoint.version.in_([0,root.head_version])).all()
        attempts=db.query(StepAttempt).filter_by(run_id=run_id).order_by(StepAttempt.started_at.desc()).limit(100).all()[::-1]
        return {'run_id':run_id,'generation':root.generation,'plan_version':root.head_version,'state':root.status,
            'resume_count':root.resume_count,'mode':root.state['mode'],'model':root.state['model'],
            'plan':revisions[-1].definition if revisions else None,
            'revisions':[{'version':r.version,'reason':r.reason,'invalidated_steps':r.invalidated_steps,'created_at':r.created_at} for r in revisions],
            'steps':[{'version':r.version,'key':r.step_key,'status':r.status,'result':r.result if include_results else None,'error':r.error,'reused_from_version':r.reused_from_version} for r in nodes],
            'attempts':[{k:(getattr(a,k) if include_results or k not in ('params','result') else None) for k in ('id','version','step_key','number','generation','tool_name','replay_safe','status','params','result','error','started_at','finished_at')} for a in attempts]}


@dataclass(frozen=True)
class Checkpoint:
    run_id:str
    user_id:str
    generation:int

    async def io(self,function,*args):
        task=asyncio.create_task(asyncio.to_thread(function,*args))
        try:return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Do not abandon a checkpoint write running in a SQL thread.
            try:await asyncio.shield(task)
            finally:raise

    def _state(self):
        with db_session() as db:
            root=_owned_root(db,self.run_id,self.user_id)
            if root.generation!=self.generation:raise ServiceError('STALE_EXECUTION','执行代次已失效。',409)
            return dict(root.state)

    async def state(self):return await self.io(self._state)

    def _plan(self):
        with db_session() as db:
            root=_owned_root(db,self.run_id,self.user_id)
            revision=db.get(PlanRevision,(self.run_id,root.head_version)) if root.head_version else None
            return (Plan.model_validate(revision.definition),root.head_version) if revision else (None,0)

    async def plan(self):return await self.io(self._plan)

    def _ensure(self,plan,reason):
        with db_session() as db:
            root=_write_fence(db,self.run_id,self.user_id,self.generation)
            existing=db.get(PlanRevision,(self.run_id,root.head_version)) if root.head_version else None
            if existing:
                original=Plan.model_validate(existing.definition)
                plan=plan.model_copy(update={'plan_id':original.plan_id})
                if digest(original.model_dump())==digest(plan.model_dump()):return root.head_version
            return _revision(db,root,plan,reason,root.head_version)

    async def ensure_plan(self,plan,reason='execution plan'):return await self.io(self._ensure,plan,reason)

    def _nodes(self,version):
        with db_session() as db:
            _owned_root(db,self.run_id,self.user_id)
            return {r.step_key:r.result for r in db.query(StepCheckpoint).filter_by(run_id=self.run_id,version=version,status='succeeded').all()}

    async def nodes(self,version):return await self.io(self._nodes,version)

    def _cached(self,version,key,input_hash):
        with db_session() as db:
            root=_owned_root(db,self.run_id,self.user_id)
            if root.generation!=self.generation:raise ServiceError('STALE_EXECUTION','执行代次已失效。',409)
            row=db.get(StepCheckpoint,(self.run_id,version,key))
            return (True,row.result) if row and row.status=='succeeded' and row.input_hash==input_hash else (False,None)

    def _begin(self,version,key,tool_name,params,input_hash,replay_safe):
        with db_session() as db:
            root=_write_fence(db,self.run_id,self.user_id,self.generation)
            previous=db.query(StepAttempt).filter_by(run_id=self.run_id,step_key=key).order_by(StepAttempt.number.desc()).first()
            if previous and previous.status in ('running','unknown','cancelled') and not previous.replay_safe:
                raise ServiceError('STEP_OUTCOME_UNKNOWN','上次调用结果未知，禁止自动重放有副作用的工具。',409)
            count=previous.number if previous else 0
            if count>=get_settings().CHECKPOINT_MAX_STEP_ATTEMPTS:raise ServiceError('STEP_BUDGET_EXHAUSTED','该步骤尝试预算已用尽。',409)
            if root.state['mode']=='chat' and key.startswith('step:'):
                used=db.query(StepAttempt).filter(StepAttempt.run_id==self.run_id,StepAttempt.step_key.like('step:%')).count()
                if used>=3:raise ServiceError('SEARCH_BUDGET_EXHAUSTED','检索总预算已用尽，恢复不会重置三次调用上限。',409)
            row=db.get(StepCheckpoint,(self.run_id,version,key))
            if not row:
                row=StepCheckpoint(run_id=self.run_id,version=version,step_key=key,status='pending',input_hash='',updated_at=time.time());db.add(row)
            row.status='running';row.input_hash=input_hash;row.error=None;row.result=None;row.updated_at=time.time()
            attempt=StepAttempt(id=uuid.uuid4().hex,run_id=self.run_id,version=version,step_key=key,number=count+1,generation=self.generation,
                tool_name=tool_name,replay_safe=replay_safe,status='running',params=encoded(params),started_at=time.time())
            db.add(attempt);db.flush()
            return attempt.id

    async def begin(self,version,key,tool_name,params,input_hash,replay_safe):
        return await self.io(self._begin,version,key,tool_name,params,input_hash,replay_safe)

    def _parameters(self,attempt_id,params):
        with db_session() as db:
            _write_fence(db,self.run_id,self.user_id,self.generation)
            attempt=db.query(StepAttempt).filter_by(id=attempt_id,run_id=self.run_id,generation=self.generation,status='running').first()
            if attempt is None:raise ServiceError('STALE_EXECUTION','步骤尝试已失效。',409)
            attempt.params=encoded(params)

    async def parameters(self,attempt_id,params):return await self.io(self._parameters,attempt_id,params)

    def _end(self,attempt_id,status,result,error,params):
        with db_session() as db:
            _write_fence(db,self.run_id,self.user_id,self.generation)
            attempt=db.query(StepAttempt).filter_by(id=attempt_id,run_id=self.run_id,generation=self.generation).first()
            if attempt is None:raise ServiceError('STALE_EXECUTION','步骤尝试已失效。',409)
            # A cancellation can arrive after a SQL thread committed success but
            # before its awaiter resumed. Never overwrite that durable terminal result.
            if attempt.status!='running':return False
            attempt.status=status;attempt.result=encoded(result);attempt.error=encoded(error);attempt.finished_at=time.time()
            if params is not None:attempt.params=encoded(params)
            row=db.get(StepCheckpoint,(self.run_id,attempt.version,attempt.step_key))
            row.status=status;row.result=attempt.result;row.error=attempt.error;row.updated_at=time.time()
            return True

    async def end(self,attempt_id,status,result=None,error=None,params=None):return await self.io(self._end,attempt_id,status,result,error,params)

    def _skip(self,version,key,result):
        with db_session() as db:
            _write_fence(db,self.run_id,self.user_id,self.generation)
            row=db.get(StepCheckpoint,(self.run_id,version,key))
            if row is None:
                row=StepCheckpoint(run_id=self.run_id,version=version,step_key=key,input_hash='',updated_at=time.time());db.add(row)
            row.status='skipped';row.result=encoded(result);row.updated_at=time.time()

    async def skip(self,version,key,result):return await self.io(self._skip,version,key,result)

    async def stage(self,key,params,operation):
        key='stage:'+key
        value_hash=digest(params)
        hit,value=await self.io(self._cached,0,key,value_hash)
        if hit:return value
        attempt=await self.begin(0,key,'internal:'+key,params,value_hash,True)
        try:
            value=await operation()
            await self.end(attempt,'succeeded',value)
            return value
        except asyncio.CancelledError:
            await self.end(attempt,'cancelled',error={'code':'INTERRUPTED','message':'步骤执行被中断'})
            raise
        except Exception as error:
            await self.end(attempt,'failed',error=public_error(error))
            raise

def execution_progress(run_id,user_id):
    with db_session() as db:
        root=_owned_root(db,run_id,user_id)
        nodes=db.query(StepCheckpoint).filter_by(run_id=run_id,version=root.head_version).all()
        return {'plan_version':root.head_version,'steps':[r.result for r in nodes if r.step_key.startswith('step:') and r.result]}
