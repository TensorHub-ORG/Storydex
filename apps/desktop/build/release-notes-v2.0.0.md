# Storydex v2.0.0

Storydex v2.0.0 是 Agent 基座与创作工作台的完整升级版本。本次发行将 Coomi Rust 运行时直接内置到 Storydex，并完善搜索、编辑、项目结构和本地版本控制，降低长篇创作中的上下文切换与运行环境差异。

## 主要更新

- Agent 基座升级为 Storydex 专用 `storydex-coomi-bridge` Rust 运行时，移除旧 Python Coomi wheel，并修复 Windows Agent 面板的子进程兼容问题。
- Coomi 上下文窗口新增 128K、256K、512K 和自定义选项，默认统一为 256K；执行异常会展示可定位的详细信息。
- 全文搜索扩展到整个项目，支持中文短语、常用文本和源码格式；搜索控件、清除按钮和筛选项布局同步优化。
- 文件内查找支持全部匹配高亮、当前项强调以及上下结果定位，查找组件使用不透明背景并避开编辑器顶栏。
- 资源管理器补齐文件与文件夹多选、连续选择和批量拖动，Agent 输出中的项目文件直接在中间编辑区打开。
- 项目可选择规范架构或自由架构；规范项目还能切换为剧情、角色、世界背景、剧本、预设和模板分类视图。
- 编辑器新增 10 种字体样式并修复字体缩放与输入焦点稳定性，新建项目窗口和版本控制控件完成视觉整理。
- Git 分支管理支持创建与切换，空仓库可在首个提交前建立目标分支；提交错误信息和运行缓存忽略规则更加明确。
- 正式包内置前端、后端、Python 3.9、MinGit、使用指南、提示词、技能模板和新的 Coomi Rust bridge，并执行逐项封装校验。

## Windows 发行资产

- `StorydexSetup-x64-2.0.0.exe`：Windows x64 NSIS 安装包。
- `StorydexSetup-x64-2.0.0.exe.blockmap`：应用内差分更新元数据。
- `Storydex-win-unpacked.zip`：免安装便携包。
- `latest.yml`：`electron-updater` 更新源元数据。
- `SHA256SUMS.txt`：正式发行文件 SHA-256 校验值。
- `BUILD_MANIFEST.json`：构建环境、提交和产物清单。
- `DEPENDENCIES.json`：Node.js、Python 和运行时依赖清单。

## 升级说明

- 已安装旧版本的用户可在“系统设置 -> 更新与关于”中检查更新，也可以下载安装包覆盖升级。
- 便携版用户应解压到新目录，避免与旧版本文件混用。
- 项目文件、会话记录和用户 Provider 配置位于应用安装目录之外，正常升级不会删除创作数据。

Windows 更新源：<https://updates.septemc.com/storydex/windows/>
