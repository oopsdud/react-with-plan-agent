# 前端接口与交互文档

## 结构与运行

前端是原生 HTML/CSS/ES Modules，无外部 CDN 和构建依赖；由 FastAPI 的 `/console/` 同源托管。

| 文件 | 职责 |
| --- | --- |
| `frontend/index.html` | 对话、工具、文档页面及无障碍语义结构 |
| `frontend/styles.css` | 响应式布局、状态和主题 |
| `frontend/api.mjs` | 可复用 HTTP 客户端，公开模块接口 |
| `frontend/app.mjs` | 页面交互与内存状态，不提供外部服务 |

启动见 [后端接口文档](backend-api.md)。前端没有独立 HTTP 业务接口；它消费后端 API，通过下列 JavaScript 接口供其他页面复用。不能使用 `file://` 直接打开 HTML。

## AgentAPI

```javascript
import { AgentAPI, APIError } from './api.mjs';
const api = new AgentAPI('', { timeoutMs: 120000 });

await api.health();
await api.runtime();
await api.tools();
await api.tool('query_user');
await api.intent({ user_input: '查询张三', session_history: [] });
await api.chat({
  user_input: '查询张三',
  user_id: 'default_user',
  session_history: []
});
```

### 构造参数

`new AgentAPI(baseURL = '', options = {})`

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| baseURL | string | `''` | 空值代表同源；可设 HTTP(S) 地址或带路径的 API 前缀，末尾斜线自动去除 |
| options.timeoutMs | number | `120000` | 单请求等待超时，毫秒 |
| options.fetchImpl | function | 原生 fetch | 用于测试或定制传输 |

地址拒绝非 HTTP(S) 协议、URL 内嵌凭证、查询参数和片段。浏览器对跨域请求需要后端 CORS 支持；HTTPS 页面连接 HTTP 后端可能被浏览器阻止。客户端固定 `credentials: 'omit'`，当前不支持 Cookie 登录或授权头配置。

### 方法

| 方法 | 后端接口 | Promise 返回 |
| --- | --- | --- |
| health() | GET /health | `{status: string}` |
| runtime() | GET /runtime | 运行信息，见后端文档 |
| tools() | GET /tools | `{tools: string[], count: number}` |
| tool(name: string) | GET /tools/{name} | `{name, description, timeout_seconds}` |
| intent(payload) | POST /intent | 意图对象 |
| chat(payload) | POST /chat | 完整任务结果 |
| request(path, {method, body}) | 通用 JSON 请求 | 解析后的 JSON |

`request` 的 method 默认 GET，body 默认未提供。请求 JSON 序列化；非 2xx、非 JSON、网络异常或超时抛出 `APIError`。业务层 `success=false` 仍返回结果，由 UI 处理。

### APIError

公开字段：`name='APIError'`、`message: string`、`status: number`、`detail: unknown`。网络和超时错误的 status 为 0；HTTP 异常保留响应状态和 detail。客户端不自动重试。

## 页面行为

- **完整执行**：提交前面的成功对话历史和当前输入，等待 `/chat`，显示回答、意图、耗时、trace、每步输入输出与完整 JSON。失败步骤优先于后端成功标记，显示“存在失败”。
- **仅识别意图**：调用 `/intent`，不执行工具，不写入后续对话历史。耗时显示为 `—`，该接口没有耗时字段。
- **历史**：只有无失败步骤且 success 为 true 的完整执行加入请求历史，最多保留最近 100 条消息。后端目前不存储前端对话。
- **输入**：Enter 发送，Shift+Enter 换行；中文输入法组合输入时不会误提交；界面限制 12000 字符，空白任务不提交。
- **并发**：单页面一次只允许一个任务；等待期间禁用发送、模式切换、新建和保存设置，防止状态错位。
- **查看历史详情**：每个已返回的响应均有“查看本次执行详情”按钮，切回该次结果，不重复请求。
- **新建对话**：清空当前内存历史和执行详情，不删除服务端情景记忆。
- **连接设置**：切换后端或调试用户会清空对话；只在点击保存时生效。连接检测使用 health 与 runtime，不执行任务。
- **工具目录**：读取工具列表和各工具详情，支持名称、描述搜索和刷新。单个工具详情失败不会隐藏其他工具。
- **接口文档**：链接到所配置后端的 Swagger 和实时 OpenAPI。
- **超时**：停止等待并展示风险提示；不提供“后端已取消”的保证，也没有自动重试。

## 浏览器存储

localStorage 键：`agent-studio.settings.v1`。

```json
{"baseURL":"","userId":"default_user"}
```

聊天正文、工具结果只在页面内存中，刷新后清空。存储不可用时退化为页面内设置。用户 ID 不是认证凭证。不要在前端填入模型密钥。

## 展示安全与限制

后端内容通过 DOM `textContent` 展示，不执行返回的 HTML、Markdown 或脚本。资源不依赖互联网。页面不伪造规划过程或执行进度，等待完整响应后展示实际结果。

当前后端工具为 Mock，尚无流式接口、服务端会话列表、登录、审批、取消与断点恢复；前端不提供这些未实现的操作。

## 验证

```powershell
node --test tests/frontend-api.test.mjs
node --check frontend/app.mjs
$env:DEBUG = "false"
python -m unittest discover -s tests -p "test_console.py"
python scripts/export_openapi.py
```

Python 检查需要依赖已安装，并按启动说明配置 PYTHONPATH。测试覆盖静态页面与资源、运行配置不泄露密钥、接口协议、HTTP 错误、业务失败、URL 编码及请求超时。视觉布局需在浏览器中人工复核。
