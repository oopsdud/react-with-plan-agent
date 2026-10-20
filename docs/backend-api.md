# 后端接口文档

版本：0.1.0。此文档描述当前实现，不代表后续架构设计已实现。

## 启动与地址

在仓库根目录执行：

```powershell
$env:PYTHONPATH = "$PWD/.python_packages"
$env:DEBUG = "false"
python -m uvicorn agent_system.api.routes:app --host 127.0.0.1 --port 8000
```

首次安装依赖：`python -m pip install --target .python_packages -r agent_system/requirements.txt`。
需 Python 3.10+。从仓库根目录运行，确保相对数据库路径一致。默认无需 Node 构建。

- 前端：http://127.0.0.1:8000/console/
- Swagger：http://127.0.0.1:8000/docs
- ReDoc：http://127.0.0.1:8000/redoc
- 实时协议：http://127.0.0.1:8000/openapi.json
- 协议快照：`docs/openapi.json`，修改接口后执行 `python scripts/export_openapi.py` 更新。

## 通用约定

请求与响应使用 JSON，POST 设置 `Content-Type: application/json`。当前没有登录鉴权，`user_id` 是调用方传入的调试标识，不能用于生产身份认证。默认工具均为 Mock；缺少模型密钥时会使用 Mock LLM。CORS 默认只允许 localhost:8000 和 127.0.0.1:8000，可通过 `CORS_ORIGINS` 的 JSON 字符串数组配置。

## GET /

响应：`{"name":"Agent System","version":"0.1.0","status":"running"}`。

## GET /health

响应：`{"status":"healthy"}`。仅表示进程可响应，不检测模型、数据库或向量库就绪状态。

## GET /runtime

返回非敏感运行信息，不返回 API Key、数据库地址或密钥。

```json
{
  "version": "0.1.0",
  "llm_mode": "mock",
  "llm_model": "mock-llm",
  "embedding_provider": "local",
  "embedding_note": "配置值；实际加载失败时可能回退到 Mock",
  "tools_mode": "mock",
  "streaming": false
}
```

`llm_mode` 为 `mock` 或 `api`；`embedding_provider` 是配置值，不保证实际加载成功；`tools_mode` 当前固定为 `mock`。

## POST /chat

运行完整的意图识别、规划、工具执行与汇总流程。非流式接口，没有后端取消 API。

| 请求字段 | 类型 | 必填 | 默认值 / 说明 |
| --- | --- | --- | --- |
| user_input | string | 是 | 非空白，1–12000 字符 |
| user_id | string | 否 | `default_user`，非空白，1–100 字符 |
| session_history | array<object<string,string>> | 否 | `[]`，最多 100 条，建议元素为 `role`、`content`；不含本次输入 |

```json
{
  "user_input": "查询张三的员工信息",
  "user_id": "default_user",
  "session_history": [
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "你好，请描述任务。"}
  ]
}
```

响应结构（示意，具体内容取决于运行结果）：

```json
{
  "trace_id": "task_0123456789ab",
  "success": false,
  "answer": "任务执行失败",
  "intent": {
    "intent_id": "general",
    "complexity": "complex",
    "confidence": 0.85,
    "is_multi_intent": false,
    "secondary_intents": [],
    "extracted_slots": {},
    "reasoning": "识别说明",
    "routing_path": "llm_fallback"
  },
  "steps": [
    {
      "step_id": 1,
      "tool_name": "query_user",
      "status": "failed",
      "params": {},
      "result": null,
      "error": "错误说明",
      "retry_count": 0,
      "latency_ms": 1
    }
  ],
  "latency_ms": 10,
  "error": "错误说明"
}
```

`answer`、`intent`、`error` 可以是 null。步骤状态包括 `succeeded`、`failed`、`skipped`、`pending`；`result` 是任意 JSON 值。`intent` 与 `steps` 当前在 OpenAPI 中属于通用对象，以上为核心实现使用的结构。

**成功判断：** HTTP 200 仅表示返回了任务结果，必须检查 `success`、`error`、`steps[].status`。为兼容后端版本差异，即使 `success=true`，只要存在失败步骤，前端仍额外标记“存在失败”。

## POST /intent

仅识别，不执行工具。请求：

```json
{"user_input":"查询张三的员工信息","session_history":[]}
```

`user_input` 必填；`session_history` 可选，默认 `[]`。响应为上述 `intent` 对象，所有列出的字段均存在。

`/intent` 和 `/chat` 均从数据库加载启用的意图；空数据库使用 LLM 兜底。Mock 模型只生成固定占位结果，不代表真实自然语言理解。

## GET /tools

```json
{"tools":["query_user","send_notification"],"count":2}
```

此处仅示意两项，实际返回全部注册工具。`tools` 为工具名称字符串数组，`count` 为数量。

## GET /tools/{tool_name}

路径参数 `tool_name` 为工具名称，必须进行 URL 编码。

```json
{"name":"query_user","description":"查询用户信息","timeout_seconds":30}
```

工具和执行器均限制等待时间；同步工具在线程池中执行时，超时无法强制终止底层线程。不存在的工具返回 404。

## 分支失败语义

步骤的依赖必须全部成功才可运行；某步骤失败或被跳过，其后代标记 `skipped`，不影响无依赖关系的分支继续执行。`is_critical` 决定关键步骤失败/被阻塞是否令整体 `success=false`，不表示中断其他独立分支。所有独立分支结束后统一汇总结果；当前 API 不增加 `partially_succeeded` 状态。

## 错误与超时

| HTTP 状态 | 含义 | 响应 |
| --- | --- | --- |
| 404 | 工具不存在 | `{"detail":"Tool not found: xxx"}` |
| 422 | 请求字段缺失或类型不匹配 | `{"detail":[{"loc":["body","user_input"],"msg":"...","type":"..."}]}` |
| 500 | 未处理的服务异常 | `{"detail":"错误信息"}` |

Agent 内部捕获的失败通常通过 `/chat` 的 HTTP 200 结果表达。当前异常文本可能包含内部信息，上线前应统一错误码与脱敏。客户端超时/断开不代表任务回滚或停止，不应自动重试写操作。

## 前端资源

`GET /console/` 为 HTML，`/console/styles.css`、`/console/app.mjs`、`/console/api.mjs` 为静态资源。根路径 `/` 保留原有 JSON 服务信息，不重定向。
