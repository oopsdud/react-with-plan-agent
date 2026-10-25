"""Authenticated API with bounded scheduling, lifecycle persistence and sanitized errors."""
import asyncio
import json
import logging
import hashlib
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeout
from pydantic import BaseModel, Field, ConfigDict, field_validator
from agent_system.config.settings import get_settings, credential_configured
from agent_system.db.session import init_db, db_session, get_db
from agent_system.db.models import AgentRun,ConversationMessage
from agent_system.core.agent import run_agent
from agent_system.core.intent_router import IntentRouter
from agent_system.utils.llm_client import OpenAIClient
from agent_system.tools.registry import get_tool_registry
from agent_system.tools.web_search import search_configured
from agent_system.api.auth import router as auth_router, require_user, require_admin, seed_users
from agent_system.api.conversations import router as conversations_router, reserve_turn, finish_turn, owned, recover_interrupted, mark_running, run_view, bounded_history
from agent_system.api.model_settings import router as model_router, available_models
from agent_system.runtime import get_runtime
from agent_system.runtime.errors import ServiceError, public_error
from agent_system.runtime.process_lease import ProcessLease

logger=logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app):
    settings=get_settings()
    from sqlalchemy.engine import make_url
    url=make_url(settings.DATABASE_URL)
    directory=Path(url.database).resolve().parent if url.get_backend_name()=='sqlite' and url.database not in (None,':memory:') else Path(__file__).resolve().parents[2]/'data'
    lease=ProcessLease(directory/'agent-runtime.lock').acquire()
    runtime=get_runtime()
    try:
        def initialize():
            init_db()
            with db_session() as db:
                seed_users(db)
                recover_interrupted(db)
        await asyncio.to_thread(initialize)
        yield
    finally:
        try:await runtime.scheduler.close()
        finally:
            runtime.search_threads.close()
            lease.release()

app=FastAPI(title='Agent System',description='ReAct-with-Plan 智能体框架',version='0.4.0',lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=get_settings().CORS_ORIGINS,allow_credentials=True,
    allow_methods=['GET','POST','PUT','DELETE'],allow_headers=['Content-Type'],expose_headers=['X-Request-ID','Retry-After'])
app.include_router(auth_router)
app.include_router(conversations_router)
app.include_router(model_router)

@app.middleware('http')
async def request_metadata(request,call_next):
    request.state.request_id=uuid.uuid4().hex
    state=get_runtime()
    if request.url.path=='/health':
        response=await call_next(request)
    elif state.http_requests>=state.scheduler.settings.HTTP_MAX_CONCURRENT:
        response=error_response(request,ServiceError('HTTP_CAPACITY_EXCEEDED','服务请求过多，请稍后重试。',429,True,2))
    else:
        state.http_requests+=1
        try:response=await call_next(request)
        finally:state.http_requests-=1
    response.headers['X-Request-ID']=request.state.request_id
    if request.url.path.startswith(('/auth','/conversations','/runs','/chat','/config','/models','/runtime','/operations')):
        response.headers['Cache-Control']='no-store'
    return response


def error_response(request,error):
    info=error.public()
    headers={'X-Request-ID':getattr(request.state,'request_id',''), 'Cache-Control':'no-store'}
    if error.retry_after:headers['Retry-After']=str(error.retry_after)
    return JSONResponse(status_code=error.status,content={'detail':error.message,'error':info,
        'request_id':getattr(request.state,'request_id',None)},headers=headers)

@app.exception_handler(ServiceError)
async def service_failure(request,error):return error_response(request,error)

@app.exception_handler(HTTPException)
async def http_failure(request,error):
    codes={401:'AUTHENTICATION_REQUIRED',403:'FORBIDDEN',404:'NOT_FOUND',409:'CONVERSATION_BUSY',422:'INVALID_REQUEST',502:'UPSTREAM_ERROR'}
    value=ServiceError(codes.get(error.status_code,'HTTP_ERROR'),error.detail if isinstance(error.detail,str) else '请求失败。',error.status_code,error.status_code in (429,502,503,504))
    return error_response(request,value)

@app.exception_handler(RequestValidationError)
async def invalid_request(request,error):
    # ValidationError.input can contain passwords and entire conversation bodies.
    return error_response(request,ServiceError('INVALID_REQUEST','请求格式或参数不合法，请检查必填字段及长度。',422))

@app.exception_handler(PoolTimeout)
@app.exception_handler(OperationalError)
async def database_failure(request,error):
    return error_response(request,ServiceError('DATABASE_BUSY','数据库暂时不可用，请稍后重试。',503,True,2))

