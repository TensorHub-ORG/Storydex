# Storydex

<table align="center">
  <tr>
    <td align="center" width="33%">
      <img src="assets/storydex.png" alt="Storydex 项目 LOGO" width="96" />
      <br />
      <sub><strong>Storydex</strong><br />项目 LOGO</sub>
    </td>
    <td align="center" width="33%">
      <img src="assets/coomi.png" alt="Coomi Agent 吉祥物" width="96" />
      <br />
      <sub><strong>Coomi</strong><br />Agent 吉祥物</sub>
    </td>
    <td align="center" width="33%">
      <img src="assets/tensorhub.png" alt="TensorHub 组织 LOGO" width="96" />
      <br />
      <sub><strong>TensorHub</strong><br />组织 LOGO</sub>
    </td>
  </tr>
</table>

<p align="center">
  <img alt="release" src="https://img.shields.io/badge/release-v0.3.3-2563eb?style=flat-square" />
  <img alt="license" src="https://img.shields.io/badge/license-Apache--2.0%20%2B%20Commons%20Clause-0f766e?style=flat-square" />
  <img alt="platform" src="https://img.shields.io/badge/platform-Windows-f97316?style=flat-square" />
  <img alt="desktop" src="https://img.shields.io/badge/desktop-Electron%2034-47848f?style=flat-square&logo=electron&logoColor=white" />
  <img alt="frontend" src="https://img.shields.io/badge/frontend-Vue%203%20%2B%20Vite%205-42b883?style=flat-square&logo=vuedotjs&logoColor=white" />
  <img alt="backend" src="https://img.shields.io/badge/backend-FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white" />
  <img alt="python" src="https://img.shields.io/badge/python-3.9-3776ab?style=flat-square&logo=python&logoColor=white" />
  <img alt="agent" src="https://img.shields.io/badge/agent-Coomi-7c3aed?style=flat-square" />
  <img alt="local first" src="https://img.shields.io/badge/local--first-novel%20workspace-334155?style=flat-square" />
  <img alt="version control" src="https://img.shields.io/badge/version%20control-MinGit-22c55e?style=flat-square&logo=git&logoColor=white" />
</p>

Storydex 是一个面向长篇小说创作的本地优先写作工作台。它把正文编辑、项目文件管理、Coomi Agent、版本控制、预设管理、知识图谱和使用指南放在同一个桌面应用里，让 AI 参与创作时保持可观察、可审阅、可回滚。

<p align="center">
  <a href="docs/assets/readme/storydex-workbench-review.png">点击查看 2098 × 1223 完整分辨率工作台截图</a>
</p>

<p align="center">
  <a href="docs/assets/readme/storydex-workbench-review.png">
    <img src="docs/assets/readme/storydex-workbench-editor-detail.png" alt="Storydex 项目浏览器、编辑器与差异审阅界面细节" width="100%" />
  </a>
</p>

<p align="center">
  <a href="docs/assets/readme/storydex-workbench-review.png">
    <img src="docs/assets/readme/storydex-workbench-coomi-detail.png" alt="Storydex Coomi 助手界面细节" width="718" />
  </a>
</p>

## 核心能力

- **小说项目工作台**：统一管理章节、设定、角色、WIKI、世界观和项目资源。
- **Coomi Agent**：理解你本轮的创作意图，围绕当前 Storydex 项目读取上下文，进行续写、整理、审阅、生成和工具调用。
- **检索与记忆**：面向中文优化的项目全文检索、滚动章节摘要、相关旧文召回和 WIKI 参考注入，让 Agent 在长篇续写中先查证再落笔。
- **版本控制**：内置 Git / MinGit 工作流，支持本轮修改审阅、Diff 查看和历史回看。
- **创作预设**：维护写作约束、风格规则、导入规则和默认章节目录。
- **知识图谱与 WIKI**：把角色、事件、关系、地点和设定组织成可检索的结构化资料。
- **桌面体验**：提供多主题界面、资源浏览器、编辑区、Agent 面板和项目级使用指南，支持应用内差分更新。

## 快速开始

```powershell
npm --prefix apps/frontend install
npm --prefix apps/desktop install
pip install -r requirements.txt
```

复制 `.env.sample` 为 `.env`，按需填写模型服务配置，然后启动：

```powershell
.\start-storydex.bat
```

也可以分别启动桌面端或完整开发环境：

```powershell
.\start-desktop.bat
.\scripts\run_fullstack_dev.bat
```

## 项目结构

```text
Storydex/
├─ apps/frontend/        # Vue 工作台
├─ apps/backend/         # FastAPI 后端服务
├─ apps/desktop/         # Electron 桌面壳
├─ assets/               # 项目 LOGO、吉祥物与组织 LOGO
├─ docs/使用指南/         # 内置使用指南
├─ docs/assets/readme/   # README 展示图
├─ scripts/              # 开发与启动脚本
└─ start-storydex.bat    # 一键启动入口
```

## 文档

- [使用指南](docs/使用指南/README.md)
- [项目架构说明](docs/项目架构说明.md)

## 许可证

