"""
Agent 核心 - ReAct-with-Plan 执行流程
"""
from typing import Dict, Any, List, Optional, TypedDict, Annotated
import operator
import uuid
import time
import logging
from agent_system.db.session import db_session

logger = logging.getLogger(__name__)

from agent_system.core.intent_router import IntentRouter, IntentDecision
from agent_system.core.planner import SkeletonPlanner, Plan, PlanningError
from agent_system.core.executor import ReActExecutor, ExecutionContext
from agent_system.memory.working_memory import WorkingMemory, create_working_memory
from agent_system.memory.episodic_memory import EpisodicMemory
from agent_system.memory.procedural_memory import get_procedural_memory
from agent_system.tools.registry import get_tool_registry, ToolRegistry


class AgentState(TypedDict):
    """Agent状态"""
    # 输入
    user_input: str
    user_id: str
    session_history: List[Dict[str, str]]
    
    # 意图识别
    intent_decision: Optional[Dict[str, Any]]
    
    # 规划
    plan: Optional[Dict[str, Any]]
    
    # 执行
    execution_context: Optional[Dict[str, Any]]
    trace_id: str
    
    # 输出
    final_answer: Optional[str]
    error: Optional[str]
    
    # 元数据
    step_results: Annotated[List[Dict[str, Any]], operator.add]
    latency_ms: int


