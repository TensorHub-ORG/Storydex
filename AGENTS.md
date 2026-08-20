# Storydex Agent Rules

本文件补充全局 Agent 规则，适用于整个仓库。

## CI 与提交

- 修改前先运行 `git status --short --branch`，不得覆盖其他会话或用户的未提交改动。
- 开始仓库任务时检查 `git config --local --get core.hooksPath`。结果不是 `.githooks` 时，先运行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_git_hooks.ps1`；该操作只修改当前仓库配置，不得改全局 Git 配置。
- 提交前运行与改动直接相关的测试。准备推送时，工作区必须没有已暂存或未暂存的跟踪文件改动。
- 每次推送仍必须执行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_pre_push_ci.ps1`，但该脚本只运行编码、冲突标记、版本一致性和 whitespace 基础检查；禁止使用 `--no-verify` 绕过 hook。
- 本地 pre-push 不运行 Backend、Frontend、Desktop、Android、Rust、coverage、打包或 E2E 套件，也不再按 `HEAD` 生成门禁认证。组件测试交给 GitHub Actions，开发者只需在提交前运行与改动直接相关的聚焦测试。
- 当前仓库协作只约束 `dev/windows` 与 `main`：Windows 改动先进入 `dev/windows` 并通过 Development CI，再进入 `main` 接受完整质量门禁。其他远端分支不在本文件治理范围内，不因日常任务主动清理、同步或改写。
- `main` 与正式发布 workflow 的完整质量门禁只在 GitHub Actions 中执行；本地完整套件仅在人工明确需要时运行，不得作为每次 push 的前置条件。
- 不得在已知失败的远端 CI 基线上继续堆叠并推送提交。开始新提交前检查 `origin/main` 最近一次 CI；若失败，先定位并修复或明确说明与当前任务无关且获得用户决定。
- 推送后必须监控该 `HEAD` 对应的 GitHub Actions 到最终 `success`：`main` 使用完整 CI，`dev/windows` 使用 Development CI。失败时读取具体 job/step，修复根因并重新验证；CI 未结束或失败时不得宣称任务完成。
- 只修改文档也不得自行跳过 hook；基础检查应保持在秒级。

## 本地检查

- 标准推送基础检查入口：`scripts/run_pre_push_ci.ps1`，该命令不得调用 `scripts/run_full_test_suite.ps1`。
- `scripts/run_full_test_suite.ps1 -Mode Fast` 保留为人工完整验证入口，不由 pre-push 自动调用。
- 手动运行 `Fast` 仍表示全组件检查，并包含 Rust、Backend 全量 coverage、Frontend 和 Desktop 标准检查；不要在普通开发 push 前运行它。
- `dev/windows` 不得把轻量 CI 的成功表述为 release-ready；合入 `main` 时必须重新通过完整质量门禁。
- 本地 Python 版本不能覆盖 GitHub Actions 的 Python 版本时，必须在交付说明中注明，并以推送后的真实 CI 结果作为最终依据。
