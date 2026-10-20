"""
工具系统 - 注册表、Mock工具、通知网关
"""
from typing import Dict, Any, Optional, Callable, List, Union, get_type_hints
from pydantic import BaseModel, Field, create_model, ConfigDict
from functools import cached_property
import inspect
import asyncio
import time
from datetime import datetime

from agent_system.memory.procedural_memory import get_procedural_memory


class ToolResult(BaseModel):
    """工具执行结果"""
    success: bool
    data: Any = None
    error: Optional[str] = None
    latency_ms: int = 0


class BaseTool:
    """工具基类"""
    
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        from agent_system.config.settings import get_settings
        self.timeout_seconds = get_settings().TOOL_TIMEOUT_SECONDS

    @cached_property
    def input_model(self):
        """工具签名是参数类型与模型提取 Schema 的唯一来源。"""
        hints = get_type_hints(self._run)
        fields = {}
        for name, parameter in inspect.signature(self._run).parameters.items():
            if parameter.kind in (parameter.VAR_KEYWORD, parameter.VAR_POSITIONAL):
                continue
            annotation = hints.get(name, Any)
            default = ... if parameter.default is inspect.Parameter.empty else parameter.default
            if default is None:
                annotation = Optional[annotation]
            fields[name] = (annotation, default)
        return create_model(f"{self.name}_Input", __config__=ConfigDict(extra="allow"), **fields)
    
    async def run(self, **kwargs) -> ToolResult:
        """执行工具"""
        start = time.time()
        try:
            params = self.input_model.model_validate(kwargs, strict=True).model_dump()
            result = await asyncio.wait_for(self._run(**params), timeout=self.timeout_seconds)
            return ToolResult(
                success=True,
                data=result,
                latency_ms=int((time.time() - start) * 1000)
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error="tool timeout" if isinstance(e, TimeoutError) else str(e),
                latency_ms=int((time.time() - start) * 1000)
            )
    
    async def _run(self, **kwargs) -> Any:
        """子类实现"""
        raise NotImplementedError


