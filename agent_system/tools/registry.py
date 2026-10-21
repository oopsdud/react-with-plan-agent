"""Typed tool registry. The only bundled tool is keyless web search."""
import asyncio
import inspect
import time
from functools import cached_property
from typing import Annotated, Any, Dict, List, Optional, get_type_hints

import httpx
from pydantic import BaseModel, ConfigDict, Field, create_model

from agent_system.config.settings import get_settings
from agent_system.tools.web_search import search_web


class ToolResult(BaseModel):
    success: bool
    data: Any = None
    error: Optional[str] = None
    latency_ms: int = 0


class BaseTool:
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        self.timeout_seconds = get_settings().TOOL_TIMEOUT_SECONDS

    @cached_property
    def input_model(self):
        hints = get_type_hints(self._run, include_extras=True)
        fields = {}
        for name, parameter in inspect.signature(self._run).parameters.items():
            if parameter.kind in (parameter.VAR_KEYWORD, parameter.VAR_POSITIONAL):
                continue
            default = ... if parameter.default is inspect.Parameter.empty else parameter.default
            fields[name] = (hints.get(name, Any), default)
        return create_model(f'{self.name}_Input', __config__=ConfigDict(extra='forbid'), **fields)

    async def run(self, **kwargs) -> ToolResult:
        start = time.monotonic()
        try:
            params = self.input_model.model_validate(kwargs, strict=True).model_dump()
            result = await asyncio.wait_for(self._run(**params), timeout=self.timeout_seconds)
            return ToolResult(success=True, data=result, latency_ms=int((time.monotonic() - start) * 1000))
        except Exception as error:
            return ToolResult(success=False, error='tool timeout' if isinstance(error, TimeoutError) else str(error),
                              latency_ms=int((time.monotonic() - start) * 1000))

    async def _run(self, **kwargs) -> Any:
        raise NotImplementedError


class WebSearchTool(BaseTool):
    def __init__(self, transport: Optional[httpx.AsyncBaseTransport] = None):
        super().__init__('web_search', '搜索互联网网页，返回标题、链接和内容摘要；用于获取和核实公开信息。')
        self.transport = transport

    async def _run(self, query: Annotated[str, Field(min_length=1, max_length=2000)],
                   max_results: Annotated[int, Field(ge=1, le=10)] = 5) -> Dict[str, Any]:
        if not query.strip():
            raise ValueError('query must not be blank')
        return await search_web(query,max_results,get_settings(),self.transport)



class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}
        self.register(WebSearchTool())

    def register(self, tool: BaseTool):
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def exists(self, name: str) -> bool:
        return name in self._tools

    def list_tools(self) -> List[str]:
        return list(self._tools)

    def get_descriptions(self) -> str:
        return '\n'.join(f'- {name}: {tool.description}' for name, tool in self._tools.items())


_tool_registry: Optional[ToolRegistry] = None


def get_tool_registry() -> ToolRegistry:
    global _tool_registry
    if _tool_registry is None:
        _tool_registry = ToolRegistry()
    return _tool_registry


def get_tool(name: str) -> Optional[BaseTool]:
    return get_tool_registry().get(name)


def list_available_tools() -> List[str]:
    return get_tool_registry().list_tools()