class AgentCore:
    """Agent核心"""
    
    def __init__(self, 
                 tool_registry: Optional[ToolRegistry] = None):
        self.tool_registry = tool_registry or get_tool_registry()
        self.intent_router = IntentRouter()
        self.planner = SkeletonPlanner(available_tools=self.tool_registry.list_tools())
        self.executor = ReActExecutor(
            tools={name: self._create_tool_wrapper(name) for name in self.tool_registry.list_tools()}
        )
        self.episodic_memory = EpisodicMemory()
        self.procedural_memory = get_procedural_memory()
    
    def _create_tool_wrapper(self, tool_name: str):
        """创建工具包装器（适配Executor接口）"""
        tool = self.tool_registry.get(tool_name)
        
        async def wrapper(**kwargs):
            result = await tool.run(**kwargs)
            if not result.success:
                raise Exception(result.error)
            return result.data
        wrapper.input_schema = tool.input_model.model_json_schema()
        return wrapper
    
    async def run(self, 
                  user_input: str,
                  user_id: str = "default_user",
                  session_history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
        """
        运行Agent（主入口）
        """
        start_time = time.time()
        trace_id = f"task_{uuid.uuid4().hex[:12]}"
        
        # 初始化状态
        state: AgentState = {
            "user_input": user_input,
            "user_id": user_id,
            "session_history": session_history or [],
            "intent_decision": None,
            "plan": None,
            "execution_context": None,
            "trace_id": trace_id,
            "final_answer": None,
            "error": None,
            "step_results": [],
            "latency_ms": 0,
        }
        
        try:
            # Step 1: 意图识别
            state = await self._intent_node(state)
            
            # Step 2: 规划
            state = await self._plan_node(state)
            
            # Step 3: 执行
            state = await self._execute_node(state)
            
            # Step 4: 总结
            state = await self._summarize_node(state)
            
            # Step 5: 记忆存储
            state["latency_ms"] = int((time.time() - start_time) * 1000)
            try:
                await self._store_memory(state)
            except Exception:
                logger.warning("Episode storage unavailable for %s", trace_id)
            
        except PlanningError as e:
            # 规划失败：安全失败，不执行任何工具
            state["error"] = f"规划失败: {str(e)}"
            state["final_answer"] = f"无法完成任务，请尝试更明确的描述: {str(e)}"
        except Exception as e:
            state["error"] = str(e)
            state["final_answer"] = f"任务执行失败: {str(e)}"
        
        state["latency_ms"] = int((time.time() - start_time) * 1000)
        
        return {
            "trace_id": trace_id,
            "success": state["error"] is None,
            "answer": state["final_answer"],
            "intent": state["intent_decision"],
            "steps": state["step_results"],
            "latency_ms": state["latency_ms"],
            "error": state["error"],
        }
    
    async def _intent_node(self, state: AgentState) -> AgentState:
        """意图识别节点"""
        with db_session() as db:
            self.intent_router.load_intents_from_db(db)
        decision = await self.intent_router.route(
            user_input=state["user_input"],
            session_history=state["session_history"]
        )
        
        state["intent_decision"] = decision.model_dump()
        
        return state
    
    async def _plan_node(self, state: AgentState) -> AgentState:
        """规划节点"""
        intent_decision = state["intent_decision"]
        
        # 检索相似骨架（情景记忆）
        similar_skeletons = None
        if intent_decision.get("intent_id"):
            try:
                recommended = await self.episodic_memory.get_recommended_skeleton(
                    user_id=state["user_id"],
                    intent_id=intent_decision["intent_id"],
                    extracted_slots=intent_decision.get("extracted_slots", {})
                )
            except Exception:
                logger.warning("Episode retrieval unavailable for %s", state["trace_id"])
                recommended = None
            if recommended:
                similar_skeletons = [{"skeleton_steps": recommended}]
        
        # 生成计划
        plan = await self.planner.plan(
            intent_id=intent_decision["intent_id"],
            user_input=state["user_input"],
            extracted_slots=intent_decision.get("extracted_slots", {}),
            similar_skeletons=similar_skeletons
        )
        
        state["plan"] = plan.model_dump()
        
        return state
    
    async def _execute_node(self, state: AgentState) -> AgentState:
        """执行节点"""
        plan_data = state["plan"]
        intent_decision = state["intent_decision"]
        
        # 重建Plan对象
        plan = Plan.model_validate(plan_data)
        
        # 执行
        exec_ctx = await self.executor.execute(
            plan=plan,
            trace_id=state["trace_id"],
            session_context={"user_id": state["user_id"]}
        )
        
        state["execution_context"] = exec_ctx.model_dump()
        state["step_results"] = [r.model_dump() for r in exec_ctx.step_results]
        
        if exec_ctx.status == "failed":
            state["error"] = exec_ctx.error
        
        return state
    
    async def _summarize_node(self, state: AgentState) -> AgentState:
        """总结节点"""
        exec_ctx = state["execution_context"]
        intent_decision = state["intent_decision"]
        
        # 生成总结
        steps = state["step_results"]
        successful = sum(1 for s in steps if s.get("status") == "succeeded")
        failed = sum(1 for s in steps if s.get("status") == "failed")
        
        if state["error"]:
            state["final_answer"] = f"任务执行失败: {state['error']}"
        else:
            state["final_answer"] = (
                f"任务执行完成。\n"
                f"意图: {intent_decision.get('intent_id', 'unknown')}\n"
                f"总步骤: {len(steps)}，成功: {successful}，失败: {failed}\n"
                f"执行结果: {exec_ctx.get('context', {})}"
            )
        
        return state
    
    async def _store_memory(self, state: AgentState):
        """存储记忆"""
        intent_decision = state["intent_decision"]
        plan = state["plan"]
        exec_ctx = state["execution_context"]
        
        if not all([intent_decision, plan, exec_ctx]):
            return
        
        # 提取骨架
        steps = plan.get("steps", [])
        skeleton = []
        for s in steps:
            if isinstance(s, dict):
                skeleton.append(s.get("tool_name", "unknown"))
            else:
                skeleton.append(str(s))
        
        # 判断成功
        success = state["error"] is None
        
        # 存储情景记忆
        await self.episodic_memory.store_episode(
            user_id=state["user_id"],
            intent_id=intent_decision["intent_id"],
            skeleton=skeleton,
            extracted_slots=intent_decision.get("extracted_slots", {}),
            success=success,
            duration_ms=state["latency_ms"],
            failure_info={"reason": state["error"]} if state["error"] else None
        )


# ==================== 便捷函数 ====================

async def run_agent(user_input: str,
                   user_id: str = "default_user",
                   session_history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """运行Agent便捷函数"""
    agent = AgentCore()
    return await agent.run(user_input, user_id, session_history)
