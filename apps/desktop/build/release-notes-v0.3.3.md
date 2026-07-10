# Storydex v0.3.3

## 修复与体验改进

- 修复桌面冷启动、离线运行或字体首次加载失败时 Material Symbols 图标空白的问题；加入重试、超时状态和可见降级。
- Coomi 请求进入后立即返回 `RunAccepted`，意图识别、上下文装配、任务规划和模型等待期间持续输出带耗时的阶段事件与 heartbeat。
- 意图 LLM 最长等待 2 秒，超时使用确定性 heuristic fallback；明确指令优先走快速路径。
- 恢复应用重启后的真实 Coomi JSONL 会话上下文、上一轮助手回复和 pending action；项目与 sessionId 完全隔离。
- 修复活动文件位于 `chapters/` 时省略指令被错误判定为新剧情生成的问题。
- 优化 Git 提交确认面板：点击后立即反馈、跳过走快速路径、自动提交说明超时后使用本地回退。
- 新增后端、前端和 Electron 分层测试、统一 Fast/Full/Release 测试入口、可复用 CI 质量门禁和打包冒烟验证。

## 从 v0.3.2 更新

安装版可在应用内检查 v0.3.3。更新源同时提供 `latest.yml`、v0.3.3 installer 与对应 blockmap，`electron-updater` 会优先执行 v0.3.2 → v0.3.3 差分下载；也可直接下载完整安装包覆盖安装。更新失败时不会删除现有 v0.3.2 安装，用户仍可重新启动旧版本。

本地 Git 功能只为小说项目创建本地提交，不自动配置远程仓库，也不会自动 push。