@app.exception_handler(Exception)
async def unexpected_failure(request,error):
    logger.error('request_failure request=%s type=%s',getattr(request.state,'request_id',None),type(error).__name__)
    return error_response(request,ServiceError('INTERNAL_ERROR','服务内部异常，请凭请求编号联系管理员。',500))

class ChatRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    user_input:str=Field(min_length=1,max_length=12000)
    conversation_id:Optional[str]=Field(default=None,max_length=64)
    client_request_id:Optional[str]=Field(default=None,min_length=8,max_length=128)
    model:Optional[str]=Field(default=None,min_length=1,max_length=500)
    @field_validator('user_input')
    @classmethod
    def nonblank(cls,value):
        if not value.strip():raise ValueError('must not be blank')
        return value

class ChatResponse(BaseModel):
    conversation_id:str
    message_id:int
    trace_id:str
    success:bool
    answer:Optional[str]
    intent:Optional[dict[str,Any]]
    steps:list[dict[str,Any]]
    latency_ms:int
    error:Optional[str]
    sources:list[dict[str,Any]]=Field(default_factory=list)
    rounds:list[dict[str,Any]]=Field(default_factory=list)
    evidence_sufficient:Optional[bool]=None
    model:Optional[str]=None
    run_id:Optional[str]=None
    run_state:Optional[str]=None
    error_info:Optional[dict[str,Any]]=None
    degraded:bool=False
    queue_wait_ms:int=0

@app.get('/')
def root():return {'name':'Agent System','version':'0.4.0','status':'running'}

@app.get('/health')
async def health():return {'status':'healthy'}

@app.get('/ready')
async def ready():
    if get_runtime().scheduler.closing:raise ServiceError('SERVICE_DRAINING','服务正在排空任务。',503,True,5)
    def ping():
        from sqlalchemy import text
        with db_session() as db:db.execute(text('SELECT 1'))
    await asyncio.to_thread(ping)
    return {'ready':True,'deployment':'single-process'}

@app.get('/runtime')
def runtime(user=Depends(require_user)):
    settings=get_settings()
    return {'version':'0.4.0','llm_mode':'api','llm_model':settings.LLM_MODEL,'llm_configured':credential_configured(settings.LLM_API_KEY) and credential_configured(settings.LLM_MODEL),
        'search_provider':settings.SEARCH_PROVIDER,'search_configured':search_configured(settings),'tools_mode':'live','streaming':True,'max_search_rounds':3}

@app.get('/operations')
async def operations(user=Depends(require_admin)):
    state=get_runtime()
    return {'scheduler':state.scheduler.snapshot(),'dependencies':state.guards.snapshot(),'http_inflight':state.http_requests,'persistence_failures':state.persistence_failures,'deployment':'single-process'}

@app.get('/models')
async def models(user=Depends(require_user)):
    return {'models':await available_models(),'default_model':get_settings().LLM_MODEL}

@app.get('/tools')
def tools(user=Depends(require_user)):
    names=get_tool_registry().list_tools()
    return {'tools':names,'count':len(names)}

@app.get('/tools/{tool_name}')
def tool_info(tool_name:str,user=Depends(require_user)):
    tool=get_tool_registry().get(tool_name)
    if tool is None:raise HTTPException(404,'Tool not found')
    return {'name':tool.name,'description':tool.description,'timeout_seconds':tool.timeout_seconds,'input_schema':tool.input_model.model_json_schema(),'configured':search_configured(get_settings())}


def failure_result(error,run_id,state='failed'):
    if isinstance(error,(OperationalError,PoolTimeout)):error=ServiceError('DATABASE_BUSY','数据库暂时不可用，请稍后重试。',503,True,2)
    info=public_error(error)
    return {'success':False,'answer':info['message'],'error':info['message'],'error_info':info,
        'trace_id':run_id,'intent':None,'steps':[],'sources':[],'latency_ms':0,'run_state':state}

