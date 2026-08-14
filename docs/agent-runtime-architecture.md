# Storydex Agent 双运行时架构

## 1. 运行时边界

Storydex 从同一套经过项目适配的 Coomi Rust 基线派生出两个独立 workspace：

| 平台 | 源码根目录 | 集成入口 | 产品定位 |
| --- | --- | --- | --- |
| Windows 桌面端 | `apps/desktop/coomi-rs-desktop` | `storydex-coomi-bridge` JSONL | 专业长篇小说创作工作台 |
| Android 手机端 | `apps/desktop/coomi-rs-android` | `coomi-ui` HTTP/WebSocket | 角色扮演文字冒险游戏 |

两个 workspace 分别维护版本和 `Cargo.lock`。Provider、engine、security、tools
的通用稳定性修复应分别验证后同步；提示词、入口 crate、交互协议和平台工具不得混用。
旧 `vendor/coomi-rs` 暂时只作为迁移前快照保留，不再参与构建、打包、版本校验或 CI。

## 2. 桌面端职责

桌面端通过 FastAPI 编排 HTTP/SSE、项目上下文、WIKI、人物、世界观、时间线、
预设和写入复核。Rust bridge 负责 Provider 网络、会话、上下文压缩、权限、工具循环、
MCP、memory 与 checkpoint。系统提示强调作者拥有创作决策权、区分正典证据与推断、
保持文风/视角/节奏/连续性，并将写入控制在可审阅范围。

## 3. Android 职责

Android Rust binary 在本机提供 WebView API 和 WebSocket，会话与任务不依赖单个连接存活。
系统提示强调玩家自主权，不代替玩家决定行动、思想、台词或同意；Story、Narrator、Agent
三种模式分别约束正文推进、旁白裁定和文件/工具操作。关闭全局记忆时，security policy
同时阻断文件工具和 shell 对私有 session/config/memory/cache 路径的访问。

## 4. 执行稳定性

- Provider 请求有连接、响应头、首字节、流停滞和总读取超时。
- 408、429 和可恢复 5xx 有有界重试，并尊重服务端 `Retry-After`，最长等待 30 秒。
- 流在未产生可见输出前可恢复；工具参数截断可扩容输出预算后重试。
- 用户消息、模型回复、每个工具结果、压缩结果和终态均原子写入 session checkpoint。
- 中断恢复使用内部 recovery message，不伪装成用户消息，并要求不重复已完成工具。
- 压缩前保存带完整性 hash 的 checkpoint，保留工具调用与证据修订。

## 5. 反馈状态机

主动反馈和运行错误反馈均须由用户发起。工具故障反馈仅在单轮至少三次失败时显示警告：

`consent -> analyzing -> ready -> uploading -> complete`

分析失败回到可重新整理状态；上传失败保留 `ready` 报告，重试只上传，不再次调用模型。
前端先将工具参数转换为结构形状并移除真实值；Rust/后端再次执行 key、路径、URL、密钥、
邮箱和长标识符脱敏。分析固定使用当前 Provider、`reasoning_effort=low`、`tools=[]`、
最多 40 条调用、32 KB 请求和 180 秒超时。上传内容不得包含对话、小说正文、文件内容、
真实路径、URL、API key 或模型隐藏思维。

服务端按 Windows/Android 和 `tool_failure_analysis` 分类检索，分别展示程序证据与本地模型分析。

## 6. 标准验证入口

```powershell
cargo test --manifest-path apps/desktop/coomi-rs-desktop/Cargo.toml --locked --workspace
cargo test --manifest-path apps/desktop/coomi-rs-android/Cargo.toml --locked --workspace
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_full_test_suite.ps1 -Mode Fast
```
