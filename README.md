# ReAct with Plan Agent

**简体中文** | [English](README.en.md)

不绑定业务领域、可在本地调试的通用 Agent 框架：Vue 3 + TypeScript 控制台，Python + FastAPI 后端。当前只提供一个真实工具 `web_search`，默认通过免 Key 的 DDGS 检索公开网页，可选自建 SearXNG，再由你配置的大模型整理带来源的回答。原有 Mock 占位工具已移除，缺少模型密钥时明确提示配置问题。

![Agent Studio](docs/images/console.png)

## 本地启动

建议 Python 3.12、Node.js 22 LTS；当前项目也在 Node 20.15 上完成构建。新环境首次安装（在仓库根目录执行）：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
npm.cmd ci --prefix frontend
Copy-Item .env.example .env
```

当前工作区已经准备好了 `.env`，不要覆盖你随后填写的真实配置。新克隆仓库才需要复制模板。

模型无需手动编辑配置文件。先启动前后端，使用管理员 user1 登录，在界面左侧「模型配置」中填写 API 基础地址和 API Key，点击「读取模型列表」，从下拉列表选择模型，再点击「保存并应用」。模型服务需兼容 Chat Completions 和 `GET /models`。不支持 JSON mode 时可在界面关闭该选项，但模型仍需输出合法 JSON。

界面配置自动保存为本机 `data/model-config.json`，刷新或下次启动会读取，密钥不回显；进入模型配置页时会用已保存的连接自动读取模型列表。保存后新任务立即生效，无需重启。优先级：界面配置文件 > 系统环境变量 > 根目录 `.env`。配置文件含密钥，已被 Git 忽略，不要共享。

网页检索无需注册专用 API 或填写搜索 Key，默认 `SEARCH_PROVIDER=ddgs`。升级已有环境后先重新安装 `agent_system/requirements.txt`（本工作区已安装），再启动后端。公开搜索引擎可能限流、要求验证码或在当前网络不可达；失败会明确提示，不会伪装成“无结果”。可在 `.env` 设置 `DDGS_PROXY` 或 `DDGS_BACKEND`，也可切换到自建 SearXNG，详见 [检索方案与配置](docs/web-search.md)。修改检索环境配置需重启后端。

打开两个 PowerShell 终端，都从仓库根目录启动：

```powershell
# 终端 1：后端，支持代码修改后自动重载
.\scripts\dev-backend.ps1
```

```powershell
# 终端 2：Vue + TypeScript 前端，支持热更新
.\scripts\dev-frontend.ps1
```

若系统阻止执行本地脚本，可用 `powershell -ExecutionPolicy Bypass -File .\scripts\dev-backend.ps1`（前端同理）。脚本优先使用 `.venv`，并兼容当前工作区已有的 `.python_packages`。

- 调试页面：<http://127.0.0.1:5173/console/>
- 后端 API：<http://127.0.0.1:8000>
- Swagger：<http://127.0.0.1:8000/docs>

前端默认将 `/api` 请求代理到 `127.0.0.1:8000`，无需在浏览器填写密钥。修改代理地址可复制 `frontend/.env.example` 为 `frontend/.env` 并调整 `VITE_API_PROXY_TARGET`；这些变量是公开的，不可放敏感信息。前端 API 连接设置也可临时覆盖地址。如果后端仍用 8010，将 `frontend/.env` 的 `VITE_API_PROXY_TARGET` 设为 `http://127.0.0.1:8010` 并重启 Vite；也可构建后直接打开 `http://127.0.0.1:8010/console/`。后端根路径只返回状态。对话与执行结果保存在 SQLite，刷新、重新登录或创建新会话均不会删除旧记录。侧栏可搜索、切换和删除历史会话。

## 构建和验证

```powershell
npm.cmd run build --prefix frontend
npm.cmd test --prefix frontend
$env:DEBUG="false"
python -m unittest discover -s tests -v
python scripts/export_openapi.py
```

若使用虚拟环境，将上述 `python` 替换为 `.venv\Scripts\python.exe`。构建后，后端可以直接提供 <http://127.0.0.1:8000/console/>，不必同时启动 Vite；构建产物不提交 Git。开发时推荐使用 5173，避免访问旧构建。前端通过 NDJSON 增量响应显示状态和回答；保留 `/chat` 完整响应接口。测试使用测试夹具，不需要真实密钥、不消耗额度；真实服务的模型兼容性、网络和配额需要你在界面配置后验证。

## 代码与文档

| 路径 | 内容 |
| --- | --- |
| `frontend/src/App.vue` | Vue 控制台：登录、历史会话、流式对话、模型选择、工具、执行详情 |
| `frontend/src/api.ts`、`types.ts` | TypeScript API 客户端与接口类型 |
| `frontend/vite.config.ts` | Vite 开发代理、构建和测试配置 |
| `agent_system/api/routes.py` | FastAPI 接口与静态页面服务 |
| `agent_system/core/` | 意图识别、计划生成、DAG 执行和结果总结 |
| `agent_system/tools/registry.py` | 唯一内置工具 `web_search` 及扩展注册接口 |
| `agent_system/utils/llm_client.py` | 真实 OpenAI 兼容模型客户端 |
| `data/model-config.json` | 界面生成的本机模型配置，含密钥，不提交 Git |
| `.env.example` | 可提交的配置模板；真实 `.env` 被 Git 忽略 |
| [docs/api.md](docs/api.md) | 后端接口文档 |
| [docs/frontend.md](docs/frontend.md) | 前端接口与开发说明 |
| [docs/openapi.json](docs/openapi.json) | 从当前代码导出的 OpenAPI 描述 |
| [docs/web-search.md](docs/web-search.md) | 免 Key 检索实现、开源参考与可选配置 |
| [docs/architecture.md](docs/architecture.md) | 运行流程、失败语义与边界 |

