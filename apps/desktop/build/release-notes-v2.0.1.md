# Storydex v2.0.1

Storydex v2.0.1 是面向 Windows 安装体验和发行流程的维护版本。

## 本次修复

- 修复安装引导在允许普通用户安装时可能重新启动提权实例、导致许可协议页面出现两次的问题。
- 关闭 NSIS 安装器自动提权，安装按钮不再显示管理员权限盾牌图标，避免图标与按钮文字错位。
- 同步桌面端版本、更新元数据和 Windows 发行产物命名。

## 安装与更新

- `StorydexSetup-x64-2.0.1.exe`：Windows x64 安装包。
- `StorydexSetup-x64-2.0.1.exe.blockmap`：应用内差分更新数据。
- `Storydex-win-unpacked.zip`：便携版应用目录压缩包。
- `latest.yml`、`SHA256SUMS.txt`、`BUILD_MANIFEST.json` 和 `DEPENDENCIES.json`：更新与校验元数据。

安装包为当前用户安装，不会要求管理员权限。若目标目录本身不可写，请选择用户目录下的安装位置。
