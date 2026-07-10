# Storydex v0.3.1 Windows 修复版

Storydex v0.3.1 修复桌面版应用内更新入口在部分安装包中缺少更新源配置的问题。

## 修复

- 修复点击「系统设置 -> 更新与关于 -> 检查更新」时可能报错 `ENOENT: no such file or directory, open ...\\resources\\app-update.yml` 的问题。新版客户端会在运行时显式设置更新源，不再单独依赖 `resources/app-update.yml`。
- 修复 Windows 安装包未携带 `resources/app-update.yml` 的问题。后续安装包会在打包阶段写入更新源配置，指向 `https://updates.septemc.com/storydex/windows/`。

## 下载内容

- `StorydexSetup-x64-0.3.1.exe`：Windows x64 安装包，推荐大多数用户下载。
- `Storydex-win-unpacked.zip`：免安装版本，解压后运行 `Storydex.exe`。
- `SHA256SUMS.txt`：发布文件校验值。

以下文件用于应用内更新，不需要手动打开：

- `latest.yml`
- `StorydexSetup-x64-0.3.1.exe.blockmap`

## 升级说明

- 如果已安装的 v0.3.0 点击检查更新时报缺少 `app-update.yml`，请直接下载 v0.3.1 安装包覆盖安装一次。安装 v0.3.1 后，后续版本即可继续通过「系统设置 -> 更新与关于」进行应用内差分更新。
- Storydex 不会主动删除用户项目；升级前仍建议确认重要写作项目已经备份或提交到版本控制。
