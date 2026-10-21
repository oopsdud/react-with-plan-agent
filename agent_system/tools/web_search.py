"""Keyless search adapters behind one model-facing web_search tool."""
import asyncio
import importlib.util
import math
from urllib.parse import urlsplit
import httpx


def searxng_url(settings):
    value=(settings.SEARXNG_BASE_URL or '').strip().rstrip('/')
    try:
        parsed=urlsplit(value)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError()
    except ValueError:
        raise RuntimeError('请配置有效的 SEARXNG_BASE_URL（HTTP/HTTPS 实例地址）。') from None
    return value


def search_configured(settings):
    if settings.SEARCH_PROVIDER == 'ddgs':
        return importlib.util.find_spec('ddgs') is not None
    try:
        searxng_url(settings)
        return True
    except RuntimeError:
        return False


def ddgs_search(query,max_results,settings):
    try:
        from ddgs import DDGS
        from ddgs.exceptions import RatelimitException,TimeoutException
    except ImportError:
        raise RuntimeError('缺少 ddgs 依赖，请安装 agent_system/requirements.txt。') from None
    try:
        # DDGS is synchronous; the async entry point runs this function in a worker.
        with DDGS(timeout=settings.SEARCH_TIMEOUT_SECONDS,proxy=settings.DDGS_PROXY) as client:
            return list(client.text(query,max_results=max_results,backend=settings.DDGS_BACKEND,
                region=settings.DDGS_REGION,safesearch=settings.SEARCH_SAFESEARCH))
    except RatelimitException:
        raise RuntimeError('DDGS rate limit：公开搜索引擎限流，请稍后重试。') from None
    except TimeoutException:
        raise RuntimeError('DDGS search timeout，请检查网络或代理配置。') from None
    except Exception as error:
        # Only the library's explicit empty-result marker means "no matches".
        if str(error).strip().rstrip('.!').lower() == 'no results found':
            return []
        raise RuntimeError('DDGS search failed：公开搜索引擎可能阻断了请求，请检查网络、代理或更换 DDGS_BACKEND；也可自建 SearXNG。') from None


async def searxng_search(query,settings,transport=None):
    base_url=searxng_url(settings)
    try:
        async with httpx.AsyncClient(timeout=settings.SEARCH_TIMEOUT_SECONDS,transport=transport,follow_redirects=False) as client:
            response=await client.get(base_url+'/search',params={'q':query,'format':'json','categories':'general',
                'safesearch':{'off':0,'moderate':1,'on':2}[settings.SEARCH_SAFESEARCH]})
            response.raise_for_status()
            payload=response.json()
    except httpx.HTTPStatusError as error:
        status=error.response.status_code
        label='rate limit' if status==429 else 'temporary unavailable' if status>=500 else 'request rejected'
        raise RuntimeError(f'SearXNG {label} (HTTP {status})；请检查实例可用性及是否启用 JSON 搜索格式。') from None
    except httpx.TimeoutException:
        raise RuntimeError('SearXNG search timeout') from None
    except httpx.RequestError:
        raise RuntimeError('SearXNG network connection failed') from None
    except ValueError:
        raise RuntimeError('SearXNG returned invalid JSON，请启用 JSON 搜索格式。') from None
    if not isinstance(payload,dict) or not isinstance(payload.get('results'),list):
        raise RuntimeError('SearXNG returned an invalid search response')
    if not payload['results'] and payload.get('unresponsive_engines'):
        raise RuntimeError('SearXNG engines unavailable，检索源未正常返回，不能判定为无结果。')
    return payload['results']


def normalize_results(rows,max_results):
    if not isinstance(rows,list):
        raise RuntimeError('Search provider returned an invalid result list')
    results=[]
    seen=set()
    for row in rows[:100]:
        if not isinstance(row,dict):continue
        url=row.get('url',row.get('href',''))
        if not isinstance(url,str):continue
        try:
            parsed=urlsplit(url)
            if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:continue
        except ValueError:continue
        if url in seen:continue
        score=row.get('score')
        score=score if isinstance(score,(int,float)) and not isinstance(score,bool) and math.isfinite(score) else None
        results.append({'title':str(row.get('title') or url)[:500],'url':url,
            'content':str(row.get('content') or row.get('body') or '')[:5000],'score':score})
        seen.add(url)
        if len(results)>=max_results:break
    if rows and not results:
        raise RuntimeError('Search provider returned no valid web results（数据格式错误或链接无效）。')
    return results


async def search_web(query,max_results,settings,transport=None):
    if settings.SEARCH_PROVIDER=='ddgs':
        rows=await asyncio.wait_for(asyncio.to_thread(ddgs_search,query,max_results,settings),timeout=settings.SEARCH_TIMEOUT_SECONDS+1)
    else:
        rows=await searxng_search(query,settings,transport)
    results=normalize_results(rows,max_results)
    return {'query':query,'results':results,'count':len(results),'provider':settings.SEARCH_PROVIDER}
