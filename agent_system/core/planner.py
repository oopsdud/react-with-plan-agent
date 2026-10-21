"""
规划模块 - 骨架规划（ReAct-with-Plan）
"""
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field
import uuid

from agent_system.utils.llm_client import get_llm_client, BaseLLMClient
from agent_system.utils.embedding_client import get_embedding_client, BaseEmbeddingClient


class PlanStep(BaseModel):
    """计划步骤（仅骨架，参数占位符）"""
    step_id: int = Field(..., description="步骤序号")
    tool_name: str = Field(..., description="工具名")
    description: str = Field(default="", description="步骤描述")
    params_template: Dict[str, Any] = Field(default_factory=dict, description="参数模板（含占位符）")
    depends_on: List[int] = Field(default_factory=list, description="依赖的步骤ID列表")
    is_critical: bool = Field(default=True, description="失败或被阻塞时影响任务整体结果；独立分支继续执行")


class Plan(BaseModel):
    """计划（骨架）"""
    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    intent_id: str = Field(..., description="意图ID")
    steps: List[PlanStep] = Field(default_factory=list, description="步骤列表")
    extracted_slots: Dict[str, Any] = Field(default_factory=dict, description="预提取槽位")
    
    # 执行元数据
    has_parallel: bool = Field(default=False, description="是否有并行步骤")
    max_depth: int = Field(default=0, description="DAG最大深度")


class PlanningError(Exception):
    """规划错误"""
    pass


