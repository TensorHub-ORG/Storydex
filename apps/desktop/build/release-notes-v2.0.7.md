# Storydex v2.0.7

Storydex v2.0.7 是 Windows 桌面端 Tauri 2 的修复版本，重点修复 NSIS 安装器中文许可协议显示乱码的问题。

## 修复内容

- 将 NSIS 许可证源文件统一为 UTF-8 无 BOM，修复安装过程中“Storydex 软件许可与使用协议”显示乱码。
- 增加许可证编码回归检查，避免后续打包再次把 UTF-16 内容写入 NSIS。

## 升级说明

- v2.0.6 及更早版本请手动下载完整安装包 `StorydexSetup-x64-2.0.7.exe`，按安装器提示覆盖安装。
- 本次升级会替换程序文件，不会主动删除项目目录或用户数据；仍建议安装前备份重要项目。
- 应用内更新的 Tauri → Tauri 下载、重启和安装链路仍在实机验证中，暂不将其作为已保证的升级方式。
- Windows 更新源：<https://updates.septemc.com/storydex/windows/latest.json>

## 发行资产

- `StorydexSetup-x64-2.0.7.exe`：Windows x64 NSIS 安装包。
- `StorydexSetup-x64-2.0.7.exe.sig`：Tauri updater 签名文件。
- `Storydex-win-portable.zip`：Rust-only 便携包。
- `latest.json`、`SHA256SUMS.txt`、`BUILD_MANIFEST.json` 和 `DEPENDENCIES.json`：更新清单、校验值、构建信息与依赖清单。
