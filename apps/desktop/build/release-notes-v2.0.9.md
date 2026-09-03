# Storydex v2.0.9

Storydex v2.0.9 是 Windows 桌面端稳定修复版本，重点修复中文工作区在模型回复完成后出现内部错误，以及 Windows 任务栏图标被低分辨率图层放大而模糊的问题。

## 修复内容

- 修复包含中文字符的工作区路径触发 follow-up mailbox 路径规范化 panic，避免正常回复结束后再次出现 `Storydex Agent service encountered an internal error.`。
- 增加中文工作区的 follow-up 路由回归覆盖，确保列出和发送待处理消息不会因 UTF-8 字节边界错误返回 500。
- 调整 Windows ICO 图层顺序，将 256×256 PNG 图层放在首位，同时保留 16/20/24/32/40/48/64/96/128/256 全部尺寸，避免 Tauri 运行时将 16px 图标放大到任务栏造成模糊。
- 官网 Windows 下载链接更新为 `StorydexSetup-x64-2.0.9.exe`，应用内更新源继续使用 `https://updates.septemc.com/storydex/windows/latest.json`。

## 升级说明

- v2.0.8 用户可直接使用完整安装包 `StorydexSetup-x64-2.0.9.exe` 覆盖安装；安装不会删除项目目录、会话记录或 Provider 配置。
- 安装前仍建议备份重要项目。更新源会在新版本资产全部上传并校验后原子切换到 v2.0.9。
- Windows 更新源：<https://updates.septemc.com/storydex/windows/latest.json>

## 发行资产

- `StorydexSetup-x64-2.0.9.exe`：Windows x64 NSIS 安装包。
- `StorydexSetup-x64-2.0.9.exe.sig`：Tauri updater 签名文件。
- `Storydex-win-portable.zip`：Rust-only 便携包。
- `latest.json`、`SHA256SUMS.txt`、`BUILD_MANIFEST.json` 和 `DEPENDENCIES.json`：更新清单、校验值、构建信息与依赖清单。