class SkeletonPlanner:
    """骨架规划器"""
    
    PLAN_TEMPLATES: Dict[str, List[Dict[str, Any]]] = {
        "web_search": [{"step_id": 1, "tool_name": "web_search", "description": "搜索网页",
                        "params_template": {"query": "{{slots.query}}"}, "depends_on": []}]
    }

    def __init__(self, llm_client: Optional[BaseLLMClient] = None,
                 embedding_client: Optional[BaseEmbeddingClient] = None,
                 available_tools: Optional[List[str]] = None):
        self.llm = llm_client or get_llm_client()
        self.embedding = embedding_client
        if available_tools is None:
            from agent_system.tools.registry import get_tool_registry
            available_tools = get_tool_registry().list_tools()
        self.available_tools = set(available_tools)
    
    async def plan(self, 
                   intent_id: str,
                   user_input: str,
                   extracted_slots: Dict[str, Any],
                   similar_skeletons: Optional[List[Dict]] = None) -> Plan:
        """
        生成计划骨架
        """
        extracted_slots = {**extracted_slots, "original_input": user_input}
        if intent_id == "web_search":
            extracted_slots["query"] = extracted_slots.get("query") or user_input
        # Step 1: 检查模板命中
        if intent_id in self.PLAN_TEMPLATES:
            template = self.PLAN_TEMPLATES[intent_id]
            steps = [PlanStep(**step) for step in template]
            
            # 验证DAG
            self._validate_dag(steps)
            
            return Plan(
                intent_id=intent_id,
                steps=steps,
                extracted_slots=extracted_slots,
                has_parallel=self._check_parallel(steps),
                max_depth=self._calculate_depth(steps)
            )
        
        # Step 2: LLM动态规划（Few-shot引导）
        skeleton = await self._llm_plan(intent_id, user_input, extracted_slots, similar_skeletons)
        
        if not skeleton:
            raise PlanningError(f"无法为任务生成有效计划: {user_input}")
        
        # 转换为PlanStep
        steps = []
        for i, step_name in enumerate(skeleton, 1):
            steps.append(PlanStep(
                step_id=i,
                tool_name=step_name,
                description=f"执行{step_name}",
                params_template={},  # LLM生成骨架时不填参数，执行时动态提取
                depends_on=[i-1] if i > 1 else []  # 默认串行依赖
            ))
        
        # 验证DAG
        self._validate_dag(steps)
        
        return Plan(
            intent_id=intent_id,
            steps=steps,
            extracted_slots=extracted_slots,
            has_parallel=False,
            max_depth=len(steps)
        )
    
    async def _llm_plan(self, intent_id: str, user_input: str, 
                        extracted_slots: Dict[str, Any],
                        similar_skeletons: Optional[List[Dict]] = None) -> List[str]:
        """LLM动态生成骨架"""
        
        # 构建Few-shot示例
        few_shot_text = ""
        if similar_skeletons:
            for sk in similar_skeletons[:3]:  # 最多3个示例
                few_shot_text += f"输入: {sk.get('original_input', '')}\n"
                few_shot_text += f"骨架: {sk.get('skeleton_steps', [])}\n\n"
        
        # 可用工具列表
        available_tools = sorted(self.available_tools)
        
        prompt = f"""为以下任务生成执行骨架（仅步骤序列，不包含参数）。

任务: {user_input}
意图: {intent_id}
预提取槽位: {extracted_slots}

可用工具:
{chr(10).join([f"- {tool}" for tool in available_tools])}

相似任务骨架示例:
{few_shot_text if few_shot_text else "无"}

要求:
1. 仅输出步骤名列表，如 ["web_search"]
2. 步骤按执行顺序排列
3. 只使用可用工具列表中的工具
4. 步骤数量1-5个
5. 如果任务无法完成或超出能力范围，返回空列表 []

输出JSON格式:
{{"skeleton": ["step1", "step2", ...]}}"""
        
        schema = {
            "type": "object",
            "properties": {
                "skeleton": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["skeleton"]
        }
        
        try:
            result = await self.llm.extract_structured(prompt, schema)
            skeleton = result.get("skeleton", [])
            
            # 校验：步骤必须在可用工具中
            if (not isinstance(skeleton, list) or not skeleton or
                    len(skeleton) > 20 or any(not isinstance(s, str) or s not in self.available_tools for s in skeleton)):
                return []
            return skeleton
        except Exception:
            # 兜底：返回空列表，让上层处理为无法规划
            return []
    
    def _validate_dag(self, steps: List[PlanStep]):
        """验证DAG：节点唯一、依赖存在、无环"""
        if not steps:
            return
        
        step_ids = [s.step_id for s in steps]
        
        # 检查节点唯一
        if len(step_ids) != len(set(step_ids)):
            raise PlanningError(f"计划步骤ID重复: {step_ids}")
        
        # 检查依赖存在
        step_id_set = set(step_ids)
        for step in steps:
            if step.tool_name not in self.available_tools:
                raise PlanningError(f"工具未注册: {step.tool_name}")
            if len(step.depends_on) != len(set(step.depends_on)):
                raise PlanningError(f"步骤{step.step_id}包含重复依赖")
            for dep in step.depends_on:
                if dep not in step_id_set:
                    raise PlanningError(f"步骤{step.step_id}依赖不存在的步骤: {dep}")
        
        # 检查无环（Kahn算法）
        in_degree = {s.step_id: 0 for s in steps}
        for step in steps:
            for dep in step.depends_on:
                in_degree[step.step_id] += 1
        
        # 统计入度为0的节点数
        zero_in_degree = sum(1 for deg in in_degree.values() if deg == 0)
        if zero_in_degree == 0 and len(steps) > 0:
            raise PlanningError("计划存在循环依赖，无入度为0的起始节点")
        
        # 拓扑排序验证
        visited = set()
        queue = [sid for sid, deg in in_degree.items() if deg == 0]
        
        while queue:
            node = queue.pop(0)
            visited.add(node)
            
            for step in steps:
                if node in step.depends_on:
                    in_degree[step.step_id] -= 1
                    if in_degree[step.step_id] == 0:
                        queue.append(step.step_id)
        
        if len(visited) != len(steps):
            unvisited = set(step_ids) - visited
            raise PlanningError(f"计划存在循环依赖，未访问节点: {unvisited}")
    
    def _check_parallel(self, steps: List[PlanStep]) -> bool:
        """检查是否有可并行步骤"""
        step_ids = {s.step_id for s in steps}
        for step in steps:
            for dep in step.depends_on:
                if dep not in step_ids:
                    return False
        # 简单判断：如果有步骤依赖相同的前置步骤，则可能并行
        dep_map = {}
        for step in steps:
            key = tuple(sorted(step.depends_on))
            dep_map[key] = dep_map.get(key, 0) + 1
        return any(count > 1 for count in dep_map.values())
    
    def _calculate_depth(self, steps: List[PlanStep]) -> int:
        """计算DAG最大深度"""
        if not steps:
            return 0
        
        # 使用拓扑排序计算深度（避免环导致死循环）
        depth_map = {s.step_id: 1 for s in steps}
        in_degree = {s.step_id: 0 for s in steps}
        
        for step in steps:
            for dep in step.depends_on:
                in_degree[step.step_id] += 1
        
        queue = [(sid, 1) for sid, deg in in_degree.items() if deg == 0]
        max_depth = 1
        
        while queue:
            node, depth = queue.pop(0)
            max_depth = max(max_depth, depth)
            
            for step in steps:
                if node in step.depends_on:
                    in_degree[step.step_id] -= 1
                    if in_degree[step.step_id] == 0:
                        new_depth = depth + 1
                        depth_map[step.step_id] = new_depth
                        queue.append((step.step_id, new_depth))
        
        return max_depth


class DependencyAnalyzer:
    """依赖分析器 - 将骨架转换为DAG"""
    
    @staticmethod
    def analyze(plan: Plan) -> Dict[str, Any]:
        """
        分析计划依赖关系
        返回执行批次（拓扑排序）
        """
        steps = plan.steps
        if not steps:
            return {"batches": [], "dependencies": [], "has_cycle": False, "orphan_steps": []}
        
        # 构建依赖图
        dependencies = []  # [(from_id, to_id), ...]
        in_degree = {s.step_id: 0 for s in steps}
        
        for step in steps:
            for dep in step.depends_on:
                dependencies.append((dep, step.step_id))
                in_degree[step.step_id] += 1
        
        # Kahn算法拓扑排序
        batches = []
        current_batch = [sid for sid, deg in in_degree.items() if deg == 0]
        visited = set()
        
        while current_batch:
            batches.append(current_batch)
            visited.update(current_batch)
            next_batch = []
            
            for sid in current_batch:
                for from_id, to_id in dependencies:
                    if from_id == sid:
                        in_degree[to_id] -= 1
                        if in_degree[to_id] == 0:
                            next_batch.append(to_id)
            
            current_batch = next_batch
        
        # 检查是否有未访问节点（有环或孤立节点）
        all_step_ids = {s.step_id for s in steps}
        unvisited = all_step_ids - visited
        
        return {
            "batches": batches,
            "dependencies": dependencies,
            "has_cycle": len(unvisited) > 0,
            "orphan_steps": list(unvisited)
        }


# ==================== 便捷函数 ====================

async def create_plan(intent_id: str, 
                     user_input: str,
                     extracted_slots: Dict[str, Any],
                     similar_skeletons: Optional[List[Dict]] = None) -> Plan:
    """创建计划便捷函数"""
    planner = SkeletonPlanner()
    return await planner.plan(intent_id, user_input, extracted_slots, similar_skeletons)
