# 后端 API 文档

[中文 README](../README.md) · [English README](../README.en.md) · [前端契约](frontend.md)

默认地址 `http://127.0.0.1:8000`，Swagger `/docs`，完整机器契约 [openapi.json](openapi.json)。用 `python scripts/export_openapi.py` 更新快照。除健康/根路径、文档、静态页面和登录入口外，数据接口均要求登录 Cookie。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/auth/login` | `{username,password}`，返回 `{id,username,is_admin}`，写入 HttpOnly Cookie |
| GET | `/auth/me` | 当前登录用户 |
| POST | `/auth/logout` | 撤销会话，清除 Cookie |
| POST | `/auth/register` | 预留，始终 403，注册关闭 |
| GET | `/conversations` | 当前用户最近 200 个会话，按更新时间倒序 |
| POST | `/conversations` | `{title?:string}` 创建独立会话 |
| GET | `/conversations/{id}` | 当前用户指定会话及按顺序排列的消息 |
| DELETE | `/conversations/{id}` | 删除自己的会话和消息；运行中返回 409 |
| GET | `/models` | 当前服务可用模型与共享默认模型 |
| GET | `/config/model` | 公开连接信息及密钥是否存在，不返回密钥 |
| POST | `/config/model/models` | 管理员测试连接并读取模型列表 |
| PUT | `/config/model` | 管理员验证并保存模型配置 |
| GET | `/runtime` | 模型及检索就绪状态、streaming=true、max_search_rounds=3 |
| GET | `/tools` | `{tools:["web_search"],count:1}` |
| GET | `/tools/web_search` | 工具说明、输入 Schema、超时与就绪状态 |
| POST | `/intent` | 只识别意图，不执行检索或保存消息 |
| POST | `/chat` | 执行并持久化对话，返回完整结果 |
| POST | `/chat/stream` | 相同流程，返回 NDJSON 增量事件 |
| GET | `/`, `/health` | 公开名称/版本、进程健康 |
| GET | `/console/` | 已构建前端，未构建返回 503 |

## 认证与归属

`POST /auth/login` 接收 `{"username":"user1","password":"AgentUser1!"}`。登录 Cookie 默认有效 7 天，HttpOnly、SameSite=Strict；浏览器请求使用 `credentials:'include'`。服务端保存令牌哈希和有效期，密码使用 PBKDF2 SHA256 加盐哈希。登出撤销令牌。默认 3 个用户及首次初始化密码见 README 和 `.env.example`。`COOKIE_SECURE=true` 适用于 HTTPS；本地 HTTP 默认为 false。

用户身份只来自服务端认证。每个会话查询均验证 owner；访问、删除或向其他用户会话发送消息返回 404。共享模型服务写配置和连接测试只允许 user1。配置读取不返回密钥。用户/会话上下文由数据库生成，拒绝客户端传 `user_id`、`session_history` 等额外字段。

## 对话请求与返回

`POST /chat`、`/chat/stream` 和 `/intent` 统一输入：

```json
{"user_input":"检索 Vue TypeScript 官方文档","conversation_id":"当前用户的会话 ID","model":"服务提供的模型 ID"}
```

`user_input` 必填非空白、最长 12000；`conversation_id` 可省略（chat 自动创建）；`model` 可省略（使用共享默认），显式指定时验证服务模型列表。`GET /models` 返回 `{models:["model-a","model-b"],default_model:"model-a"}`，按服务地址及密钥哈希缓存 60 秒；保存配置清除缓存。模型列表中的条目仍需支持 Chat Completions。

完整结果字段：

```json
{
  "conversation_id":"uuid", "message_id":2, "trace_id":"task_example",
  "success":true, "answer":"已核实部分信息。[1]", "model":"model-a",
  "intent":{"intent_id":"web_search"}, "steps":[], "latency_ms":1500, "error":null,
  "sources":[{"title":"Vue","url":"https://vuejs.org/","content":"摘要","score":null}],
  "rounds":[{"round":1,"query":"Vue TypeScript","sufficient":true,"reason":"覆盖关键条件","next_query":""}],
  "evidence_sufficient":true
}
```

示例省略步骤与意图元数据，精确字段以实际结果及 OpenAPI 为准。`message_id` 是本轮 assistant 消息 ID。`evidence_sufficient` 对普通对话为 null；`success=true` 可以伴随证据不足，回答会明确说明局限，不能将运行成功解释成完整答案已证实。全部搜索失败为 `success=false`；正常空结果与工具故障区分。

会话摘要包含 `id,title,model,updated_at,running`；详情还含 `messages`，消息字段为 `id,role,content,model,status,result,created_at`。状态包含 completed、pending、failed、interrupted。上下文只取当前会话最近 50 条 completed 消息，并限制总字符数；失败和中断轮次不传给模型。

## 增量协议

`POST /chat/stream` 返回 `application/x-ndjson`，**每行一个独立 JSON 对象**，不是 SSE。UTF-8 网络分块不保证与 JSON 行或字符边界一致，客户端需要增量解码并按换行解析。事件顺序如下（部分事件可重复）：

```json
{"type":"start","conversation_id":"uuid","message_id":2}
{"type":"status","phase":"routing","message":"正在理解任务"}
{"type":"intent","intent":{"intent_id":"web_search"}}
{"type":"status","phase":"searching","round":1,"query":"关键词","message":"第 1/3 轮检索"}
{"type":"step","step":{"step_id":1,"status":"succeeded"}}
{"type":"review","round":1,"query":"关键词","sufficient":false,"reason":"缺少条件","next_query":"改进查询"}
{"type":"delta","content":"回答片段"}
{"type":"done","result":{"完整字段":"同 /chat"}}
```

空闲约 10 秒发送 `{"type":"heartbeat"}`。`done.result` 是最终权威结果，并已持久化；流程失败可通过 `done.result.success=false` 表达。保存等异常会发送 `type=error,message`；没有 done 的 EOF 视作流中断。浏览器断开后任务继续执行并保存，可 GET 会话恢复，不会自动取消。同一会话重复执行返回 409，独立会话互不阻塞。后端重启将 pending 轮次标为 interrupted；不是断点恢复。可通过 `/runs/{id}/cancel` 取消自己的任务。

## 模型配置

`GET /config/model` 返回 `{base_url,model,api_key_configured,json_mode}`。`POST /config/model/models` 接收 `{base_url,api_key?}`，调用提供方 `GET {base_url}/models`，返回 `{models:[...]}`，不保存配置。`PUT /config/model` 接收 `{base_url,api_key?,model,json_mode:true}`，再次验证模型后原子保存本机 `data/model-config.json`，优先于环境变量和 `.env`，新任务立即生效。

基础地址必须为 HTTP(S)，不可包含凭证、查询串或 fragment。空密钥仅可沿用同一服务的已保存密钥；换地址必须提供新密钥。密钥不回显，错误正文不直接透传，私有数据响应 no-store。任务使用开始时的连接和模型快照。

## 检索与错误语义

唯一工具 `web_search` 输入 `query:string`（1–2000 非空白）、`max_results:int`（1–10，默认 5），不允许多余字段。输出 `query,provider,count,results`，每条来源含 `title,url,content,score`，过滤不安全链接并按 URL 去重。DDGS 不需要专用 Key；SearXNG 调用自建实例 GET `/search?q=...&format=json`。网络故障、限流等不会伪装成空结果，详见 [检索实现](web-search.md)。

证据评估使用原问题、上下文、累计来源和本轮结果；不足则改进查询，最多 3 次搜索调用（失败占预算，无额外工具重试）。充分则提前停止；空查询或重复查询也结束，最终明确证据局限。

- 401 未登录/登录过期；403 注册关闭或权限不足；404 工具/所属会话不存在。
- 409 同一会话已有任务或正在运行不能删除；422 参数/模型/凭证配置不合法。
- 502 模型发现连接/鉴权/列表格式异常；500 配置写入等服务错误。
- 200 + success=false：执行失败，仍保留已执行步骤、来源及记录。
- 通用 DAG 中失败阻塞依赖，独立分支继续；关键失败使整体失败。检索轮次专门禁用额外重试以满足总数上限。

面向单进程调试，已加入用户限流与有界调度；尚无开放注册、SSO 和分布式队列。

## 0.4 并发与任务接口

执行请求可额外提供 `client_request_id`（8–128 字符，建议 UUID）。它按认证用户去重；重复请求返回 409/DUPLICATE_REQUEST，已有 run_id 在 error.details.run_id；编号对应不同内容返回 IDEMPOTENCY_CONFLICT。保留窗口 24 小时（进行中的任务不会因过期放行）。没有自动 200 结果重放，读取原任务及所属会话即可恢复。

- `GET /runs/{run_id}`：仅所有者；返回 id、conversation_id、message_id、state、created_at、started_at、finished_at、error_code。外部任务返回 404。
- `POST /runs/{run_id}/cancel`：仅所有者；返回 cancellation_requested。取消请求是协作式的，最终状态以任务查询为准；已经完成或正在提交结果时不会再次取消。
- `GET /operations`：仅管理员，返回调度总数、依赖熔断状态、HTTP 处理数、结果保存失败计数，不返回其他用户消息。
- `GET /ready`：公开就绪探针，检查排空状态及数据库。`/health` 仅存活，不检测模型配额。
- `GET /conversations/{id}?before_id=123&limit=100`：默认最近 100 条，最多 200，返回 messages（时间正序）、has_more、next_before_id。消息仍按当前用户会话归属过滤。会话摘要增加 run_id，便于刷新后取消。

最终 ChatResult 增加 `run_id,run_state,queue_wait_ms,degraded,error_info`。生命周期为 queued → running → succeeded/failed/cancelled/timed_out/expired/interrupted。排队超时及执行失败在已受理的 /chat 返回 200 + success=false；流式返回 done.result。受理前的容量拒绝为 429，包含 Retry-After。已受理任务的返回延迟可能超过普通短 HTTP 超时，前端应优先用 /chat/stream 或查询任务。

统一 HTTP 错误：`{detail,error:{code,message,retryable,retry_after,details?},request_id}`，带 X-Request-ID。常见新增码：USER_CAPACITY_EXCEEDED、QUEUE_FULL、USER_RATE_LIMITED、QUEUE_TIMEOUT、RUN_TIMEOUT、RUN_CANCELLED、CIRCUIT_OPEN、DEPENDENCY_BUSY、UPSTREAM_RATE_LIMITED、UPSTREAM_UNAVAILABLE、RESULT_PERSIST_FAILED。错误详情不回显原始 provider body、密码、SQL 或完整输入。

NDJSON start 增加 run_id，后续 status.phase 可为 queued/degraded。单客户端缓存默认 128 个事件；过慢时发 error/code=SLOW_CONSUMER 并结束推送，后台结果仍持久化，可刷新读取。此时不能推断任务被取消。

默认执行 4 个任务，额外在途 32；每用户执行 1、额外在途 3。排队 60 秒、执行 180 秒截止，依赖连续失败 3 次熔断 30 秒。具体配置见 `.env.example`。数据库写入失败时可能保留运行锁；恢复数据库后，确认旧进程已停止，再重启单实例恢复中断状态。当前不保证崩溃后续跑或数据库故障期间结果不丢失。
