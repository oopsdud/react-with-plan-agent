"""
程序记忆 - 固化的工作流程（计划模板、提示词模板）
"""
from typing import Dict, List, Any, Optional
from dataclasses import dataclass
import json
import hashlib


@dataclass
class PromptTemplate:
    """提示词模板"""
    name: str
    version: str
    content: str
    variables: List[str]
    
    def render(self, **kwargs) -> str:
        """渲染模板"""
        result = self.content
        for var in self.variables:
            value = kwargs.get(var, f"{{{{{var}}}}}")
            result = result.replace(f"{{{{{var}}}}}", str(value))
        return result
    
    @property
    def hash(self) -> str:
        """模板哈希（版本追踪）"""
        return hashlib.sha256(self.content.encode()).hexdigest()[:16]


class ProceduralMemory:
    """程序记忆管理器"""
    
    def __init__(self):
        # 计划模板（与Planner中的PLAN_TEMPLATES对应）
        self._plan_templates: Dict[str, List[Dict[str, Any]]] = {}
        
        # 提示词模板
        self._prompt_templates: Dict[str, PromptTemplate] = {}
        
        # 工具使用模式
        self._tool_patterns: Dict[str, Dict[str, Any]] = {}
        
        self._init_default_templates()
    
    def _init_default_templates(self):
        """初始化默认模板"""
        
        # ========== 计划模板 ==========
        self._plan_templates = {
            "notify_event": [
                {"step_id": 1, "tool_name": "query_user", "params_template": {"names": "{{slots.person}}"}, "depends_on": []},
                {"step_id": 2, "tool_name": "query_events", "params_template": {"user_ids": "{{steps.1.result.user_ids}}", "date": "{{slots.date}}"}, "depends_on": [1]},
                {"step_id": 3, "tool_name": "generate_content", "params_template": {"users": "{{steps.1.result.users}}", "events": "{{steps.2.result.events}}"}, "depends_on": [1, 2]},
                {"step_id": 4, "tool_name": "send_notification", "params_template": {"recipients": "{{steps.3.result.recipients}}", "contents": "{{steps.3.result.contents}}"}, "depends_on": [3]},
            ],
            "greeting": [
                {"step_id": 1, "tool_name": "query_user", "params_template": {"names": "{{slots.person}}"}, "depends_on": []},
                {"step_id": 2, "tool_name": "query_birthday", "params_template": {"user_ids": "{{steps.1.result.user_ids}}"}, "depends_on": [1]},
                {"step_id": 3, "tool_name": "generate_greeting", "params_template": {"users": "{{steps.1.result.users}}"}, "depends_on": [1, 2]},
                {"step_id": 4, "tool_name": "send_notification", "params_template": {"recipients": "{{steps.3.result.recipients}}"}, "depends_on": [3]},
            ],
            "ingest_document": [
                {"step_id": 1, "tool_name": "parse_document", "params_template": {"file_path": "{{slots.file_path}}"}, "depends_on": []},
                {"step_id": 2, "tool_name": "chunk_text", "params_template": {"text": "{{steps.1.result.text}}"}, "depends_on": [1]},
                {"step_id": 3, "tool_name": "embed_chunks", "params_template": {"chunks": "{{steps.2.result.chunks}}"}, "depends_on": [2]},
                {"step_id": 4, "tool_name": "store_knowledge", "params_template": {"chunks": "{{steps.2.result.chunks}}", "embeddings": "{{steps.3.result.embeddings}}"}, "depends_on": [3]},
            ],
            "knowledge_qa": [
                {"step_id": 1, "tool_name": "query_memory", "params_template": {"query": "{{slots.query}}"}, "depends_on": []},
                {"step_id": 2, "tool_name": "search_knowledge", "params_template": {"query": "{{slots.query}}"}, "depends_on": [1]},
                {"step_id": 3, "tool_name": "generate_answer", "params_template": {"query": "{{slots.query}}", "retrieved": "{{steps.2.result.chunks}}"}, "depends_on": [2]},
            ],
            "simple_reminder": [
                {"step_id": 1, "tool_name": "create_reminder", "params_template": {"time": "{{slots.datetime}}", "content": "{{slots.content}}"}, "depends_on": []},
            ],
            "query_employee": [
                {"step_id": 1, "tool_name": "query_employee", "params_template": {"name": "{{slots.name}}", "department": "{{slots.department}}"}, "depends_on": []},
            ],
        }
        
        # ========== 提示词模板 ==========
        self._prompt_templates = {
            "intent_recognition": PromptTemplate(
                name="intent_recognition",
                version="1.0",
                content="""分析用户输入的意图。

用户输入: {{user_input}}

候选意图:
{{candidate_intents}}

请输出JSON格式:
{{
    "intent_id": "意图ID",
    "complexity": "simple或complex",
    "confidence": 0.0到1.0之间的置信度,
    "is_multi_intent": true或false,
    "secondary_intents": ["次要意图ID"],
    "reasoning": "决策理由"
}}""",
                variables=["user_input", "candidate_intents"]
            ),
            
            "skeleton_planning": PromptTemplate(
                name="skeleton_planning",
                version="1.0",
                content="""为以下任务生成执行骨架（仅步骤序列，不包含参数）。

任务: {{user_input}}
意图: {{intent_id}}
预提取槽位: {{extracted_slots}}

可用工具:
{{available_tools}}

相似任务骨架示例:
{{few_shot_examples}}

要求:
1. 仅输出步骤名列表，如 ["query_user", "send_notification"]
2. 步骤按执行顺序排列
3. 只使用可用工具列表中的工具
4. 步骤数量2-5个

输出JSON格式:
{{"skeleton": ["step1", "step2", ...]}}""",
                variables=["user_input", "intent_id", "extracted_slots", "available_tools", "few_shot_examples"]
            ),
            
            "param_extraction": PromptTemplate(
                name="param_extraction",
                version="1.0",
                content="""提取工具参数。

工具: {{tool_name}}
参数schema: {{tool_schema}}

预提取槽位: {{extracted_slots}}
上下文: {{context}}

请提取调用该工具所需的参数，输出JSON格式。""",
                variables=["tool_name", "tool_schema", "extracted_slots", "context"]
            ),
            
            "param_fix": PromptTemplate(
                name="param_fix",
                version="1.0",
                content="""修复工具参数。

工具: {{tool_name}}
原始参数: {{original_params}}
错误信息: {{error_message}}

请修复参数，输出JSON格式。""",
                variables=["tool_name", "original_params", "error_message"]
            ),
            
            "reflection": PromptTemplate(
                name="reflection",
                version="1.0",
                content="""分析任务失败原因并生成反思。

任务: {{task}}
失败步骤: {{failed_step}}
错误信息: {{error}}
重试次数: {{retry_count}}

请输出JSON格式:
{{
    "summary": "失败摘要",
    "root_cause": "根本原因",
    "suggested_fix": "建议修复策略",
    "severity": "transient/parameter_error/permanent_tool_error/planning_flaw"
}}""",
                variables=["task", "failed_step", "error", "retry_count"]
            ),
            
            "summarize": PromptTemplate(
                name="summarize",
                version="1.0",
                content="""总结任务执行结果。

任务: {{task}}
执行步骤: {{steps}}
结果: {{results}}

请生成结构化Markdown报告，包括:
1. 任务概述
2. 执行明细
3. 关键指标
4. 建议（如有）""",
                variables=["task", "steps", "results"]
            ),
        }
        
        # ========== 工具使用模式 ==========
        self._tool_patterns = {
            "send_notification": {
                "backup_tools": ["send_wechat", "send_email"],
                "rate_limit": {"wechat": 30, "email": 60},
                "retry_strategy": "exponential_backoff",
                "max_retries": 3,
            },
            "query_user": {
                "backup_tools": [],
                "cache_ttl": 300,
                "retry_strategy": "immediate",
                "max_retries": 2,
            },
            "search_knowledge": {
                "backup_tools": ["bm25_search"],
                "hybrid_search": True,
                "rrf_k": 60,
            },
        }
    
    def get_plan_template(self, intent_id: str) -> Optional[List[Dict[str, Any]]]:
        """获取计划模板"""
        return self._plan_templates.get(intent_id)
    
    def get_prompt_template(self, name: str) -> Optional[PromptTemplate]:
        """获取提示词模板"""
        return self._prompt_templates.get(name)
    
    def render_prompt(self, name: str, **kwargs) -> str:
        """渲染提示词"""
        template = self.get_prompt_template(name)
        if not template:
            raise ValueError(f"Prompt template not found: {name}")
        return template.render(**kwargs)
    
    def get_tool_pattern(self, tool_name: str) -> Dict[str, Any]:
        """获取工具使用模式"""
        return self._tool_patterns.get(tool_name, {})
    
    def register_plan_template(self, intent_id: str, template: List[Dict[str, Any]]):
        """注册计划模板"""
        self._plan_templates[intent_id] = template
    
    def register_prompt_template(self, template: PromptTemplate):
        """注册提示词模板"""
        self._prompt_templates[template.name] = template
    
    def list_intents(self) -> List[str]:
        """列出所有意图"""
        return list(self._plan_templates.keys())
    
    def get_template_version(self, intent_id: str) -> str:
        """获取模板版本（哈希）"""
        template = self._plan_templates.get(intent_id)
        if not template:
            return ""
        template_str = json.dumps(template, sort_keys=True)
        return hashlib.sha256(template_str.encode()).hexdigest()[:16]


# 全局单例
_procedural_memory: Optional[ProceduralMemory] = None


def get_procedural_memory() -> ProceduralMemory:
    """获取程序记忆单例"""
    global _procedural_memory
    if _procedural_memory is None:
        _procedural_memory = ProceduralMemory()
    return _procedural_memory
