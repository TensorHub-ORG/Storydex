# Storydex v0.1.1 Windows 修复发行

本次发行修复 Windows 安装包和桌面启动相关问题，建议已安装 v0.1.0 的用户升级。

## 修复内容

- 修复安装向导许可协议页面中文乱码问题。
- 确认打包版内置 Python 运行时已在发布流程中做可迁移校验，避免后端启动时报 `No Python at C:\hostedtoolcache\...`。
- 发布流程继续在打包前校验文本编码、release 配置和内置 Python 预检结果。

## 下载内容

- `StorydexSetup-x64-0.1.1.exe`：Windows x64 安装包。
- `Storydex-win-unpacked.zip`：免安装可执行目录压缩包，解压后运行 `Storydex.exe`。
- `SHA256SUMS.txt`：发行文件校验值。

## 校验建议

发布前请确认安装包许可协议可正常显示中文，并在全新 Windows 环境中启动 `Storydex.exe`，确认本地后端健康检查通过。
