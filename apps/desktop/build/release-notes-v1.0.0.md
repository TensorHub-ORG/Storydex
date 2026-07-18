# Storydex v1.0.0

Storydex v1.0.0 是首个稳定版 Windows 发行。本版本在既有本地优先小说工作台、Coomi Agent、项目版本控制、知识管理与应用内更新能力之上，重点提升任务执行反馈、工作台状态可见性、配置诊断和发布一致性。

## 主要更新

- Agent 面板与浮动状态条提供更清晰的接受、执行、等待、完成和异常反馈，长任务过程更易观察。
- 资源浏览器与状态栏统一展示项目、会话和运行状态，减少状态重复、缺失或切换不及时的问题。
- 模型配置面板补充模型列表获取、配置来源和错误诊断提示，兼容服务的配置排查更直接。
- 会话回放、Coomi 模型获取和 Provider usage 记录继续加强一致性，并补充契约与回归测试。
- 新增 Storydex 官方站点、在线帮助文档、隐私说明与使用条款；下载入口会读取正式更新源的最新版本。
- 左侧知识图谱下新增“指令仓库”，可按分类检索、识别模板参数、复制提示词或直接填入 Agent；内置模板从 `docs/prompts` 动态读取。
- 新小说项目会从 `docs/skills` 初始化详细、通用且可直接执行的内置技能模板；用户修改过的同名技能会被保留。
- 安装包与便携包完整内置 `docs/guide`、`docs/prompts` 和 `docs/skills`，应用内帮助、指令仓库和项目初始化均可离线使用。

## Windows 发行资产

- `StorydexSetup-x64-1.0.0.exe`：Windows x64 NSIS 安装包。
- `StorydexSetup-x64-1.0.0.exe.blockmap`：应用内差分更新元数据。
- `Storydex-win-unpacked.zip`：免安装便携包。
- `latest.yml`：`electron-updater` 正式更新源元数据。
- `SHA256SUMS.txt`：发行文件 SHA-256 校验值。
- `BUILD_MANIFEST.json`：构建环境、提交和产物清单。
- `DEPENDENCIES.json`：Node.js 与 Python 依赖清单。

## 升级说明

- 已安装旧版本的用户可在 Storydex 的“系统设置 -> 更新与关于”中检查更新，也可下载 v1.0.0 安装包覆盖安装。
- 便携版用户请下载新的便携 ZIP 并解压到新目录，避免与旧版本文件混用。
- 项目文件、会话记录与用户配置不存放在应用安装目录中，正常覆盖安装不会删除创作数据。

Windows 更新源：<https://updates.septemc.com/storydex/windows/>
