# Storydex v2.0.5

Storydex v2.0.5 完成 Windows 桌面 Agent 架构重构，并集中提升长篇小说创作任务中的调用稳定性、过程反馈和隐私保护。

## 主要更新

- 桌面 Agent 迁移到 `apps/desktop/agent-runtime`，与 Android 运行时彻底分离，使用面向小说创作者工作台的独立提示词、技能目录和 JSONL bridge。
- 对齐 Coomi 优化后的 Provider 执行逻辑，增强流式响应、连接恢复、重试退避、usage 统计和工具调用链路的稳定性。
- 补齐 Agent 接受、上下文装配、模型等待、工具执行、完成与异常阶段的可观察反馈，并新增可审阅的工具失败反馈入口。
- 强化桌面反馈隐私保护，提交前过滤敏感配置、鉴权信息、本地路径和不应离开设备的诊断内容。
- 优化 Windows 开发分支 CI 与运行时构建校验，确保后端测试使用当前提交对应的 Rust Agent，降低跨平台迭代耦合。

## 升级说明

- 支持从上一正式版本通过应用内更新升级，也可以下载 `StorydexSetup-x64-2.0.5.exe` 覆盖安装。
- 本版本继续提供安装包、差分更新 blockmap、便携 ZIP、校验值、依赖清单和构建 manifest。
- 安装包继续内置 Python 3.9、MinGit、后端服务及 Storydex Windows 专用 Coomi Rust 运行时，无需用户另外安装运行环境。
