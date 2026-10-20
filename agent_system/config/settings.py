import sys
from pathlib import Path

# 添加自定义包路径
_CUSTOM_PACKAGES = Path(__file__).parent.parent.parent / ".python_packages"
if _CUSTOM_PACKAGES.exists():
    sys.path.insert(0, str(_CUSTOM_PACKAGES))

from pydantic_settings import BaseSettings
from pydantic import Field
from functools import lru_cache
from typing import Optional, List


class Settings(BaseSettings):
    """应用配置"""
    
    # 应用
    APP_NAME: str = "agent-system"
    DEBUG: bool = False
    CORS_ORIGINS: List[str] = ["http://127.0.0.1:8000", "http://localhost:8000"]
    
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
    
    # Embedding配置
    EMBEDDING_PROVIDER: str = "local"  # local / api
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
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
