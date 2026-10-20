"""
LLM 客户端 - 预留接口，支持多提供商
"""
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
import json
import re

from agent_system.config.settings import get_settings

settings = get_settings()


class BaseLLMClient(ABC):
    """LLM客户端基类"""
    
    @abstractmethod
    async def chat(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """对话接口"""
        pass
    
    @abstractmethod
    async def extract_structured(self, prompt: str, schema: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        """结构化输出接口"""
        pass


class OpenAIClient(BaseLLMClient):
    """OpenAI兼容客户端（OpenAI / DeepSeek / 其他兼容API）"""
    
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or settings.LLM_API_KEY
        self.base_url = base_url or settings.LLM_BASE_URL
        self.model = model or settings.LLM_MODEL
        
        if not self.api_key:
            raise ValueError("LLM API key not configured")
    
    async def chat(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """对话"""
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
            
            response = await client.chat.completions.create(
                model=self.model,
                messages=messages,
                **kwargs
            )
            return response.choices[0].message.content
        except Exception as e:
            raise LLMError(f"LLM chat failed: {str(e)}")
    
    async def extract_structured(self, prompt: str, schema: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        """结构化输出（使用Function Calling或JSON Mode）"""
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
            
            # 使用JSON Mode
            response = await client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a helpful assistant that outputs JSON."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                **kwargs
            )
            
            content = response.choices[0].message.content
            return json.loads(content)
        except Exception as e:
            raise LLMError(f"LLM structured extraction failed: {str(e)}")


class MockLLMClient(BaseLLMClient):
    """Mock客户端 - 用于无API Key时验证流程"""
    
    def __init__(self):
        self.model = "mock-llm"
    
    async def chat(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """Mock对话"""
        user_msg = messages[-1].get("content", "") if messages else ""
        return f"[MOCK LLM Response] Received: {user_msg[:100]}..."
    
    async def extract_structured(self, prompt: str, schema: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        """Mock结构化输出"""
        # 尝试从schema推断结构
        properties = schema.get("properties", {})
        result = {}
        
        for key, prop in properties.items():
            prop_type = prop.get("type", "string")
            if prop_type == "string":
                result[key] = f"mock_{key}"
            elif prop_type == "integer":
                result[key] = 0
            elif prop_type == "number":
                result[key] = 0.0
            elif prop_type == "boolean":
                result[key] = False
            elif prop_type == "array":
                result[key] = []
            elif prop_type == "object":
                result[key] = {}
            else:
                result[key] = None
        
        # 特殊处理常见字段
        if "intent_id" in properties:
            result["intent_id"] = "general"
        if "confidence" in properties:
            result["confidence"] = 0.85
        if "is_multi_intent" in properties:
            result["is_multi_intent"] = False
        if "complexity" in properties:
            result["complexity"] = "complex"
        if "skeleton" in properties or "steps" in properties:
            result["skeleton"] = ["query_user", "send_notification"]
        
        return result


class LLMError(Exception):
    """LLM错误"""
    pass


def get_llm_client() -> BaseLLMClient:
    """获取LLM客户端"""
    provider = settings.LLM_PROVIDER.lower()
    
    if provider in ("openai", "deepseek", "local"):
        try:
            return OpenAIClient()
        except ValueError:
            # 无API Key时返回Mock
            return MockLLMClient()
    else:
        return MockLLMClient()