## 登录与会话

| 用户 | 初始密码 | 权限 |
| --- | --- | --- |
| `user1` | `AgentUser1!` | 管理共享模型服务配置及自己的会话 |
| `user2` | `AgentUser2!` | 使用可用模型及自己的会话 |
| `user3` | `AgentUser3!` | 使用可用模型及自己的会话 |

注册入口与 `/auth/register` 已预留，当前关闭。初始密码可在首次启动前通过 `.env` 的 `DEFAULT_USER1_PASSWORD` 等变量修改；只在账号不存在时创建，后续更改变量不会重置密码。密码经 PBKDF2 加盐哈希，登录使用 HttpOnly、SameSite=Strict Cookie，默认有效 7 天；退出登录会撤销服务端会话。数据目录已被 Git 忽略。

数据库同时保存会话、消息、模型、来源及执行详情。后端从认证身份确定用户并检查会话归属，不能由客户端指定其他用户或上传历史。上下文只取当前账号当前会话最近 50 条已完成消息，另有总 48000 字符上限；失败及中断轮次不纳入后续上下文。新会话不会继承其他会话记忆，旧情景记忆模块不接入对话路径。

管理员配置模型连接后，三个账号都可在对话区下拉选择该服务 `/models` 提供的模型；选择只作用于该次请求，不修改共享默认配置。配置地址或密钥只允许管理员修改，普通账号无法调用写配置及连接测试接口。

## 当前执行逻辑

`意图识别 → 检索 → 证据充分性评估 → 改进查询再检索（最多 3 轮）→ 带来源回答`。评估包含原问题、当前会话上下文、累计来源和本轮工具结果；工具成功不等于证据充分。证据覆盖关键问题时提前结束；不足时改进关键词，最多发出 3 次工具调用（失败也占预算，无额外隐式搜索重试）。空查询或重复查询结束循环，不重复消耗预算。

达到上限仍不足时明确说明缺失信息，仅总结可证实的部分；无可用网页时明确无证据，全部检索失败时标记任务失败。普通对话由模型直接回答，不强制检索。模型对证据的判断仍可能有误，摘要检索也不等同于抓取全文。

前端按动画帧合并流式片段，仅在接近底部时自动跟随滚动；可以切换会话查看已有消息。相同会话有运行任务时返回 409，独立会话可分别运行。页面断开后，已开始的任务在后端继续保存结果，可刷新会话恢复。后端进程重启会将未完成任务标记为中断，不会假装继续执行。现在可在对话区停止自己的任务，历史记录分批加载。

通用 DAG 执行器仍保留：失败阻塞依赖后续，**独立分支继续执行**；关键失败影响整体成功状态，不停止独立分支。当前检索流程每轮使用单步计划。

## 使用边界

当前是单进程本地研发框架，启动时进行中断恢复，请使用一个 Uvicorn worker。已加入公平有界排队、每用户配额/限速、依赖熔断、任务超时/取消和安全降级；仍无分布式持久队列、SSO/部门权限、密码管理和生产审计，不能直接当作生产公网服务。默认不需要 Embedding、Chroma 或 Redis，SQLite 自动创建目录和新增表。框架使用自定义 Python 执行器，不依赖 LangGraph。

## 并发治理（0.4）

默认全局执行 4、额外在途 32；每用户执行 1、额外在途 3，按用户轮转调度。排队 60 秒、执行 180 秒超时；依赖连续失败 3 次熔断 30 秒，支持单半开探测。慢客户端事件缓存有上限，已有网页来源时允许明确标注的未核验摘要降级。前端提供取消按钮，拒绝受理时保留输入并显示重试建议，不留下假的对话记录。

配置见 `.env.example`，接口包括 `/runs/{id}`、`/runs/{id}/cancel`、管理员 `/operations` 和就绪探针 `/ready`。新增表自动创建，升级前应停止旧实例并备份 data。仍需 **一个 worker**，新版本会通过本地文件锁拒绝第二个 API 进程。容量参数需根据真实模型额度和任务耗时调整，不能将执行槽位数当作吞吐承诺。


数据库无法写入时，接口明确返回保存失败；数据库中的运行锁可能保留。恢复数据库后，应确认旧进程已停止，再重启单实例，将未完成任务标记为中断并释放会话。当前不支持崩溃后自动续跑，也不保证数据库故障期间的结果不丢失。HTTP 错误返回稳定错误码、请求编号及适用的 Retry-After，详情见 [接口文档](docs/api.md)。