class ToolRegistry:
    """工具注册表"""
    
    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}
        self._register_mock_tools()
    
    def register(self, tool: BaseTool):
        """注册工具"""
        self._tools[tool.name] = tool
    
    def get(self, name: str) -> Optional[BaseTool]:
        """获取工具"""
        return self._tools.get(name)
    
    def exists(self, name: str) -> bool:
        """工具是否存在"""
        return name in self._tools
    
    def list_tools(self) -> List[str]:
        """列出所有工具"""
        return list(self._tools.keys())
    
    def get_descriptions(self) -> str:
        """获取工具描述（供LLM使用）"""
        lines = []
        for name, tool in self._tools.items():
            lines.append(f"- {name}: {tool.description}")
        return "\n".join(lines)
    
    def _register_mock_tools(self):
        """注册Mock工具"""
        
        # ==================== 查询类工具 ====================
        
        class QueryUserTool(BaseTool):
            def __init__(self):
                super().__init__("query_user", "查询用户信息")
            
            async def _run(self, names: List[str] = None, user_ids: List[str] = None, **kwargs):
                # Mock数据
                mock_users = {
                    "张三": {"id": "u1", "name": "张三", "email": "zhangsan@example.com", "department": "技术部"},
                    "李四": {"id": "u2", "name": "李四", "email": "lisi@example.com", "department": "产品部"},
                    "王五": {"id": "u3", "name": "王五", "email": "wangwu@example.com", "department": "技术部"},
                }
                
                if names:
                    selected = [mock_users.get(name, {"id": f"u_unknown_{name}", "name": name, "email": f"{name}@example.com"}) for name in names]
                    users = {user["id"]: user for user in selected}
                    return {"users": users, "user_ids": [u["id"] for u in users.values()]}
                elif user_ids:
                    users = {uid: {"id": uid, "name": f"用户{uid}", "email": f"{uid}@example.com"} for uid in user_ids}
                    return {"users": users, "user_ids": user_ids}
                else:
                    users = {user["id"]: user for user in mock_users.values()}
                    return {"users": users, "user_ids": list(users)}
        
        class QueryEventsTool(BaseTool):
            def __init__(self):
                super().__init__("query_events", "查询用户事件")
            
            async def _run(self, user_ids: List[str] = None, date: str = None, **kwargs):
                # Mock数据
                return {
                    "events": {
                        "u1": [{"title": "产品评审", "time": "14:00", "location": "会议室A"}],
                        "u2": [{"title": "季度汇报", "time": "15:30", "location": "会议室B"}],
                    },
                    "date": date or datetime.now().strftime("%Y-%m-%d")
                }
        
        class QueryBirthdayTool(BaseTool):
            def __init__(self):
                super().__init__("query_birthday", "查询生日信息")
            
            async def _run(self, user_ids: List[str] = None, **kwargs):
                return {
                    "birthdays": {uid: {"is_today": False, "birthday": "1990-01-01"} for uid in (user_ids or [])}
                }
        
        class QueryTodoTool(BaseTool):
            def __init__(self):
                super().__init__("query_todo", "查询待办事项")
            
            async def _run(self, user_id: str = None, status: str = None, **kwargs):
                return {
                    "todos": [
                        {"id": "t1", "title": "完成报告", "status": "pending", "priority": "high"},
                        {"id": "t2", "title": "回复邮件", "status": "pending", "priority": "medium"},
                    ]
                }
        
        class QueryEmployeeTool(BaseTool):
            def __init__(self):
                super().__init__("query_employee", "查询员工信息")
            
            async def _run(self, name: str = None, department: str = None, **kwargs):
                mock_employees = [
                    {"id": "e1", "name": "张三", "department": "技术部", "position": "工程师"},
                    {"id": "e2", "name": "李四", "department": "产品部", "position": "经理"},
                ]
                
                if name:
                    return {"employees": [e for e in mock_employees if name in e["name"]]}
                elif department:
                    return {"employees": [e for e in mock_employees if e["department"] == department]}
                return {"employees": mock_employees}
        
        class QueryGroupTool(BaseTool):
            def __init__(self):
                super().__init__("query_group", "查询群组/部门成员")
            
            async def _run(self, department: str = None, group_name: str = None, **kwargs):
                return {
                    "members": [
                        {"id": "u1", "name": "张三"},
                        {"id": "u2", "name": "李四"},
                    ],
                    "count": 2
                }
        
        # ==================== 创建类工具 ====================
        
        class CreateReminderTool(BaseTool):
            def __init__(self):
                super().__init__("create_reminder", "创建提醒")
            
            async def _run(self, time: str, content: str, **kwargs):
                import time as time_module
                return {
                    "reminder_id": f"r_{int(time_module.time())}",
                    "time": time,
                    "content": content,
                    "status": "scheduled"
                }
        
        class CreateMeetingTool(BaseTool):
            def __init__(self):
                super().__init__("create_meeting", "创建会议")
            
            async def _run(self, participant_ids: List[str], start_time: str, topic: str = "", **kwargs):
                return {
                    "meeting_id": f"m_{int(time.time())}",
                    "participant_ids": participant_ids,
                    "start_time": start_time,
                    "topic": topic or "未命名会议",
                    "status": "created"
                }
        
        # ==================== 通知类工具 ====================
        
        class SendNotificationTool(BaseTool):
            def __init__(self):
                super().__init__("send_notification", "发送通知")
            
            async def _run(self, recipients: List[str], contents: Union[List[str], Dict[str, str]], channel: str = "email", **kwargs):
                # Mock发送
                results = {}
                for i, recipient in enumerate(recipients):
                    if isinstance(contents, dict):
                        content = contents.get(recipient, "默认内容")
                    else:
                        content = contents[i] if i < len(contents) else contents[0] if contents else "默认内容"
                    results[recipient] = {
                        "success": True,
                        "message_id": f"msg_{int(time.time())}_{i}",
                        "channel": channel
                    }
                
                return {
                    "sent": len(recipients),
                    "failed": 0,
                    "results": results,
                    "channel": channel
                }
        
        # ==================== 内容生成类工具（Mock，实际由LLM完成） ====================
        
        class GenerateContentTool(BaseTool):
            def __init__(self):
                super().__init__("generate_content", "生成通知内容")
            
            async def _run(self, users: Dict = None, events: Dict = None, **kwargs):
                contents = {}
                recipients = []
                
                for user_id, user_info in (users or {}).items():
                    user_events = (events or {}).get(user_id, [])
                    content = f"尊敬的{user_info.get('name', user_id)}，您有{len(user_events)}个待处理事件。"
                    contents[user_id] = content
                    recipients.append(user_id)
                
                return {
                    "contents": contents,
                    "recipients": recipients,
                    "count": len(contents)
                }
        
        class GenerateGreetingTool(BaseTool):
            def __init__(self):
                super().__init__("generate_greeting", "生成祝福内容")
            
            async def _run(self, users: Dict = None, birthdays: Dict = None, **kwargs):
                contents = {}
                recipients = []
                
                for user_id, user_info in (users or {}).items():
                    content = f"亲爱的{user_info.get('name', user_id)}，祝您生日快乐！"
                    contents[user_id] = content
                    recipients.append(user_id)
                
                return {
                    "contents": contents,
                    "recipients": recipients,
                    "count": len(contents)
                }
        
        class GenerateAnswerTool(BaseTool):
            def __init__(self):
                super().__init__("generate_answer", "生成答案")
            
            async def _run(self, query: str, retrieved: List = None, context: str = "", **kwargs):
                return {
                    "answer": f"根据检索到的{len(retrieved or [])}条资料，关于'{query}'的回答...",
                    "confidence": 0.85,
                    "sources": [r.get("source", "unknown") for r in (retrieved or [])]
                }
        
        # ==================== RAG类工具 ====================
        
        class SearchKnowledgeTool(BaseTool):
            def __init__(self):
                super().__init__("search_knowledge", "检索知识库")
            
            async def _run(self, query: str, top_k: int = 5, **kwargs):
                # Mock检索结果
                return {
                    "chunks": [
                        {"text": f"关于{query}的资料1...", "source": "员工手册.pdf", "score": 0.92},
                        {"text": f"关于{query}的资料2...", "source": "规章制度.docx", "score": 0.85},
                    ],
                    "query": query,
                    "count": 2
                }
        
        class QueryMemoryTool(BaseTool):
            def __init__(self):
                super().__init__("query_memory", "查询工作记忆")
            
            async def _run(self, query: str = None, **kwargs):
                return {
                    "context": "用户之前查询过相关信息...",
                    "query": query
                }
        
        # ==================== 文档处理类工具 ====================
        
        class ParseDocumentTool(BaseTool):
            def __init__(self):
                super().__init__("parse_document", "解析文档")
            
            async def _run(self, file_path: str, **kwargs):
                return {
                    "text": f"文档{file_path}的解析内容...",
                    "file_path": file_path,
                    "pages": 10
                }
        
        class ChunkTextTool(BaseTool):
            def __init__(self):
                super().__init__("chunk_text", "文本分块")
            
            async def _run(self, text: str, chunk_size: int = 500, **kwargs):
                chunks = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]
                return {
                    "chunks": chunks,
                    "count": len(chunks)
                }
        
        class EmbedChunksTool(BaseTool):
            def __init__(self):
                super().__init__("embed_chunks", "向量化文本块")
            
            async def _run(self, chunks: List[str], **kwargs):
                return {
                    "embeddings": [[0.1] * 768 for _ in chunks],
                    "count": len(chunks)
                }
        
        class StoreKnowledgeTool(BaseTool):
            def __init__(self):
                super().__init__("store_knowledge", "存储知识")
            
            async def _run(self, chunks: List, embeddings: List, metadata: Dict = None, **kwargs):
                return {
                    "stored": len(chunks),
                    "collection": "knowledge_base"
                }
        
        # ==================== 日志类工具 ====================
        
        class LogResultTool(BaseTool):
            def __init__(self):
                super().__init__("log_result", "记录结果")
            
            async def _run(self, task_type: str = None, results: Dict = None, **kwargs):
                return {
                    "logged": True,
                    "task_type": task_type,
                    "log_id": f"log_{int(time.time())}"
                }
        
        # 注册所有Mock工具
        self.register(QueryUserTool())
        self.register(QueryEventsTool())
        self.register(QueryBirthdayTool())
        self.register(QueryTodoTool())
        self.register(QueryEmployeeTool())
        self.register(QueryGroupTool())
        self.register(CreateReminderTool())
        self.register(CreateMeetingTool())
        self.register(SendNotificationTool())
        self.register(GenerateContentTool())
        self.register(GenerateGreetingTool())
        self.register(GenerateAnswerTool())
        self.register(SearchKnowledgeTool())
        self.register(QueryMemoryTool())
        self.register(ParseDocumentTool())
        self.register(ChunkTextTool())
        self.register(EmbedChunksTool())
        self.register(StoreKnowledgeTool())
        self.register(LogResultTool())


# 全局工具注册表
_tool_registry: Optional[ToolRegistry] = None


def get_tool_registry() -> ToolRegistry:
    """获取工具注册表单例"""
    global _tool_registry
    if _tool_registry is None:
        _tool_registry = ToolRegistry()
    return _tool_registry


def get_tool(name: str) -> Optional[BaseTool]:
    """获取工具"""
    return get_tool_registry().get(name)


def list_available_tools() -> List[str]:
    """列出可用工具"""
    return get_tool_registry().list_tools()
