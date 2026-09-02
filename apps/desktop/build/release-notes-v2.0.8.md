# Storydex v2.0.8

Storydex v2.0.8 是 Windows 桌面端 Tauri 2 的运行时兼容性修复版本，修复从旧 Python 运行时迁移后部分项目和待发送消息无法继续使用的问题。

## 修复内容

- 兼容旧 Python 运行时写入的工作区路径格式：统一处理正斜杠、反斜杠、`\\?\\` 扩展前缀、UNC 路径、大小写差异和重复分隔符，避免同一目录被误判为不同工作区。
- 兼容缺少 `previousTraceId` 的旧待发送消息，反序列化时安全默认为空，并保留原消息内容及已有 `segmentId`。
- 从旧 mailbox 的活动消息和事件元数据恢复 `lastTraceId`，避免恢复待发送消息时触发错误的过期 trace 判断。
- 首次正常写入时规范化旧 mailbox 格式，不删除项目文件、消息内容或 mailbox 数据。

## 升级说明

- v2.0.6 和 v2.0.7 用户请下载完整安装包 `StorydexSetup-x64-2.0.8.exe` 覆盖安装，以获得 mailbox 兼容性修复。
- 本次修复不会主动删除项目目录、会话记录或待发送消息；安装前仍建议备份重要项目。
- v2.0.5 及更早版本仍处于 Electron → Tauri 迁移路径，请按对应版本说明先使用完整安装包迁移；不要把 Tauri 文件直接复制到旧 Electron 目录。
- Windows 更新源：<https://updates.septemc.com/storydex/windows/latest.json>

## 发行资产

- `StorydexSetup-x64-2.0.8.exe`：Windows x64 NSIS 安装包。
- `StorydexSetup-x64-2.0.8.exe.sig`：Tauri updater 签名文件。
- `Storydex-win-portable.zip`：Rust-only 便携包。
- `latest.json`、`SHA256SUMS.txt`、`BUILD_MANIFEST.json` 和 `DEPENDENCIES.json`：更新清单、校验值、构建信息与依赖清单。
