# 免 Key 网页检索

当前唯一工具仍是 `web_search`，不绑定业务领域。输入 `query`、`max_results`，输出标准化标题、URL、摘要及来源。模型 API Key 仍是你所选模型服务的凭证；检索没有额外专用 Key。

## 默认 DDGS

```dotenv
SEARCH_PROVIDER=ddgs
DDGS_BACKEND=auto
DDGS_REGION=us-en
SEARCH_SAFESEARCH=moderate
SEARCH_TIMEOUT_SECONDS=20
TOOL_TIMEOUT_SECONDS=30
# 仅在网络需要时配置代理；不要把带凭证的代理地址分享出去。
# DDGS_PROXY=http://127.0.0.1:7890
```

依赖 `ddgs>=9.14.1,<10`，通过 `python -m pip install -r agent_system/requirements.txt` 安装。DDGS 自行访问公开搜索引擎并解析、聚合结果，不使用 Tavily。`DDGS_BACKEND` 可以是 `auto`、`duckduckgo`、`brave` 或库支持的逗号分隔后端列表；可用后端以安装版本为准。默认 us-en 避免部分百科后端对全球地区代码的兼容问题，可按需改为 cn-zh 等地区。

公开引擎仍可能出现限流、验证码、网络不可达和页面格式变化。程序会明确报告执行失败；只把库明确返回的空结果当作无匹配。不保证免费检索具有商业 API 的可用性或 SLA。返回的是搜索摘要，不自动抓取所有来源全文。

## 可选自建 SearXNG

```dotenv
SEARCH_PROVIDER=searxng
SEARXNG_BASE_URL=http://127.0.0.1:8088
```

先部署你自己的 [SearXNG 实例](https://docs.searxng.org/admin/installation.html)，在它的 settings.yml 中启用 JSON：

```yaml
search:
  formats:
    - html
    - json
```

本项目向实例发送 `GET /search?q=...&format=json&categories=general`，不发送专用搜索 Key。未启用 JSON 常见返回 403；不能将其当作“没有搜索结果”。默认不绑定任何公共实例，避免依赖别人的配额和稳定性。实例部署、网络和维护仍有自身成本。

## 开源调研依据（2026-10-21）

| 项目 | 实现方式 | 本项目采用点 |
| --- | --- | --- |
| [DeerFlow 配置](https://github.com/bytedance/deer-flow/blob/main/config.example.yaml)、[DDGS 工具源码](https://github.com/bytedance/deer-flow/blob/main/backend/packages/harness/deerflow/community/ddg_search/tools.py) | 提供无需 Key 的 DDGS 与可选 SearXNG，区分故障和正常空结果 | 默认使用现成 Python 检索库，保留错误边界 |
| [DeepSeek Harness 源码](https://github.com/deepseek-ai/deepseek-harness)、[公共引擎社区提供方](https://github.com/hy-sde/dsh-web-search-public) | 工具/服务/提供方分离；社区实现多公开引擎并发检索、合并结果 | 一个模型可见工具，对接不同检索实现；不直接移植 TypeScript 抓取器 |
| [DDGS](https://github.com/deedy5/ddgs) | 多搜索源聚合 Python 库，可设置后端、地区、代理和超时 | 作为默认依赖调用，不自行维护每家网页解析规则 |
| [SearXNG 搜索 API](https://docs.searxng.org/dev/search_api.html) | 聚合检索实例通过 HTTP 返回 JSON，需要实例启用格式 | 可选自托管后端 |

DSH 默认提供方与免 Key 社区提供方是不同实现，不应把“开源框架”等同于“默认免费检索”。本项目借鉴适配架构，未复制这些项目的搜索抓取代码。
