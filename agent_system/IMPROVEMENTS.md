# Agent System 改进记录

## 已修复问题

### P0 - 步骤失败仍可能返回任务成功
**位置**: `core/executor.py`
**修复**:
- 统一处理"抛异常"和"返回失败结果"：`_execute_step_with_fallback` 封装所有异常为 `StepResult`
- 关键步骤失败阻塞其依赖分支，独立分支继续执行；全部分支结束后，关键步骤失败或被阻塞则整个任务标记为 `failed`
- 下游依赖失败时不能继续：`_check_dependencies_satisfied` 检查上游状态，失败则跳过下游
- 最终状态判定：检查是否有失败的关键步骤，有则整体失败

### P0 - 参数模板破坏数据类型
**位置**: `core/executor.py` `_render_template`
**修复**:
- 完整占位符（如 `{{steps.1.result.user_ids}}`）返回原对象，保持类型
- 混合文本（如 `"用户{{steps.1.result.name}}"`）才做字符串替换
- 未解析的占位符必须报错，不允许静默通过

### P0 - 规划失败默认生成发送通知流程
**位置**: `core/planner.py`
**修复**:
- LLM规划失败时返回空列表 `[]`
- `plan()` 方法检测空骨架时抛出 `PlanningError`
- `agent.py` 捕获 `PlanningError`，安全失败，不执行任何工具

### P0 - 用户身份由请求体指定
**位置**: `api/routes.py`
**状态**: 预留改进接口（当前为本地开发版本，后续接入认证）

### P1 - 四级回退没有接入
**位置**: `core/executor.py`
**修复**:
- `_execute_step_with_fallback` 集成完整四级回退
- Level 1: 重试（指数退避，最多3次）
- Level 2: LLM参数修复
- Level 3: 工具降级（备用工具映射）
- Level 4: 跳过（非关键步骤）

### P1 - 主流程没有加载意图配置
**位置**: `core/agent.py`
**状态**: 已接入，`/intent` 和 `/chat` 均加载数据库中启用的意图；空库使用 LLM 兜底。

### P1 - DAG校验不完整
**位置**: `core/planner.py`
**修复**:
- `_validate_dag`: 节点唯一、依赖存在、无环（Kahn算法）
- `_calculate_depth`: 使用拓扑排序计算深度，避免环导致死循环
- `DependencyAnalyzer.analyze`: 正确检测环和孤立节点

### 代码错误 - time模块遮蔽
**位置**: `tools/registry.py:177`
**修复**: `import time as time_module`，避免参数名遮蔽

## 待改进（预留接口）

1. **用户认证**: 接入JWT/OAuth，用户身份来自认证上下文
2. **意图配置管理**: 增加意图配置管理 API 和初始化示例数据
3. **槽位提取**: 兜底识别已输出槽位，仍需完善语义命中后的槽位提取与多轮追问
4. **审批门**: 高风险工具执行前审批
5. **审计日志**: 执行日志持久化到数据库
