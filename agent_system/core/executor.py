"""
执行模块 - ReAct执行器（骨架 + 动态参数提取）
"""
from typing import List, Dict, Any, Optional, Callable
from pydantic import BaseModel, Field
import asyncio
import time
import re

from agent_system.utils.llm_client import get_llm_client, BaseLLMClient
from agent_system.core.planner import Plan, PlanStep, DependencyAnalyzer
from agent_system.config.settings import get_settings


class StepResult(BaseModel):
    """步骤执行结果"""
    step_id: int
    tool_name: str
    status: str  # succeeded / failed / skipped / pending
    params: Dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    error: Optional[str] = None
    retry_count: int = 0
    latency_ms: int = 0


class ExecutionContext(BaseModel):
    """执行上下文（工作记忆）"""
    trace_id: str
    current_step_idx: int = 0
    step_results: List[StepResult] = Field(default_factory=list)
    context: Dict[str, Any] = Field(default_factory=dict)  # 共享黑板
    status: str = "executing"  # executing / completed / failed
    error: Optional[str] = None
    replan_count: int = 0


class ToolError(Exception):
    """工具错误"""
    pass


class TransientError(ToolError):
    """瞬时错误（可重试）"""
    pass


class ParameterError(ToolError):
    """参数错误"""
    pass


class ToolUnavailableError(ToolError):
    """工具不可用"""
    pass


