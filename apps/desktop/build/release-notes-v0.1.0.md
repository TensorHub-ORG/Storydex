# Storydex v0.1.0 Windows 正式发行

本次发行提供 Windows 桌面版 Storydex，采用 Electron + 内置 Python 环境方案，并随包内置 MinGit。用户无需额外安装 Python 或 Git 即可启动本地写作工作台和版本控制能力。

## 下载内容

- `StorydexSetup-x64-0.1.0.exe`：Windows x64 安装包。
- `Storydex-win-unpacked.zip`：免安装可执行目录压缩包，解压后运行 `Storydex.exe`。
- `SHA256SUMS.txt`：发行文件校验值。

## 封装内容

- Electron 桌面应用。
- Vue 3 前端工作台构建产物。
- FastAPI 本地后端服务。
- 内置 Python 3.9 运行环境和依赖。
- 内置 MinGit，用于 Storydex 项目版本控制。
- `docs/使用指南`，用于应用内阅读和 Agent 回答使用问题时参考。

## 主要能力

- 本地优先的小说创作项目管理。
- Markdown 正文编辑与资源浏览。
- Coomi Agent 面板与项目上下文协作。
- 版本控制、本轮修改审阅和历史回看。
- 预设管理、知识图谱与 WIKI。
- 内置使用指南与帮助菜单。

## 安装与使用

安装包会在安装前显示 Storydex 软件许可与使用协议。Storydex 源码开放且免费提供给个人学习、研究、教学、非商业创作和非商业评估使用；未经权利人单独书面授权，不得用于商业目的，不得贩卖本软件、安装包、修改版或衍生版本。

确认协议后，可以自定义安装目录。安装完成后可从桌面快捷方式或开始菜单启动 Storydex。

免安装包解压后，进入 `Storydex-win-unpacked` 目录运行 `Storydex.exe`。

## 许可与商业授权

Storydex 采用 Apache License 2.0 + Commons Clause 许可证组合。源码可用于非商业目的。商业使用、SaaS 托管、付费服务、二次开发后发布或对外提供衍生版本前，需要获得单独书面授权。

商业授权联系邮箱：septemc@foxmail.com

## 校验

发布前已完成以下检查：

- 前端构建通过。
- 桌面可执行目录和安装包构建通过。
- 可执行目录包含 `docs/使用指南`。
- 可执行目录包含内置 Python 3.9 运行环境。
- 可执行目录包含内置 MinGit。
- 应用资源目录未发现 `.env`、日志、缓存、测试目录等开发运行态文件。
