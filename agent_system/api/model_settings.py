"""Model discovery and local configuration endpoints."""
import httpx
from fastapi import APIRouter, HTTPException, Response, Depends
from pydantic import BaseModel, Field, SecretStr, field_validator
from typing import Optional
from agent_system.config.settings import credential_configured, get_settings
from agent_system.config.model_config import normalize_base_url, save_model_config

from agent_system.api.auth import require_user,require_admin
router=APIRouter(prefix='/config/model',tags=['Model configuration'],dependencies=[Depends(require_user)])

class ModelConnection(BaseModel):
    base_url: str = Field(min_length=1,max_length=2000)
    api_key: Optional[SecretStr] = Field(default=None)

    @field_validator('base_url')
    @classmethod
    def valid_url(cls,value):return normalize_base_url(value)

class ModelSave(ModelConnection):
    model: str = Field(min_length=1,max_length=500)
    json_mode: bool = True

class ModelConfigView(BaseModel):
    base_url: str
    model: str
    api_key_configured: bool
    json_mode: bool

class ModelList(BaseModel):
    models: list[str]


def public_config():
    settings=get_settings()
    return ModelConfigView(base_url=settings.LLM_BASE_URL or 'https://api.openai.com/v1',
        model=settings.LLM_MODEL if credential_configured(settings.LLM_MODEL) else '',
        api_key_configured=credential_configured(settings.LLM_API_KEY),json_mode=settings.LLM_JSON_MODE)


def resolve_key(request):
    settings=get_settings()
    key=request.api_key.get_secret_value().strip() if request.api_key else ''
    if not key and request.base_url == normalize_base_url(settings.LLM_BASE_URL or 'https://api.openai.com/v1'):
        key=settings.LLM_API_KEY
    if not credential_configured(key):
        raise HTTPException(422,'请输入 API Key；更换服务地址时需要重新输入密钥。')
    return key

async def discover_models(base_url,key):
    try:
        async with httpx.AsyncClient(timeout=20,follow_redirects=False) as client:
            result=await client.get(base_url+'/models',headers={'Authorization':'Bearer '+key})
            result.raise_for_status()
            data=result.json()
        if not isinstance(data,dict) or not isinstance(data.get('data'),list):raise ValueError()
        models=sorted({item['id'] for item in data['data'] if isinstance(item,dict) and isinstance(item.get('id'),str) and item['id'].strip() and len(item['id'])<=500})
        if not models:raise HTTPException(422,'服务没有返回可用模型，请检查 API 地址及账号权限。')
        return models
    except httpx.HTTPStatusError as error:
        raise HTTPException(502,f'读取模型失败（HTTP {error.response.status_code}），请检查 API 地址、密钥及服务是否支持 GET /models。') from None
    except httpx.RequestError:
        raise HTTPException(502,'读取模型连接失败或超时，请检查 API 地址及网络。') from None
    except ValueError:
        raise HTTPException(502,'服务返回了无效的模型列表，需要 OpenAI 兼容的 GET /models 接口。') from None

@router.get('',response_model=ModelConfigView)
async def read_config(response:Response):
    response.headers['Cache-Control']='no-store'
    return public_config()

@router.post('/models',response_model=ModelList,dependencies=[Depends(require_admin)])
async def list_models(request:ModelConnection,response:Response):
    response.headers['Cache-Control']='no-store'
    return ModelList(models=await discover_models(request.base_url,resolve_key(request)))

@router.put('',response_model=ModelConfigView,dependencies=[Depends(require_admin)])
async def update_config(request:ModelSave,response:Response):
    key=resolve_key(request)
    models=await discover_models(request.base_url,key)
    if request.model not in models:raise HTTPException(422,'所选模型不在当前服务返回的列表中，请重新读取模型。')
    try:
        save_model_config({'LLM_BASE_URL':request.base_url,'LLM_API_KEY':key,'LLM_MODEL':request.model,'LLM_JSON_MODE':request.json_mode})
    except OSError:
        raise HTTPException(500,'模型配置保存失败，请检查配置目录的写入权限。') from None
    get_settings.cache_clear()
    _catalog_cache.clear()
    response.headers['Cache-Control']='no-store'
    return public_config()


# A short-lived cache avoids a provider request for every turn/model menu opening.
import hashlib
import time
_catalog_cache={}
async def available_models():
    settings=get_settings()
    if not credential_configured(settings.LLM_API_KEY):return []
    key=(settings.LLM_BASE_URL or 'https://api.openai.com/v1',hashlib.sha256(settings.LLM_API_KEY.encode()).hexdigest())
    cached=_catalog_cache.get(key)
    if cached and cached[0]>time.monotonic():return cached[1]
    values=await discover_models(key[0].rstrip('/'),settings.LLM_API_KEY)
    _catalog_cache.clear()
    _catalog_cache[key]=(time.monotonic()+60,values)
    return values