class ReActExecutor:
    """ReAct执行器"""
    
    MAX_RETRIES = 3
    
    def __init__(self, 
                 tools: Dict[str, Callable],
                 llm_client: Optional[BaseLLMClient] = None):
        self.tools = tools
        self.llm = llm_client or get_llm_client()
        self.dependency_analyzer = DependencyAnalyzer()
        self.MAX_RETRIES = get_settings().MAX_STEP_RETRIES
        self.tool_timeout = get_settings().TOOL_TIMEOUT_SECONDS
    
    async def execute(self, 
                      plan: Plan,
                      trace_id: str,
                      session_context: Optional[Dict[str, Any]] = None, checkpoint=None, targets=None, on_event=None) -> ExecutionContext:
        """
        执行计划
        ReAct模式：逐步执行，动态提取参数
        """
        if checkpoint is not None:
            from agent_system.runtime.durable_executor import execute_durable
            return await execute_durable(self,plan,trace_id,session_context,checkpoint,targets,on_event)
        # 初始化执行上下文
        exec_ctx = ExecutionContext(
            trace_id=trace_id,
            context={
                "original_task": plan.extracted_slots.get("original_input", ""),
                "extracted_slots": plan.extracted_slots,
                "session_context": session_context or {},
            }
        )
        
        ids = [step.step_id for step in plan.steps]
        if not ids or len(ids) != len(set(ids)):
            exec_ctx.status = "failed"
            exec_ctx.error = "计划为空或步骤ID重复"
            return exec_ctx
        if any(len(s.depends_on) != len(set(s.depends_on)) for s in plan.steps):
            exec_ctx.status = "failed"
            exec_ctx.error = "计划包含重复依赖"
            return exec_ctx
        # 分析依赖，获取执行批次
        dag_info = self.dependency_analyzer.analyze(plan)
        batches = dag_info["batches"]
        
        # DAG验证
        if dag_info.get("has_cycle"):
            exec_ctx.status = "failed"
            exec_ctx.error = "计划存在循环依赖，无法执行"
            return exec_ctx
        
        if dag_info.get("orphan_steps"):
            exec_ctx.status = "failed"
            exec_ctx.error = f"计划存在孤立步骤: {dag_info['orphan_steps']}"
            return exec_ctx
        
        # 按批次执行
        for batch_idx, batch in enumerate(batches):
            if exec_ctx.status == "failed":
                break
            
            # 批次内步骤
            batch_steps = [s for s in plan.steps if s.step_id in batch]
            
            # 检查依赖是否已满足（上游失败则跳过）
            ready_steps = []
            for step in batch_steps:
                if not self._check_dependencies_satisfied(step, exec_ctx):
                    self._handle_step_skipped(step, exec_ctx, "上游依赖失败")
                else:
                    ready_steps.append(step)
            batch_steps = ready_steps
            
            if len(batch_steps) > 1:
                # 并行执行
                results = await asyncio.gather(
                    *[self._execute_step_with_fallback(step, plan, exec_ctx) for step in batch_steps],
                    return_exceptions=True
                )
                
                for step, result in zip(batch_steps, results):
                    if isinstance(result, Exception):
                        # 异常转失败
                        self._handle_step_failure(step, exec_ctx, str(result))
                    else:
                        # 正常结果（succeeded/failed/skipped）
                        self._record_step_result(step, exec_ctx, result)
            else:
                # 串行执行
                for step in batch_steps:
                    result = await self._execute_step_with_fallback(step, plan, exec_ctx)
                    self._record_step_result(step, exec_ctx, result)

            exec_ctx.current_step_idx = batch_idx + 1
        
        # 最终状态判定
        if exec_ctx.status != "failed":
            # 检查是否有失败的关键步骤
            critical_failed = any(
                r.status in ("failed", "skipped") and self._is_critical_step(plan, r.step_id)
                for r in exec_ctx.step_results
            )
            if critical_failed:
                exec_ctx.status = "failed"
                exec_ctx.error = "关键步骤失败或因上游失败被跳过；其他独立分支已继续执行"
            else:
                exec_ctx.status = "completed"
        
        return exec_ctx
    
    def _check_dependencies_satisfied(self, step: PlanStep, exec_ctx: ExecutionContext) -> bool:
        """检查依赖是否已满足（所有依赖步骤都成功）"""
        for dep_id in step.depends_on:
            dep_status = exec_ctx.context.get(f"step_{dep_id}_status")
            if dep_status != "succeeded":
                return False
        return True
    
    def _is_critical_step(self, plan: Plan, step_id: int) -> bool:
        """判断是否关键步骤"""
        for step in plan.steps:
            if step.step_id == step_id:
                return step.is_critical
        return True  # 默认关键
    
    async def _execute_step_with_fallback(self, 
                                          step: PlanStep, 
                                          plan: Plan,
                                          exec_ctx: ExecutionContext) -> StepResult:
        """
        执行单步（带回退处理）
        统一处理：抛异常和返回失败结果
        """
        last_error = None
        # 修复和工具切换只修改当前尝试，不能污染原计划或其他任务。
        step = step.model_copy(deep=True)
        
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                result = await self._execute_step(step, plan, exec_ctx)
                
                # 成功，直接返回
                if result.status == "succeeded":
                    result.retry_count = attempt
                    return result
                
                # 失败，记录错误，进入回退判断
                last_error = result.error
                
                # 判断回退策略
                strategy = await self._decide_fallback_strategy(step, result, attempt)
                
                if strategy == "retry":
                    # 重试：更新参数（如果有修复），继续循环
                    if attempt < self.MAX_RETRIES:
                        await asyncio.sleep(2 ** attempt)  # 指数退避
                        continue
                    else:
                        # 重试用尽
                        return StepResult(
                            step_id=step.step_id,
                            tool_name=step.tool_name,
                            status="failed",
                            params=result.params,
                            error=f"重试{self.MAX_RETRIES}次后仍失败: {last_error}",
                            retry_count=attempt
                        )
                
                elif strategy == "fix_params":
                    if attempt >= self.MAX_RETRIES:
                        result.retry_count = attempt
                        return result
                    # 参数修复：LLM修复后重试
                    fixed_params = await self._fix_params_with_llm(step, exec_ctx, result)
                    step.params_template.update(fixed_params)
                    continue
                
                elif strategy == "switch_tool":
                    # 工具降级：切换备用工具
                    backup_tool = self._get_backup_tool(step.tool_name)
                    if backup_tool and attempt < self.MAX_RETRIES:
                        step.tool_name = backup_tool
                        continue
                    else:
                        return StepResult(
                            step_id=step.step_id,
                            tool_name=step.tool_name,
                            status="failed",
                            params=result.params,
                            error=f"工具不可用且无备用: {last_error}",
                            retry_count=attempt
                        )
                
                elif strategy == "skip":
                    # 跳过
                    return StepResult(
                        step_id=step.step_id,
                        tool_name=step.tool_name,
                        status="skipped",
                        params=result.params,
                        error=f"跳过: {last_error}",
                        retry_count=attempt
                    )
                
                else:  # abort
                    # 终止
                    return StepResult(
                        step_id=step.step_id,
                        tool_name=step.tool_name,
                        status="failed",
                        params=result.params,
                        error=f"关键步骤失败: {last_error}",
                        retry_count=attempt
                    )
            
            except TransientError as e:
                # 瞬时错误，重试
                last_error = str(e)
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(2 ** attempt)
                    continue
                else:
                    return StepResult(
                        step_id=step.step_id,
                        tool_name=step.tool_name,
                        status="failed",
                        error=f"瞬时错误，重试{self.MAX_RETRIES}次后仍失败: {last_error}",
                        retry_count=attempt
                    )
            
            except ParameterError as e:
                # 参数错误，尝试修复
                last_error = str(e)
                if attempt == 0:
                    # 第一次失败，尝试LLM修复
                    try:
                        fixed = await self._fix_params_with_llm(step, exec_ctx, 
                            StepResult(step_id=step.step_id, tool_name=step.tool_name, 
                                      status="failed", error=last_error))
                        step.params_template.update(fixed)
                        continue
                    except Exception:
                        pass
                
                return StepResult(
                    step_id=step.step_id,
                    tool_name=step.tool_name,
                    status="failed",
                    error=f"参数错误: {last_error}",
                    retry_count=attempt
                )
            
            except ToolUnavailableError as e:
                # 工具不可用，尝试降级
                last_error = str(e)
                backup_tool = self._get_backup_tool(step.tool_name)
                if backup_tool and attempt < self.MAX_RETRIES:
                    step.tool_name = backup_tool
                    continue
                
                return StepResult(
                    step_id=step.step_id,
                    tool_name=step.tool_name,
                    status="failed",
                    error=f"工具不可用: {last_error}",
                    retry_count=attempt
                )
            
            except Exception as e:
                # 其他异常
                last_error = str(e)
                return StepResult(
                    step_id=step.step_id,
                    tool_name=step.tool_name,
                    status="failed",
                    error=f"执行异常: {last_error}",
                    retry_count=attempt
                )
        
        # 循环结束（不应到达）
        return StepResult(
            step_id=step.step_id,
            tool_name=step.tool_name,
            status="failed",
            error=f"未知错误: {last_error}",
            retry_count=self.MAX_RETRIES
        )
    
    async def _execute_step(self, 
                           step: PlanStep, 
                           plan: Plan,
                           exec_ctx: ExecutionContext, prepared_params=None) -> StepResult:
        """
        执行单步（ReAct核心）
        动态提取参数 → 执行 → 观察
        """
        start_time = time.time()
        
        # 1. 动态提取参数（ReAct关键）
        params = prepared_params if prepared_params is not None else await self._extract_params(step, plan, exec_ctx)
        
        # 2. 查找工具
        tool = self.tools.get(step.tool_name)
        if not tool:
            raise ToolError(f"工具不存在: {step.tool_name}")
        
        # 3. 执行工具
        try:
            if asyncio.iscoroutinefunction(tool):
                result = await asyncio.wait_for(tool(**params), timeout=self.tool_timeout)
            else:
                result = await asyncio.wait_for(asyncio.to_thread(tool, **params), timeout=self.tool_timeout)
            
            latency_ms = int((time.time() - start_time) * 1000)
            
            return StepResult(
                step_id=step.step_id,
                tool_name=step.tool_name,
                status="succeeded",
                params=params,
                result=result,
                latency_ms=latency_ms
            )
        
        except TransientError as e:
            raise
        except ParameterError as e:
            raise
        except ToolUnavailableError as e:
            raise
        except Exception as e:
            latency_ms = int((time.time() - start_time) * 1000)
            
            return StepResult(
                step_id=step.step_id,
                tool_name=step.tool_name,
                status="failed",
                params=params,
                error="tool timeout" if isinstance(e, TimeoutError) else str(e),
                latency_ms=latency_ms
            )
    
    async def _decide_fallback_strategy(self, 
                                        step: PlanStep,
                                        failed_result: StepResult,
                                        attempt: int) -> str:
        """判断回退策略"""
        error = failed_result.error or ""
        
        # Level 1: 重试（瞬时错误，未超次数）
        if attempt < self.MAX_RETRIES and self._is_transient_error(error):
            return "retry"
        
        # Level 2: 参数修复
        if self._is_parameter_error(error):
            return "fix_params"
        
        # Level 3: 工具降级
        if self._is_tool_unavailable(error):
            return "switch_tool"
        
        # Level 4: 跳过（非关键步骤）
        if not step.is_critical:
            return "skip"
        
        # 终止
        return "abort"
    
    def _is_transient_error(self, error: str) -> bool:
        """判断瞬时错误"""
        transient_keywords = ["timeout", "rate limit", "connection", "network", "temporary"]
        return any(kw in error.lower() for kw in transient_keywords)
    
    def _is_parameter_error(self, error: str) -> bool:
        """判断参数错误"""
        param_keywords = ["invalid", "format", "range", "missing", "required", "parameter"]
        return any(kw in error.lower() for kw in param_keywords)
    
    def _is_tool_unavailable(self, error: str) -> bool:
        """判断工具不可用"""
        unavailable_keywords = ["not found", "unavailable"]
        return any(kw in error.lower() for kw in unavailable_keywords)
    
    def _get_backup_tool(self, tool_name: str) -> Optional[str]:
        """获取备用工具"""
        return None  # No alternate bundled tools.

    async def _fix_params_with_llm(self, 
                                   step: PlanStep,
                                   exec_ctx: ExecutionContext,
                                   failed_result: StepResult) -> Dict[str, Any]:
        """LLM修复参数"""
        prompt = f"""修复工具参数。

工具: {step.tool_name}
原始参数: {failed_result.params}
错误信息: {failed_result.error}

请修复参数，输出JSON格式。"""
        
        schema = {
            "type": "object",
            "properties": {k: {"type": "string"} for k in failed_result.params.keys()},
        }
        
        try:
            result = await self.llm.extract_structured(prompt, schema)
            return result
        except Exception:
            return failed_result.params
    
    async def _extract_params(self, 
                             step: PlanStep, 
                             plan: Plan,
                             exec_ctx: ExecutionContext) -> Dict[str, Any]:
        """
        动态提取参数（ReAct核心）
        结合：参数模板占位符 + LLM动态提取
        """
        params = {}
        
        # Step 1: 解析参数模板占位符
        if step.params_template:
            params = self._render_template(step.params_template, exec_ctx.context, plan.extracted_slots)
        
        # Step 2: 如果模板为空或需要补充，使用LLM提取
        if not params:
            params = await self._llm_extract_params(step, plan, exec_ctx)
        
        return params
    
    def _render_template(self, 
                        template: Dict[str, Any], 
                        context: Dict[str, Any],
                        slots: Dict[str, Any]) -> Dict[str, Any]:
        """
        渲染参数模板
        支持占位符: {{slots.xxx}} 和 {{steps.N.result.xxx}}
        
        关键设计：
        - 完整占位符（如 {{steps.1.result.user_ids}}）返回原对象，保持类型
        - 混合文本（如 "用户{{steps.1.result.name}}"）才做字符串替换
        - 未解析的占位符必须报错，不允许静默通过
        """
        result = {}
        
        for key, value in template.items():
            if isinstance(value, str):
                # 检查是否为完整占位符（整个值就是占位符）
                full_match = re.fullmatch(r'\{\{([^{}]+)\}\}', value.strip())
                if full_match:
                    # 完整占位符：返回原对象，保持类型
                    placeholder = full_match.group(1)
                    resolved = self._resolve_placeholder(placeholder, context, slots)
                    if resolved is None:
                        raise ValueError(f"无法解析占位符: {value}")
                    result[key] = resolved
                else:
                    # 混合文本：字符串替换
                    resolved_value = value
                    
                    # 替换 {{slots.xxx}}
                    slots_matches = re.findall(r'\{\{slots\.(\w+)\}\}', value)
                    for match in slots_matches:
                        slot_value = slots.get(match)
                        if slot_value is None:
                            raise ValueError(f"槽位不存在: slots.{match}")
                        resolved_value = resolved_value.replace(f"{{{{slots.{match}}}}}", str(slot_value))
                    
                    # 替换 {{steps.N.result.xxx}}
                    steps_matches = re.findall(r'\{\{steps\.(\d+)\.result\.(\w+)\}\}', value)
                    for step_id_str, field in steps_matches:
                        step_id = int(step_id_str)
                        step_key = f"step_{step_id}_result"
                        step_result = context.get(step_key)
                        if step_result is None:
                            raise ValueError(f"步骤结果不存在: steps.{step_id}.result")
                        field_value = step_result.get(field)
                        if field_value is None:
                            raise ValueError(f"步骤结果字段不存在: steps.{step_id}.result.{field}")
                        resolved_value = resolved_value.replace(f"{{{{steps.{step_id}.result.{field}}}}}", str(field_value))
                    
                    # 替换 {{steps.N.result}}（整个结果）
                    whole_matches = re.findall(r'\{\{steps\.(\d+)\.result\}\}', value)
                    for step_id_str in whole_matches:
                        step_id = int(step_id_str)
                        step_key = f"step_{step_id}_result"
                        step_result = context.get(step_key)
                        if step_result is None:
                            raise ValueError(f"步骤结果不存在: steps.{step_id}.result")
                        resolved_value = resolved_value.replace(f"{{{{steps.{step_id}.result}}}}", str(step_result))
                    
                    # 检查是否还有未解析的占位符
                    remaining = re.findall(r'\{\{[^}]+\}\}', resolved_value)
                    if remaining:
                        raise ValueError(f"存在未解析的占位符: {remaining} in {value}")
                    
                    result[key] = resolved_value
            
            elif isinstance(value, dict):
                result[key] = self._render_template(value, context, slots)
            elif isinstance(value, list):
                result[key] = [
                    self._render_template({"item": item}, context, slots)["item"] 
                    for item in value
                ]
            else:
                result[key] = value
        
        return result
    
    def _resolve_placeholder(self, placeholder: str, context: Dict[str, Any], slots: Dict[str, Any]) -> Any:
        """
        解析占位符，返回原对象（保持类型）
        支持: slots.xxx, steps.N.result, steps.N.result.xxx
        """
        parts = placeholder.split('.')
        
        if parts[0] == 'slots':
            # slots.xxx
            if len(parts) == 2:
                return slots.get(parts[1])
        
        elif parts[0] == 'steps':
            # steps.N.result 或 steps.N.result.xxx
            if len(parts) >= 3:
                step_id = int(parts[1])
                step_key = f"step_{step_id}_result"
                step_result = context.get(step_key)
                
                if step_result is None:
                    return None
                
                if len(parts) == 3 and parts[2] == 'result':
                    # steps.N.result
                    return step_result
                
                elif len(parts) == 4 and parts[2] == 'result':
                    # steps.N.result.xxx
                    return step_result.get(parts[3])
        
        return None
    
    async def _llm_extract_params(self, 
                                 step: PlanStep, 
                                 plan: Plan,
                                 exec_ctx: ExecutionContext) -> Dict[str, Any]:
        """LLM动态提取参数"""
        
        tool = self.tools.get(step.tool_name)
        schema = getattr(tool, "input_schema", {"type": "object", "properties": {}})
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        
        prompt = f"""提取工具参数。

工具: {step.tool_name}
参数schema: {schema}

预提取槽位: {plan.extracted_slots}
上下文: {exec_ctx.context}

请提取调用该工具所需的参数，输出JSON格式。"""
        
        try:
            result = await self.llm.extract_structured(prompt, schema)
            return result
        except Exception:
            # LLM失败，返回空参数
            return {}
    
    def _record_step_result(self, step: PlanStep, exec_ctx: ExecutionContext, result: StepResult):
        """记录步骤结果"""
        exec_ctx.step_results.append(result)
        
        # 写入工作记忆（共享黑板）
        exec_ctx.context[f"step_{step.step_id}_result"] = result.result
        exec_ctx.context[f"step_{step.step_id}_status"] = result.status
    
    def _handle_step_failure(self, step: PlanStep, exec_ctx: ExecutionContext, error: str):
        """处理步骤失败"""
        result = StepResult(
            step_id=step.step_id,
            tool_name=step.tool_name,
            status="failed",
            error=error
        )
        exec_ctx.step_results.append(result)
        exec_ctx.context[f"step_{step.step_id}_status"] = "failed"
    
    def _handle_step_skipped(self, step: PlanStep, exec_ctx: ExecutionContext, reason: str):
        """处理步骤跳过"""
        result = StepResult(
            step_id=step.step_id,
            tool_name=step.tool_name,
            status="skipped",
            error=reason
        )
        exec_ctx.step_results.append(result)
        exec_ctx.context[f"step_{step.step_id}_status"] = "skipped"


# ==================== 便捷函数 ====================

async def execute_plan(plan: Plan, 
                      trace_id: str,
                      tools: Dict[str, Callable],
                      session_context: Optional[Dict[str, Any]] = None, checkpoint=None) -> ExecutionContext:
    """执行计划便捷函数"""
    executor = ReActExecutor(tools)
    return await executor.execute(plan, trace_id, session_context, checkpoint=checkpoint)
