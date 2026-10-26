# 前端开发与接口

[中文 README](../README.md) · [English README](../README.en.md) · [后端契约](api.md)

Vue 3 Composition API + TypeScript strict + Vite。入口 `frontend/src/main.ts`，应用 `App.vue`，消息组件 `MessageBubble.vue`，模型设置 `ModelSettings.vue`，请求客户端 `api.ts`，类型 `types.ts`，样式 `styles.css`。

## 开发

`npm ci`、`npm run dev`（frontend 目录），访问 http://127.0.0.1:5173/console/。`npm run build` 执行 vue-tsc 并输出 dist；`npm test` 使用 Vitest/jsdom。构建后 FastAPI 可同源提供 `/console/`。

开发 API 默认 `/api`，Vite 代理移除前缀，目标默认 8000。若后端用 8010，修改 `frontend/.env` 的 `VITE_API_PROXY_TARGET=http://127.0.0.1:8010`，重启 Vite。生产默认同源。`VITE_API_BASE_URL` 可覆盖公开 API 地址；所有 VITE 变量会进入前端，禁止存密钥。推荐同源或 Vite 代理，使登录 Cookie 正常工作；跨源直连需配置 CORS 的明确来源并使用同一主机名，勿混用 localhost 和 127.0.0.1。

## 客户端契约

`AgentAPI(baseURL)` 提供 `login/me/logout`、`conversations/createConversation/conversation/deleteConversation`、`catalog`、`modelConfig/models/saveModel`、`runtime/tool`、`chat/intent/stream`。请求统一携带 credentials=include；HTTP 非 2xx 抛 APIError（含 status），401 清除界面登录状态。普通请求超时 60 秒；流式连接持续读取事件，不设置覆盖整轮任务的短超时。

`chat(input,conversationId?,model?)` 返回完整 ChatResult。`intent` 同参数，不执行。`stream(input,conversationId,model,onEvent,signal?)` 增量解析 NDJSON，处理跨块 UTF-8、心跳和最终 done。HTTP 200 且 success=false 是任务失败而非网络错误；done 前断开提示刷新会话恢复。终止客户端连接并不取消后端任务。

## 页面行为

- 未认证显示登录，预置三个账号；注册按钮禁用，服务端注册同样关闭。
- 侧栏显示当前账号历史，可搜索标题、创建/切换/删除。刷新读取数据库，sessionStorage 只按用户记录最近打开的会话 ID；消息内容及凭证不存浏览器存储。
- 每个会话独立维护消息与运行状态。后端归属校验为权限边界，前端缓存仅作交互状态。退出登录清除所有消息、模型列表及运行视图，并使旧流回调失效。
- 对话区选择服务返回的模型，随本轮请求发送，不改全局默认。管理员设置共享服务，普通账号可选择模型但不可写连接配置。
- 字符片段按 requestAnimationFrame 合并，避免逐字触发多次滚动；只在接近底部时跟随，向上阅读时显示回到最新按钮。
- 独立滚动区、输入框自动增高、Enter 发送/Shift+Enter 换行、输入法组合状态保护。移动端侧栏为抽屉，支持减少动画偏好。
- 显示当前检索/评估阶段，完成后保留来源、模型与轮次。执行详情展示每轮查询、充分性原因及底层步骤。
- 模型与网页文本通过 Vue 转义，代码围栏单独显示，来源仅允许 HTTP(S)。不执行模型生成的 HTML。
- 后端任务仍运行时同一会话不能再次发送/删除；独立会话可切换。刷新当前会话读取后端结果，可在对话区取消当前任务；尚未实现跨进程事件重放和自动任务轮询。

## 模型配置

user1 可读配置、填写地址和 Key、发现模型、选择默认并保存。成功后清空密钥框、刷新模型目录和运行状态，不需重启。进入页面自动读取已保存连接和模型列表；地址/密钥改变使发现结果失效。换地址不能沿用旧密钥。其他账号仅见说明和可用模型数量。

README 中英文通过文件互链切换；界面语言目前为中文。

## 并发交互

排队事件实时显示；start.run_id 写入当前会话运行状态，停止按钮调用后端取消接口，等待最终持久化结果。HTTP 429 等未受理错误回滚乐观消息并保留输入，显示 Retry-After 与请求编号；流断开仍提示刷新恢复。来源摘要降级有明确标记。历史分页使用 before_id，在顶部加载更早消息并保持滚动位置。

请求自动生成 client_request_id 以避免同一请求重复执行；手动重新发送是新请求。APIError 增加 code、requestId、retryAfter，仍保留 status。凭证和对话内容不写浏览器存储。


## 检查点界面

消息含 checkpoint_available 时显示检查点入口；失败或中断可点击“从检查点继续”。面板展示计划版本、执行代次、步骤状态、复用来源及最近尝试，支持填写修订原因并提交完整依赖图 JSON。写入使用 expected_version，恢复使用当前 generation，后端校验归属和冲突；不能通过界面指定其他用户身份。

恢复任务在后台排队，当前会话通过轻量任务状态轮询更新，完成后重新读取持久消息。原消息就地更新，不新增重复问题。图修订后需明确点击继续，不自动执行。JSON 编辑、读取与恢复失败会保留界面并显示后端错误；退出登录使旧回调失效。
