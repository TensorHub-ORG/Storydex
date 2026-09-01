# Storydex v2.0.6

Storydex v2.0.6 是 Windows 桌面端 Tauri 2 Stable 版本，完成从旧 Electron/Python 桌面链路到 Rust-only 运行时的切换，并集中提升稳定性、安全性和创作反馈。

## 主要更新

- 桌面壳升级为 Tauri 2，内置 `storydex-agentd` 与 `storydex-coomi-bridge` Rust 运行时；正式包不再携带 Electron、Python 或 Node 运行时。
- 重做 Windows 工作台桌面交互：原生窗口控制、目录选择、文件预览、Open With、单实例和退出清理，启动与关闭更稳定。
- Agent 反馈与创作流程增强：接受、上下文装配、模型等待、工具执行、完成和异常阶段更清晰；支持会话隔离、意图澄清、工具结果折叠、失败记录管理和精确章节字数目标。
- 强化项目操作与安全边界：Rust 侧执行工作区路径校验、动态 loopback 端口和运行令牌，Git、WIKI、故事生成的写入与回滚更可靠；渲染层权限收窄并过滤敏感配置、路径和诊断信息。
- 完善 Windows 发布与更新门禁：签名 updater、NSIS 安装包、便携包、`latest.json`、校验值、依赖清单和构建 manifest 全流程校验，更新源采用原子切换。

## 升级说明

- v2.0.5 及更早版本仍使用旧 Electron 更新链路，不能直接读取 Tauri 的 `latest.json`。请先备份项目，再下载完整安装包 `StorydexSetup-x64-2.0.6.exe` 覆盖安装。
- v2.0.6 及后续 Tauri 版本可通过应用内更新升级，也可以使用 `Storydex-win-portable.zip` 运行便携版。
- Windows 更新源：<https://updates.septemc.com/storydex/windows/latest.json>

## 发行资产

- `StorydexSetup-x64-2.0.6.exe`：Windows x64 NSIS 安装包。
- `StorydexSetup-x64-2.0.6.exe.sig`：Tauri updater 签名文件。
- `Storydex-win-portable.zip`：Rust-only 便携包。
- `latest.json`、`SHA256SUMS.txt`、`BUILD_MANIFEST.json` 和 `DEPENDENCIES.json`：更新清单、校验值、构建信息与依赖清单。
