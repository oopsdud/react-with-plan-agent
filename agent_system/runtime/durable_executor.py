"""Ready-queue DAG execution with per-attempt durable writes and selective reuse."""
import asyncio
from sqlalchemy.exc import SQLAlchemyError
from agent_system.core.executor import ExecutionContext,StepResult
from agent_system.runtime.checkpoints import digest,validate_graph
from agent_system.runtime.errors import ServiceError,public_error
from agent_system.config.settings import get_settings


async def execute_durable(executor,plan,trace_id,session_context,checkpoint,targets=None,on_event=None):
    validate_graph(plan,set(executor.tools))
    version=await checkpoint.ensure_plan(plan)
    context=ExecutionContext(trace_id=trace_id,context={'original_task':plan.extracted_slots.get('original_input',''),
        'extracted_slots':plan.extracted_slots,'session_context':session_context or {}})
    mapping={s.step_id:s for s in plan.steps}
    needed=set(mapping) if targets is None else set(targets)
    frontier=list(needed)
    while frontier:
        number=frontier.pop()
        for parent in mapping[number].depends_on:
            if parent not in needed:needed.add(parent);frontier.append(parent)
    cached=await checkpoint.nodes(version)
    pending=set(needed)
    for number in sorted(needed):
        value=cached.get(f'step:{number}')
        if value is not None:
            result=StepResult.model_validate(value)
            if result.status=='succeeded':
                executor._record_step_result(mapping[number],context,result);pending.remove(number)
                if on_event:await on_event({'type':'step','step':result.model_dump(),'reused':True,'plan_version':version})
    running={}

    async def execute_step(original):
        step=original.model_copy(deep=True)
        safe=bool(getattr(executor.tools[step.tool_name],'replay_safe',False))
        for local_attempt in range(executor.MAX_RETRIES+1):
            attempt=None;params=None;dispatched=False
            try:
                material={'definition':step.model_dump(),'slots':plan.extracted_slots,
                    'upstream':{str(i):context.context.get(f'step_{i}_result') for i in step.depends_on}}
                attempt=await checkpoint.begin(version,f'step:{step.step_id}',step.tool_name,
                    {'template':step.params_template},digest(material),safe)
                params=await executor._extract_params(step,plan,context)
                await checkpoint.parameters(attempt,params)
                dispatched=True
                result=await executor._execute_step(step,plan,context,prepared_params=params)
                result.retry_count=local_attempt
                info=None if result.status=='succeeded' else {'code':'TOOL_FAILED','message':result.error or '工具失败'}
                await checkpoint.end(attempt,result.status if safe or result.status=='succeeded' else 'unknown',result.model_dump(),info,params)
                attempt=None
                if result.status=='succeeded':return result
                if not safe:return result
                strategy=await executor._decide_fallback_strategy(step,result,local_attempt)
                if local_attempt>=executor.MAX_RETRIES:return result
                if strategy=='retry':await asyncio.sleep(2**local_attempt);continue
                if strategy=='fix_params':
                    fixed=await executor._fix_params_with_llm(step,context,result)
                    if fixed==params:return result
                    step.params_template.update(fixed);continue
                return result
            except asyncio.CancelledError:
                if attempt:await checkpoint.end(attempt,'cancelled' if safe or not dispatched else 'unknown',error={'code':'INTERRUPTED','message':'执行被中断'},params=params)
                raise
            except Exception as error:
                if attempt:
                    await checkpoint.end(attempt,'failed' if safe or not dispatched else 'unknown',error=public_error(error),params=params)
                if isinstance(error,SQLAlchemyError) or (isinstance(error,ServiceError) and error.code in ('STALE_EXECUTION','CHECKPOINT_TOO_LARGE','SEARCH_BUDGET_EXHAUSTED')):raise
                return StepResult(step_id=step.step_id,tool_name=step.tool_name,status='failed',params=params or {},error=public_error(error)['message'],retry_count=local_attempt)
        raise RuntimeError('unreachable')

    try:
        while pending or running:
            # Dependency readiness is reconsidered after EACH completed node, not a batch barrier.
            for number in sorted(list(pending)):
                step=mapping[number]
                statuses=[context.context.get(f'step_{i}_status') for i in step.depends_on]
                if any(value is None for value in statuses):continue
                if any(value!='succeeded' for value in statuses):
                    result=StepResult(step_id=number,tool_name=step.tool_name,status='skipped',error='上游依赖失败')
                    await checkpoint.skip(version,f'step:{number}',result.model_dump())
                    executor._record_step_result(step,context,result);pending.remove(number)
                    if on_event:await on_event({'type':'step','step':result.model_dump(),'plan_version':version})
                    continue
                if len(running)>=get_settings().CHECKPOINT_MAX_PARALLEL_STEPS:break
                pending.remove(number)
                running[asyncio.create_task(execute_step(step))]=number
            if not running:
                if pending:raise ServiceError('INVALID_GRAPH','依赖无法继续调度。',409)
                break
            done,_=await asyncio.wait(running,return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                number=running.pop(task)
                result=await task
                executor._record_step_result(mapping[number],context,result)
                if on_event:await on_event({'type':'step','step':result.model_dump(),'plan_version':version})
        context.status='failed' if any(r.status in ('failed','skipped') and mapping[r.step_id].is_critical for r in context.step_results) else 'completed'
        if context.status=='failed':context.error='关键步骤失败或被阻塞；独立分支已继续执行'
        context.step_results.sort(key=lambda r:r.step_id)
        return context
    finally:
        # A cancelled/failed coordinator must not leave sibling coroutines writing later.
        for task in running:task.cancel()
        if running:await asyncio.gather(*running,return_exceptions=True)
