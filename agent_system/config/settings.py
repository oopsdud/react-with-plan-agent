import sys
from pathlib import Path

# 添加自定义包路径
_CUSTOM_PACKAGES = Path(__file__).parent.parent.parent / ".python_packages"
if _CUSTOM_PACKAGES.exists():
    sys.path.insert(0, str(_CUSTOM_PACKAGES))

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from functools import lru_cache
from typing import Optional, List, Literal


class Settings(BaseSettings):
    """应用配置"""
    
    # 应用
    APP_NAME: str = "react-with-plan-agent"
    DEBUG: bool = False
    CORS_ORIGINS: List[str] = ["http://127.0.0.1:8000", "http://localhost:8000", "http://127.0.0.1:5173", "http://localhost:5173"]
    
    DEFAULT_USER1_PASSWORD: str = 'AgentUser1!'
    DEFAULT_USER2_PASSWORD: str = 'AgentUser2!'
    DEFAULT_USER3_PASSWORD: str = 'AgentUser3!'
    SESSION_TTL_SECONDS: int = Field(default=604800, ge=60)
    COOKIE_SECURE: bool = False

    # Single-process bounded scheduler. Use a shared broker before adding API workers.
    AUTH_LOGIN_PER_IP_PER_MINUTE: int = Field(default=60, ge=1, le=10000)
    AUTH_LOGIN_PER_ACCOUNT_PER_MINUTE: int = Field(default=10, ge=1, le=10000)
    HTTP_MAX_CONCURRENT: int = Field(default=128, ge=8, le=10000)
    AGENT_MAX_CONCURRENT: int = Field(default=4, ge=1, le=128)
    AGENT_MAX_QUEUED: int = Field(default=32, ge=0, le=10000)
    USER_MAX_CONCURRENT: int = Field(default=1, ge=1, le=32)
    USER_MAX_QUEUED: int = Field(default=3, ge=0, le=100)
    USER_REQUESTS_PER_MINUTE: int = Field(default=20, ge=1, le=10000)
    AGENT_QUEUE_TIMEOUT_SECONDS: float = Field(default=60, gt=0, le=3600)
    AGENT_RUN_TIMEOUT_SECONDS: float = Field(default=180, gt=0, le=3600)
    AGENT_SHUTDOWN_GRACE_SECONDS: float = Field(default=15, ge=0, le=120)
    LLM_MAX_CONCURRENT: int = Field(default=4, ge=1, le=128)
    SEARCH_MAX_CONCURRENT: int = Field(default=4, ge=1, le=128)
    DEPENDENCY_SLOT_TIMEOUT_SECONDS: float = Field(default=2, gt=0, le=60)
    CIRCUIT_FAILURE_THRESHOLD: int = Field(default=3, ge=1, le=100)
    CIRCUIT_OPEN_SECONDS: float = Field(default=30, gt=0, le=600)
    CHECKPOINT_MAX_STEPS: int = Field(default=64, ge=1, le=256)
    CHECKPOINT_MAX_VERSIONS: int = Field(default=12, ge=1, le=100)
    CHECKPOINT_MAX_RESUMES: int = Field(default=5, ge=1, le=30)
    CHECKPOINT_MAX_STEP_ATTEMPTS: int = Field(default=6, ge=1, le=30)
    CHECKPOINT_MAX_PARALLEL_STEPS: int = Field(default=4, ge=1, le=32)
    CHECKPOINT_MAX_BYTES: int = Field(default=2000000, ge=10000, le=10000000)
    CONTEXT_MAX_CHARS: int = Field(default=48000, ge=2000, le=500000)
    LLM_MAX_RESPONSE_CHARS: int = Field(default=60000, ge=1000, le=500000)
    STREAM_BUFFER_EVENTS: int = Field(default=128, ge=4, le=4096)
    MODEL_CATALOG_STALE_SECONDS: int = Field(default=600, ge=60, le=3600)

    # 数据库 - 本地开发默认SQLite
    DATABASE_URL: str = "sqlite:///./data/agent.db"
    
    # ChromaDB
    CHROMA_PERSIST_DIR: str = "./data/chroma"
    
    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    
    # Celery
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"
    
    # LLM配置（预留接口）
    LLM_PROVIDER: str = "openai"  # openai / deepseek / local
    LLM_API_KEY: Optional[str] = None
    LLM_BASE_URL: Optional[str] = None
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_TIMEOUT_SECONDS: int = Field(default=60, ge=1, le=600)
    LLM_JSON_MODE: bool = True

    # One bundled tool; both providers need no dedicated search API key.
    SEARCH_PROVIDER: Literal['ddgs','searxng'] = 'ddgs'
    DDGS_BACKEND: str = 'auto'
    DDGS_REGION: str = 'us-en'
    DDGS_PROXY: Optional[str] = None
    SEARXNG_BASE_URL: Optional[str] = None
    SEARCH_SAFESEARCH: Literal['off','moderate','on'] = 'moderate'
    SEARCH_TIMEOUT_SECONDS: int = Field(default=20, ge=1, le=120)
    EPISODIC_MEMORY_ENABLED: bool = False
    
    # Embedding配置
    EMBEDDING_PROVIDER: str = "api"  # optional memory only; disabled by default
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    EMBEDDING_API_KEY: Optional[str] = None
    EMBEDDING_BASE_URL: Optional[str] = None
    
    # 意图识别阈值
    INTENT_SEMANTIC_THRESHOLD: float = 0.75  # 语义路由置信度阈值
    INTENT_MULTI_THRESHOLD: float = 0.15     # 多意图判定分差阈值
    
    # 执行配置
    MAX_STEP_RETRIES: int = Field(default=3, ge=0, le=10)
    MAX_REPLAN_COUNT: int = 3
    TOOL_TIMEOUT_SECONDS: int = Field(default=30, ge=1, le=3600)
    
    # 记忆配置
    EPISODIC_MEMORY_TTL_DAYS: int = 7
    EPISODIC_MEMORY_PER_INTENT_LIMIT: int = 20
    
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8", extra="ignore",
    )


def credential_configured(value: Optional[str]) -> bool:
    return bool(value and value.strip() and not value.upper().startswith(("YOUR_", "REPLACE_", "<")))


@lru_cache()
def get_settings() -> Settings:
    from agent_system.config.model_config import load_model_config
    return Settings(**load_model_config())