async def prepare(request,user):
    if request.conversation_id:
        def check_owner():
            with db_session() as db:owned(db,request.conversation_id,user.id)
        await asyncio.to_thread(check_owner)
    settings=get_settings()
    selected=request.model or settings.LLM_MODEL
    if not credential_configured(settings.LLM_API_KEY) or not credential_configured(selected):
        raise HTTPException(422,'请先在模型配置页连接模型服务。')
    ticket=get_runtime().scheduler.admit(user.id)
    reservation=None
    reserving=None
    try:
        if request.model and selected not in await available_models(settings):raise HTTPException(422,'所选模型不可用，请刷新模型列表。')
        # Snapshot credentials before queueing. Later admin changes affect new submissions only.
        llm=OpenAIClient(api_key=settings.LLM_API_KEY,base_url=settings.LLM_BASE_URL or 'https://api.openai.com/v1',model=selected,settings_snapshot=settings)
        fingerprint=hashlib.sha256(json.dumps(request.model_dump(exclude={'client_request_id'}),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        reserving=asyncio.create_task(asyncio.to_thread(reserve_turn,user.id,request.conversation_id,request.user_input,selected,request.client_request_id,fingerprint))
        reservation=await asyncio.shield(reserving)
        return selected,reservation,ticket,llm
    except BaseException:
        try:
            if reserving is not None and reservation is None:
                # The SQL thread may have committed after HTTP cancellation; finish it safely.
                try:reservation=await asyncio.shield(reserving)
                except Exception:pass
            if reservation:
                result=failure_result(ServiceError('REQUEST_CANCELLED','请求已取消。',409),reservation[2],'cancelled')
                await asyncio.to_thread(finish_turn,*reservation[:3],result)
        finally:get_runtime().scheduler.release(ticket)
        raise

@dataclass
class RunControl:
    task:Any=None
    started:bool=False
    cancel_requested:bool=False
    finishing:bool=False

async def execute_turn(request,user_id,selected,reservation,ticket,llm,on_event=None,control=None):
    conversation_id,message_id,run_id,history=reservation
    runtime=get_runtime()
    result=None
    queued_at=time.monotonic()
    wait_ms=0
    if control:control.started=True
    try:
        if control and control.cancel_requested:raise asyncio.CancelledError
        if on_event:await on_event({'type':'status','phase':'queued','message':'任务已受理，等待执行配额','run_id':run_id})
        await runtime.scheduler.wait(ticket)
        wait_ms=int((time.monotonic()-queued_at)*1000)
        if not await asyncio.to_thread(mark_running,run_id):
            raise ServiceError('STALE_RUN','任务状态已改变，停止执行。',409)
        async with asyncio.timeout(runtime.scheduler.settings.AGENT_RUN_TIMEOUT_SECONDS):
            result=await run_agent(user_input=request.user_input,user_id=user_id,session_history=history,model=selected,on_event=on_event,llm_client=llm)
    except asyncio.CancelledError:
        result=failure_result(ServiceError('SHUTDOWN_INTERRUPTED','服务关闭，任务已中断。',503,True,5),run_id,'interrupted') if runtime.scheduler.closing else failure_result(ServiceError('RUN_CANCELLED','任务已取消。',409),run_id,'cancelled')
    except TimeoutError:
        result=failure_result(ServiceError('RUN_TIMEOUT','任务执行超时，请缩小任务范围后重试。',504,True,5),run_id,'timed_out')
    except Exception as error:
        result=failure_result(error,run_id,'expired' if isinstance(error,ServiceError) and error.code=='QUEUE_TIMEOUT' else 'failed')
    if not ticket.running:wait_ms=int((time.monotonic()-queued_at)*1000)
    if result and result.get('latency_ms')==0:result['latency_ms']=max(0,int((time.monotonic()-queued_at)*1000)-wait_ms)
    if control:control.finishing=True
    try:
        result.update(conversation_id=conversation_id,message_id=message_id,model=selected,run_id=run_id,queue_wait_ms=wait_ms)
        result.setdefault('run_state','succeeded' if result.get('success') else 'failed')
        saving=asyncio.create_task(asyncio.to_thread(finish_turn,conversation_id,message_id,run_id,result))
        try:saved=await asyncio.shield(saving)
        except asyncio.CancelledError:saved=await asyncio.shield(saving)
        if not saved:raise ServiceError('STALE_RUN','任务执行标识已失效，结果未覆盖现有会话。',409)
        return result
    except (OperationalError,PoolTimeout):
        runtime.persistence_failures+=1
        logger.error('result_persist_failed run=%s',run_id)
        raise ServiceError('RESULT_PERSIST_FAILED','结果保存失败，请刷新任务状态或联系管理员。',503,True,2) from None
    finally:
        runtime.scheduler.release(ticket)
        runtime.runs.pop(run_id,None)


def launch(request,user_id,prepared,on_event=None):
    selected,reservation,ticket,llm=prepared
    runtime=get_runtime()
    control=RunControl()
    task=asyncio.create_task(execute_turn(request,user_id,selected,reservation,ticket,llm,on_event,control))
    control.task=task
    runtime.runs[reservation[2]]=control
    runtime.scheduler.track(task)
    return task

@app.post('/chat',response_model=ChatResponse)
async def chat(request:ChatRequest,user=Depends(require_user)):
    prepared=await prepare(request,user)
    # Both entrypoints use the same scheduler; browser disconnection doesn't cancel a run.
    return await asyncio.shield(launch(request,user.id,prepared))

@app.post('/chat/stream',response_class=StreamingResponse)
async def chat_stream(request:ChatRequest,user=Depends(require_user)):
    prepared=await prepare(request,user)
    queue=asyncio.Queue(maxsize=get_runtime().scheduler.settings.STREAM_BUFFER_EVENTS)
    connected=True
    async def event(value):
        nonlocal connected
        if not connected:return
        try:queue.put_nowait(value)
        except asyncio.QueueFull:
            # Detach a slow subscriber instead of blocking the agent or growing memory.
            connected=False
            while not queue.empty():queue.get_nowait()
            queue.put_nowait({'type':'error','code':'SLOW_CONSUMER','message':'接收速度过慢，已停止推送；任务仍在运行，请刷新会话恢复。'})
    task=launch(request,user.id,prepared,event)
    async def deliver():
        try:await event({'type':'done','result':await asyncio.shield(task)})
        except Exception as error:await event({'type':'error',**public_error(error)})
        finally:await event({'type':'end'})
    get_runtime().scheduler.track(asyncio.create_task(deliver()))
    async def stream():
        nonlocal connected
        try:
            yield json.dumps({'type':'start','conversation_id':prepared[1][0],'message_id':prepared[1][1],'run_id':prepared[1][2]},ensure_ascii=False)+'\n'
            while True:
                try:value=await asyncio.wait_for(queue.get(),timeout=10)
                except TimeoutError:value={'type':'heartbeat'}
                if value['type']=='end':break
                yield json.dumps(value,ensure_ascii=False)+'\n'
                if value['type']=='error':break
        finally:connected=False
    return StreamingResponse(stream(),media_type='application/x-ndjson',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})

@app.get('/runs/{run_id}')
def read_run(run_id:str,user=Depends(require_user),db=Depends(get_db)):
    run=db.query(AgentRun).filter_by(id=run_id,user_id=user.id).first()
    if run is None:raise HTTPException(404,'任务不存在。')
    return run_view(run)

@app.post('/runs/{run_id}/cancel')
async def cancel_run(run_id:str,user=Depends(require_user)):
    def lookup():
        with db_session() as db:
            row=db.query(AgentRun).filter_by(id=run_id,user_id=user.id).first()
            if row is None:raise HTTPException(404,'任务不存在。')
            return run_view(row)
    value=await asyncio.to_thread(lookup)
    control=get_runtime().runs.get(run_id)
    if control and not control.finishing:
        control.cancel_requested=True
        if control.started:control.task.cancel()
        return {'id':run_id,'cancellation_requested':True}
    if control and control.finishing:return {'id':run_id,'cancellation_requested':False,'state':'finishing'}
    if value['state'] in ('queued','running'):
        raise ServiceError('RUN_NOT_OWNED_BY_PROCESS','任务运行实例不可用，请联系管理员。',503,True,5)
    return {'id':run_id,'cancellation_requested':False,'state':value['state']}

@app.post('/intent')
async def intent(request:ChatRequest,user=Depends(require_user)):
    history=[]
    if request.conversation_id:
        def load_history():
            with db_session() as db:
                owned(db,request.conversation_id,user.id)
                rows=db.query(ConversationMessage).filter_by(conversation_id=request.conversation_id,status='completed').order_by(ConversationMessage.id.desc()).limit(50).all()
                return bounded_history([{'role':row.role,'content':row.content} for row in reversed(rows)])
        history=await asyncio.to_thread(load_history)
    scheduler=get_runtime().scheduler
    ticket=scheduler.admit(user.id)
    try:
        await scheduler.wait(ticket)
        async with asyncio.timeout(scheduler.settings.AGENT_RUN_TIMEOUT_SECONDS):
            if request.model and request.model not in await available_models():raise HTTPException(422,'所选模型不可用。')
            router=IntentRouter(OpenAIClient(model=request.model))
            return (await router.route(request.user_input,history)).model_dump()
    except TimeoutError:raise ServiceError('RUN_TIMEOUT','意图识别超时。',504,True,5) from None
    finally:scheduler.release(ticket)

FRONTEND_DIST=Path(__file__).resolve().parents[2]/'frontend'/'dist'
@app.get('/console/',include_in_schema=False)
def console():
    if (FRONTEND_DIST/'index.html').is_file():return FileResponse(FRONTEND_DIST/'index.html')
    return HTMLResponse('<h1>Agent Studio</h1><p>Run npm ci and npm run build in frontend/, or open http://127.0.0.1:5173/console/ during development.</p>',status_code=503)
app.mount('/console',StaticFiles(directory=FRONTEND_DIST,html=True,check_dir=False),name='console')
