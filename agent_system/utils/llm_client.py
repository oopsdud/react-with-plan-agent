"""OpenAI-compatible client with provider bulkheads, circuit protection and typed errors."""
from abc import ABC, abstractmethod
import json
from typing import Any, Dict, List, Optional
from contextlib import asynccontextmanager
from openai import AsyncOpenAI, APIStatusError, APITimeoutError, APIConnectionError
from agent_system.config.settings import credential_configured, get_settings
from agent_system.runtime import get_runtime
from agent_system.runtime.errors import ServiceError


class LLMError(ServiceError):
    def __init__(self,message,code='LLM_ERROR',retryable=False,status=502):
        super().__init__(code,message,status,retryable,5 if retryable else None)


class BaseLLMClient(ABC):
    @abstractmethod
    async def chat(self,messages,**kwargs):raise NotImplementedError
    async def stream_chat(self,messages,**kwargs):yield await self.chat(messages,**kwargs)
    @abstractmethod
    async def extract_structured(self,prompt,schema,**kwargs):raise NotImplementedError


class OpenAIClient(BaseLLMClient):
    def __init__(self,api_key:Optional[str]=None,base_url:Optional[str]=None,model:Optional[str]=None,settings_snapshot=None):
        settings=settings_snapshot or get_settings()
        self.api_key=api_key or settings.LLM_API_KEY
        self.base_url=base_url or settings.LLM_BASE_URL or None
        self.model=model or settings.LLM_MODEL
        self.timeout=settings.LLM_TIMEOUT_SECONDS
        self.json_mode=settings.LLM_JSON_MODE
        self.max_chars=settings.LLM_MAX_RESPONSE_CHARS
        if not credential_configured(self.api_key) or not credential_configured(self.model):
            raise LLMError('请在界面「模型配置」中连接模型服务并选择模型。','MODEL_NOT_CONFIGURED',False,422)

    @asynccontextmanager
    async def connection(self):
        identity=(self.base_url or 'https://api.openai.com/v1')+'|'+self.api_key
        async with get_runtime().guards.get('llm',identity).call():
            try:
                async with AsyncOpenAI(api_key=self.api_key,base_url=self.base_url,timeout=self.timeout,max_retries=0) as client:
                    yield client
            except APIStatusError as error:
                code='UPSTREAM_RATE_LIMITED' if error.status_code==429 else 'UPSTREAM_UNAVAILABLE' if error.status_code>=500 else 'UPSTREAM_REJECTED'
                raise LLMError(f'LLM request rejected (HTTP {error.status_code}); check model, credentials and quota.',code,error.status_code==429 or error.status_code>=500) from None
            except APITimeoutError:
                raise LLMError('LLM request timeout','UPSTREAM_TIMEOUT',True,504) from None
            except APIConnectionError:
                raise LLMError('LLM connection failed; check model endpoint and network.','UPSTREAM_UNAVAILABLE',True,503) from None

    async def chat(self,messages,**kwargs):
        async with self.connection() as client:
            response=await client.chat.completions.create(model=self.model,messages=messages,**kwargs)
            content=response.choices[0].message.content
            if content and len(content)>self.max_chars:raise LLMError('模型输出超过允许长度。','RESPONSE_TOO_LARGE')
            if not content:raise LLMError('LLM returned an empty answer','INVALID_MODEL_OUTPUT')
            return content

    async def stream_chat(self,messages,**kwargs):
        async with self.connection() as client:
            stream=await client.chat.completions.create(model=self.model,messages=messages,stream=True,**kwargs)
            received=False
            count=0
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    count+=len(chunk.choices[0].delta.content)
                    if count>self.max_chars:raise LLMError('模型输出超过允许长度。','RESPONSE_TOO_LARGE')
                    received=True
                    yield chunk.choices[0].delta.content
            if not received:raise LLMError('LLM returned an empty answer','INVALID_MODEL_OUTPUT')

    async def extract_structured(self,prompt,schema,**kwargs):
        messages=[{'role':'system','content':'Return only a JSON object matching the supplied JSON Schema. Do not invent tools or parameters.'},
                  {'role':'user','content':f'{prompt}\nJSON Schema:\n{json.dumps(schema,ensure_ascii=False)}'}]
        if self.json_mode:kwargs['response_format']={'type':'json_object'}
        content=await self.chat(messages,**kwargs)
        try:value=json.loads(content)
        except (ValueError,TypeError):raise LLMError('LLM returned invalid JSON; check JSON-mode support.','INVALID_MODEL_OUTPUT') from None
        if not isinstance(value,dict):raise LLMError('LLM structured output must be an object','INVALID_MODEL_OUTPUT')
        return value


def get_llm_client()->BaseLLMClient:return OpenAIClient()
