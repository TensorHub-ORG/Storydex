# Storydex Agent 双平台 Rust 架构

更新日期：2026-08-29

本文是贡献者和维护者参考，不是普通用户操作手册。它描述当前源码边界；迁移过程中的诊断、交接和一次性实验不作为公开架构文档保存。

## 1. 运行时边界

Storydex 从同一套经过项目适配的 Coomi Rust 基线派生出两个独立 workspace：

| 平台 | 源码根目录 | 集成入口 | 产品定位 |
| --- | --- | --- | --- |
| Windows 桌面端 | `apps/desktop/agent-runtime` | `storydex-agentd` HTTP/SSE sidecar | 专业长篇小说创作工作台 |
| Android 手机端 | `apps/android/agent-runtime` | `coomi-ui` HTTP/WebSocket | 角色扮演与移动创作体验 |

两个 workspace 分别维护版本和 `Cargo.lock`。Provider、engine、security、tools 的通用稳定性修复需要分别验证后同步；提示词、入口 crate、交互协议和平台工具不得混用。

Windows Stable 使用 Tauri 2 启动独立 `storydex-agentd`。Electron 桌面运行时和 Python Agent 代码已从产品路径移除；`apps/backend` 仅作为非 Stable 的后端兼容与测试边界保留。

## 2. Windows 桌面端职责

Windows 正式链路为：

```text
Vue 工作台 → Tauri 2 → storydex-agentd → Coomi Rust
```

`storydex-agentd` 负责 HTTP/SSE、项目上下文、WIKI、人物、世界观、时间线、预设、Git、写入复核和 Agent 控制面。Coomi Rust crate 负责 Provider 网络、会话、上下文压缩、权限、工具循环、MCP、memory 与 checkpoint。

Tauri 只负责桌面能力和 sidecar 生命周期，不承载长时间 Agent 执行。动态 loopback 端口和随机运行令牌不暴露给渲染层；退出时通过鉴权 shutdown 和 Windows Job Object 保证进程树回收。

## 3. Android 职责

Android Rust binary 在本机提供 WebView API 和 WebSocket，会话与任务不依赖单个连接存活。Story、Narrator、Agent 三种模式分别约束正文推进、旁白裁定和文件/工具操作。关闭全局记忆时，security policy 同时阻断文件工具和 shell 对私有 session、config、memory、cache 路径的访问。

## 4. 执行稳定性

- Provider 请求具有连接、响应头、首字节、流停滞和总读取超时。
- 408、429 和可恢复 5xx 使用有界重试，并尊重服务端 `Retry-After`。
- 流在未产生可见输出前可以恢复；工具参数截断可以扩容输出预算后重试。
- 用户消息、模型回复、工具结果、压缩结果和终态原子写入 session checkpoint。
- 中断恢复使用内部 recovery message，不伪装成用户消息，并禁止重复已完成工具。
- Windows 前端实际消费的 API 契约全部由 `storydex-agentd` 覆盖，正常路径不回退到 Python。

## 5. 反馈状态机

主动反馈和运行错误反馈均须由用户发起。工具故障反馈仅在单轮达到失败阈值时显示警告：

```text
consent → analyzing → ready → uploading → complete
```

前端先移除工具参数真实值；Rust 服务再次执行字段、路径、URL、密钥、邮箱和长标识符脱敏。上传内容不得包含对话、小说正文、文件内容、真实路径、URL、API Key 或模型隐藏思维。

## 6. 分支与源码所有权

- `main` 保存 Windows 和 Android 的稳定实现，并按改动范围执行组件门禁；相同 SHA 已通过 `dev/windows` 时复用 Windows 结果，完整跨平台矩阵改为显式运行。
- `dev/windows` 只集成 Windows 桌面端改动，先通过 Windows Development CI，再进入 `main`。
- Windows runtime 不得依赖 `apps/android/agent-runtime`，Android runtime 不得依赖 `apps/desktop/agent-runtime`。
- `apps/backend` 不再属于 Windows Stable runtime，也不提供 Agent 产品入口；需要 Python 的兼容测试必须显式运行，不能由桌面启动链路拉起。
- Provider 事件、反馈载荷和 session 版本等跨端要求通过文档、Schema 和契约 fixture 同步。

平台主要所有权如下：

| 分支 | 主要路径 |
| --- | --- |
| `dev/windows` | `apps/desktop`、`apps/frontend` |

`main` 的稳定集成范围包括 `.github`、`scripts`、`deploy`、根级依赖和跨端协议。其他远端分支不在本文的治理范围内。

## 7. 聚焦验证入口

```powershell
cargo test --manifest-path apps/desktop/agent-runtime/Cargo.toml --locked --workspace
npm --prefix apps/desktop run test:unit
npm --prefix apps/desktop run check:release
npm --prefix apps/desktop run check:tauri
```

普通 push 前只执行 `scripts/run_pre_push_ci.ps1` 的基础检查；全组件和发布门禁由 GitHub Actions 执行，本地完整套件仅在人工明确需要时运行。