本项目采用 Apache License 2.0 + Commons Clause 许可证组合。源码可用于个人学习、研究和教学等非商业用途；商业使用、SaaS 托管、付费服务、二次分发或对外提供衍生版本前，需要获得单独书面授权。

商业授权请联系：septemc@foxmail.com。详细条款请阅读 [LICENSE](LICENSE) 与 [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md)。

## 作者与版权

Copyright 2026 Septemc and Flowby.

## v0.3.3

v0.3.3 修复了离线 Material Symbols 图标在冷启动或字体加载失败时显示为空白的问题，并为字体加载增加确定性重试、超时状态和可见文本降级。Coomi 现在会在请求进入后立即发送 `RunAccepted`，随后持续发送带耗时的 `TurnPhase` 和 heartbeat，因此意图识别、上下文装配或模型等待期间不会长期停留在没有过程反馈的“执行中”。

Coomi/OpenAI provider 的首次导入和客户端初始化已从 SSE 请求事件循环中隔离，冷启动时也不会阻塞阶段计时。打包态自动化实测首个 SSE 通常在 10ms 内到达，heartbeat 按约 600ms 周期继续刷新。

会话恢复改为 Storydex 项目、Storydex sessionId 与 Coomi JSONL 历史的隔离映射。重新启动应用后，原会话可继续读取上一轮用户、助手、工具与待执行动作；“执行”等省略指令会承接上一轮确认语义，不会仅因活动文件位于 `chapters/` 而误触发新剧情生成。

同时修复了 AgentPanel 启动阶段的历史读取竞态：较晚返回的旧历史请求不会再覆盖正在运行的新会话，也不会导致末尾 Git 提交提示丢失。

本地 Git 确认面板会在点击自动提交、手动提交或跳过后立即收起并显示操作状态。跳过不再重新扫描完整仓库，提交说明生成有 2 秒超时和本地确定性回退。该功能只在小说项目中创建本地 Git 版本，**不会自动配置远程仓库，也不会自动 push**。

## 开发与测试

安装测试依赖后，可使用统一 PowerShell 入口：

```powershell
pip install -r apps/backend/requirements-test.txt
npm ci --prefix apps/frontend
npm ci --prefix apps/desktop
.\scripts\run_full_test_suite.ps1 -Mode Fast
.\scripts\run_full_test_suite.ps1 -Mode Full
.\scripts\run_full_test_suite.ps1 -Mode Release
```

- `Fast`：编码、冲突标记、版本、Python 编译、后端 pytest/覆盖率、前端类型/Vitest/回归/构建、桌面单元与发布配置检查。
- `Full`：在 Fast 基础上构建 `win-unpacked`，验证前端字体、后端、嵌入式 Python、MinGit 与更新配置，并运行隔离用户目录的 Electron E2E。
- `Release`：在 Full 基础上生成并验证 NSIS installer、blockmap、`latest.yml` 和校验文件；任何阶段失败都会返回非零退出码。

测试代码分别位于 `apps/backend/tests`、`apps/frontend/tests` 和 `apps/desktop/tests`。后端覆盖 unit、API contract、integration、security、SSE 性能、会话恢复和并发失败恢复；前端覆盖 SSE parser、Pinia store、AgentPanel 与字体状态机；桌面覆盖 Node 契约、打包资源、Electron 冷启动和更新元数据。所有自动化测试使用临时 HOME、临时项目和 fake/mock provider，不访问真实付费 LLM 或用户配置。

`.github/workflows/ci.yml` 在 PR、main push 和手动触发时调用可复用的 `quality-gate.yml`。质量门禁覆盖 Python 3.9（Windows/Ubuntu）、Python 3.13 Windows 兼容性、Node 20、Windows 打包与 Electron E2E，并上传 JUnit、覆盖率和失败诊断产物。发布工作流必须先通过同一质量门禁。

## Windows 安装、便携包与应用内更新

v0.3.3 发布资产包括 NSIS 安装包、`Storydex-win-unpacked.zip` 便携包、blockmap、`latest.yml`、SHA256 校验、发布说明、依赖清单和构建 manifest。安装包与便携包均内置可迁移的 Python 3.9 运行环境、后端依赖和 MinGit，用户无需另外安装 Python 或 Git 即可启动后端并使用小说项目本地版本管理。

发布构建会执行嵌入式 Python import/preflight、MinGit 文件检查和完整打包资源扫描；测试目录、coverage 文件、pytest 缓存、日志、`.env` 和其他开发期临时文件不会进入正式包。应用内更新使用 `electron-updater` 的 generic feed；安装版可使用 blockmap 进行差分下载，便携包适合解压后直接启动和人工验证。

## Desktop Update Feed

Windows desktop releases use the generic electron-updater feed at:

```text
https://updates.septemc.com/storydex/windows/
```

For differential-update testing, publish both `0.3.2` and `0.3.3` installer assets to the update directory, then leave `latest.yml` pointing to `0.3.3`. Install the `0.3.2` package first, then check for updates inside Storydex to verify the `0.3.2 -> 0.3.3` update path.
