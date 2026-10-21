"""OpenAI-compatible server-side client. Missing credentials never fall back to mock."""
from abc import ABC, abstractmethod
import json
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI, APIStatusError, APITimeoutError, APIConnectionError

from agent_system.config.settings import credential_configured, get_settings


class LLMError(Exception):
    pass


class BaseLLMClient(ABC):
    @abstractmethod
    async def chat(self, messages: List[Dict[str, str]], **kwargs) -> str:
        raise NotImplementedError

    async def stream_chat(self, messages: List[Dict[str,str]], **kwargs):
        yield await self.chat(messages,**kwargs)

    @abstractmethod
    async def extract_structured(self, prompt: str, schema: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        raise NotImplementedError


class OpenAIClient(BaseLLMClient):
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 model: Optional[str] = None):
        settings = get_settings()
        self.api_key = api_key or settings.LLM_API_KEY
        self.base_url = base_url or settings.LLM_BASE_URL or None
        self.model = model or settings.LLM_MODEL
        if not credential_configured(self.api_key) or not credential_configured(self.model):
            raise LLMError('请在界面「模型配置」中连接模型服务并选择模型。')

    async def chat(self, messages: List[Dict[str, str]], **kwargs) -> str:
        settings = get_settings()
        try:
            async with AsyncOpenAI(api_key=self.api_key, base_url=self.base_url,
                                   timeout=settings.LLM_TIMEOUT_SECONDS, max_retries=0) as client:
                response = await client.chat.completions.create(model=self.model, messages=messages, **kwargs)
                content = response.choices[0].message.content
                if not content:
                    raise LLMError('LLM returned an empty answer')
                return content
        except APIStatusError as error:
            raise LLMError(f'LLM request rejected (HTTP {error.status_code}); check model, credentials and quota.') from None
        except APITimeoutError:
            raise LLMError('LLM request timeout') from None
        except APIConnectionError:
            raise LLMError('LLM connection failed; check LLM_BASE_URL and network.') from None

    async def stream_chat(self,messages,**kwargs):
        settings=get_settings()
        try:
            async with AsyncOpenAI(api_key=self.api_key,base_url=self.base_url,timeout=settings.LLM_TIMEOUT_SECONDS,max_retries=0) as client:
                stream=await client.chat.completions.create(model=self.model,messages=messages,stream=True,**kwargs)
                received=False
                async for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta.content:
                        received=True
                        yield chunk.choices[0].delta.content
                if not received:raise LLMError('LLM returned an empty answer')
        except APIStatusError as error:
            raise LLMError(f'LLM request rejected (HTTP {error.status_code}); check model, credentials and quota.') from None
        except APITimeoutError:
            raise LLMError('LLM request timeout') from None
        except APIConnectionError:
            raise LLMError('LLM connection failed; check model endpoint and network.') from None

    async def extract_structured(self, prompt: str, schema: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        messages = [
            {'role': 'system', 'content': 'Return only a JSON object matching the supplied JSON Schema. Do not invent tools or parameters.'},
            {'role': 'user', 'content': f'{prompt}\nJSON Schema:\n{json.dumps(schema, ensure_ascii=False)}'},
        ]
        if get_settings().LLM_JSON_MODE:
            kwargs['response_format'] = {'type': 'json_object'}
        content = await self.chat(messages, **kwargs)
        try:
            value = json.loads(content)
        except (ValueError, TypeError):
            raise LLMError('LLM returned invalid JSON; check JSON-mode support.') from None
        if not isinstance(value, dict):
            raise LLMError('LLM structured output must be an object')
        return value


def get_llm_client() -> BaseLLMClient:
    return OpenAIClient()
