"""
FastAPI 路由
"""
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import mimetypes
from pydantic import BaseModel, Field, field_validator
from typing import List, Dict, Any, Optional
import uuid

from agent_system.core.agent import AgentCore, run_agent
from agent_system.core.intent_router import recognize_intent
from agent_system.db.session import get_db, init_db
from agent_system.tools.registry import get_tool_registry, list_available_tools
from agent_system.config.settings import get_settings

app = FastAPI(
    title="Agent System",
    description="ReAct-with-Plan 智能体框架",
    version="0.1.0"
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==================== 请求/响应模型 ====================

class ChatRequest(BaseModel):
    """聊天请求"""
    user_input: str = Field(..., min_length=1, max_length=12000, description="用户输入")
    user_id: str = Field(default="default_user", min_length=1, max_length=100, description="用户ID")
    session_history: List[Dict[str, str]] = Field(default_factory=list, max_length=100, description="会话历史")

    @field_validator("user_input", "user_id")
    @classmethod
    def non_blank(cls, value):
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class ChatResponse(BaseModel):
    """聊天响应"""
    trace_id: str
    success: bool
    answer: Optional[str]
    intent: Optional[Dict[str, Any]]
    steps: List[Dict[str, Any]]
    latency_ms: int
    error: Optional[str]


class IntentRequest(BaseModel):
    """意图识别请求"""
    user_input: str = Field(..., min_length=1, max_length=12000)
    session_history: List[Dict[str, str]] = Field(default_factory=list, max_length=100)

    @field_validator("user_input")
    @classmethod
    def non_blank(cls, value):
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class IntentResponse(BaseModel):
    """意图识别响应"""
    intent_id: str
    complexity: str
    confidence: float
    is_multi_intent: bool
    secondary_intents: List[str]
    extracted_slots: Dict[str, Any]
    reasoning: str
    routing_path: str


class RuntimeResponse(BaseModel):
    version: str
    llm_mode: str
    llm_model: str
    embedding_provider: str
    embedding_note: str
    tools_mode: str
    streaming: bool


class ToolListResponse(BaseModel):
    tools: List[str]
    count: int


class ToolInfoResponse(BaseModel):
    name: str
    description: str
    timeout_seconds: int


# ==================== 路由 ====================

@app.on_event("startup")
async def startup():
    """启动时初始化"""
    init_db()


@app.get("/")
async def root():
    """根路径"""
    return {
        "name": "Agent System",
        "version": "0.1.0",
        "status": "running"
    }


@app.get("/health")
async def health():
    """健康检查"""
    return {"status": "healthy"}


@app.get("/runtime", response_model=RuntimeResponse, tags=["Runtime"])
async def runtime_info():
    """返回不含密钥的运行配置；工具目前均为 Mock。"""
    settings = get_settings()
    from agent_system.utils.llm_client import get_llm_client, MockLLMClient
    client = get_llm_client()
    return {
        "version": "0.1.0",
        "llm_mode": "mock" if isinstance(client, MockLLMClient) else "api",
        "llm_model": client.model,
        "embedding_provider": settings.EMBEDDING_PROVIDER,
        "embedding_note": "配置值；实际加载失败时可能回退到 Mock",
        "tools_mode": "mock",
        "streaming": False,
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """
    聊天接口（完整Agent流程）
    """
    try:
        result = await run_agent(
            user_input=request.user_input,
            user_id=request.user_id,
            session_history=request.session_history
        )
        return ChatResponse(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/intent", response_model=IntentResponse)
async def recognize(request: IntentRequest, db=Depends(get_db)):
    """
    意图识别接口（仅识别，不执行）
    """
    try:
        decision = await recognize_intent(
            user_input=request.user_input,
            session_history=request.session_history,
            db_session=db
        )
        return IntentResponse(**decision.model_dump())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tools", response_model=ToolListResponse)
async def list_tools():
    """列出可用工具"""
    return {
        "tools": list_available_tools(),
        "count": len(list_available_tools())
    }


@app.get("/tools/{tool_name}", response_model=ToolInfoResponse)
async def get_tool_info(tool_name: str):
    """获取工具信息"""
    registry = get_tool_registry()
    tool = registry.get(tool_name)
    
    if not tool:
        raise HTTPException(status_code=404, detail=f"Tool not found: {tool_name}")
    
    return {
        "name": tool.name,
        "description": tool.description,
        "timeout_seconds": tool.timeout_seconds
    }


# 页面资源使用固定目录，不暴露项目文件和运行数据。
# Windows 注册表可能把 .mjs 识别为其他类型，显式声明 ES Module MIME。
mimetypes.add_type("text/javascript", ".mjs")
app.mount(
    "/console",
    StaticFiles(directory=Path(__file__).resolve().parents[2] / "frontend", html=True),
    name="console",
)

# ==================== 启动入口 ====================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
