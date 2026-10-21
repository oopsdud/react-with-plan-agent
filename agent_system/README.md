# Agent 后端

这是通用 Agent 运行框架，不预设业务领域。目前只注册一个真实的网页检索工具 `web_search`，用于验证意图识别、规划、执行和总结链路。

完整配置和启动步骤见 [中文 README](../README.md) / [English README](../README.en.md)。

- [后端 API](../docs/api.md)
- [架构与运行边界](../docs/architecture.md)
- [前端开发](../docs/frontend.md)

从仓库根目录执行 `scripts/dev-backend.ps1`，或在安装依赖并设置环境后运行 `python -m uvicorn agent_system.api.routes:app --reload --host 127.0.0.1 --port 8000`。
