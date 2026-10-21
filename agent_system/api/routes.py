"""Authenticated Agent API, persistent conversations and incremental responses."""
import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Optional
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict, field_validator
from agent_system.config.settings import get_settings, credential_configured
from agent_system.db.session import init_db, db_session, get_db
from agent_system.core.agent import run_agent
from agent_system.core.intent_router import IntentRouter
from agent_system.utils.llm_client import OpenAIClient
from agent_system.tools.registry import get_tool_registry
from agent_system.tools.web_search import search_configured
from agent_system.api.auth import router as auth_router, require_user, seed_users
from agent_system.api.conversations import router as conversations_router, reserve_turn, finish_turn, owned, recover_interrupted
from agent_system.api.model_settings import router as model_router, available_models

app=FastAPI(title='Agent System',description='ReAct-with-Plan 智能体框架',version='0.3.0')
app.add_middleware(CORSMiddleware,allow_origins=get_settings().CORS_ORIGINS,allow_credentials=True,allow_methods=['GET','POST','PUT','DELETE'],allow_headers=['Content-Type'])
app.include_router(auth_router)
app.include_router(conversations_router)
app.include_router(model_router)

@app.middleware('http')
async def private_no_cache(request,call_next):
    response=await call_next(request)
    if request.url.path.startswith(('/auth','/conversations','/chat','/config','/models','/runtime')):
        response.headers['Cache-Control']='no-store'
    return response


class ChatRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    user_input:str=Field(min_length=1,max_length=12000)
    conversation_id:Optional[str]=Field(default=None,max_length=64)
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

@app.on_event('startup')
def startup():
    init_db()
    with db_session() as db:
        seed_users(db)
        recover_interrupted(db)

@app.get('/')
def root():return {'name':'Agent System','version':'0.3.0','status':'running'}

@app.get('/health')
def health():return {'status':'healthy'}

@app.get('/runtime')
def runtime(user=Depends(require_user)):
    settings=get_settings()
    return {'version':'0.3.0','llm_mode':'api','llm_model':settings.LLM_MODEL,'llm_configured':credential_configured(settings.LLM_API_KEY) and credential_configured(settings.LLM_MODEL),
        'search_provider':settings.SEARCH_PROVIDER,'search_configured':search_configured(settings),'tools_mode':'live','streaming':True,'max_search_rounds':3}

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

async def prepare(request,user):
    # Reject a foreign conversation before any model/provider call.
    if request.conversation_id:
        with db_session() as db:owned(db,request.conversation_id,user.id)
    settings=get_settings()
    selected=request.model or settings.LLM_MODEL
    if not credential_configured(settings.LLM_API_KEY) or not credential_configured(selected):
        raise HTTPException(422,'请先在模型配置页连接模型服务。')
    if request.model and selected not in await available_models():raise HTTPException(422,'所选模型不可用，请刷新模型列表。')
    return selected,reserve_turn(user.id,request.conversation_id,request.user_input,selected)

async def execute_turn(request,user_id,selected,reservation,on_event=None):
    conversation_id,message_id,run_id,history=reservation
    result=None
    try:
        result=await run_agent(user_input=request.user_input,user_id=user_id,session_history=history,model=selected,on_event=on_event)
    except asyncio.CancelledError:
        result={'success':False,'answer':'任务已中断，请重新发送。','error':'任务中断','trace_id':run_id,'intent':None,'steps':[],'sources':[],'latency_ms':0}
        raise
    except Exception:
        result={'success':False,'answer':'任务执行失败，请检查模型服务及后端日志。','error':'任务服务异常','trace_id':run_id,'intent':None,'steps':[],'sources':[],'latency_ms':0}
    finally:
        if result is not None:
            result.update(conversation_id=conversation_id,message_id=message_id,model=selected)
            finish_turn(conversation_id,message_id,run_id,result)
    return result

@app.post('/chat',response_model=ChatResponse)
async def chat(request:ChatRequest,user=Depends(require_user)):
    selected,reservation=await prepare(request,user)
    return await execute_turn(request,user.id,selected,reservation)

_tasks=set()
@app.post('/chat/stream',response_class=StreamingResponse)
async def chat_stream(request:ChatRequest,user=Depends(require_user)):
    selected,reservation=await prepare(request,user)
    queue=asyncio.Queue()
    connected=True
    async def event(value):
        if connected:queue.put_nowait(value)
    async def work():
        try:
            result=await execute_turn(request,user.id,selected,reservation,event)
            await event({'type':'done','result':result})
        except Exception:
            await event({'type':'error','message':'执行记录保存失败，请刷新会话查看。'})
        finally:await event({'type':'end'})
    task=asyncio.create_task(work())
    _tasks.add(task);task.add_done_callback(_tasks.discard)
    async def stream():
        nonlocal connected
        try:
            yield json.dumps({'type':'start','conversation_id':reservation[0],'message_id':reservation[1]},ensure_ascii=False)+'\n'
            while True:
                try:value=await asyncio.wait_for(queue.get(),timeout=10)
                except asyncio.TimeoutError:value={'type':'heartbeat'}
                if value['type']=='end':break
                yield json.dumps(value,ensure_ascii=False)+'\n'
        finally:
            # Disconnecting a tab doesn't discard a committed turn. The background
            # task finishes and persists it; refresh can recover the final answer.
            connected=False
    return StreamingResponse(stream(),media_type='application/x-ndjson',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})

@app.post('/intent')
async def intent(request:ChatRequest,user=Depends(require_user)):
    history=[]
    if request.conversation_id:
        from agent_system.db.models import ConversationMessage
        with db_session() as db:
            owned(db,request.conversation_id,user.id)
            rows=db.query(ConversationMessage).filter_by(conversation_id=request.conversation_id,status='completed').order_by(ConversationMessage.id.desc()).limit(50).all()
            history=[{'role':row.role,'content':row.content} for row in reversed(rows)]
    if request.model and request.model not in await available_models():raise HTTPException(422,'所选模型不可用。')
    router=IntentRouter(OpenAIClient(model=request.model))
    return (await router.route(request.user_input,history)).model_dump()

FRONTEND_DIST=Path(__file__).resolve().parents[2]/'frontend'/'dist'
@app.get('/console/',include_in_schema=False)
def console():
    if (FRONTEND_DIST/'index.html').is_file():return FileResponse(FRONTEND_DIST/'index.html')
    return HTMLResponse('<h1>Agent Studio</h1><p>Run npm ci and npm run build in frontend/, or open http://127.0.0.1:5173/console/ during development.</p>',status_code=503)
app.mount('/console',StaticFiles(directory=FRONTEND_DIST,html=True,check_dir=False),name='console')
