# Storydex Agent Rules

本文件补充全局 Agent 规则，适用于整个仓库。

## CI 与提交

- 修改前先运行 `git status --short --branch`，不得覆盖其他会话或用户的未提交改动。
- 开始仓库任务时检查 `git config --local --get core.hooksPath`。结果不是 `.githooks` 时，先运行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_git_hooks.ps1`；该操作只修改当前仓库配置，不得改全局 Git 配置。
- 提交前运行与改动直接相关的测试。准备推送时，工作区必须没有已暂存或未暂存的跟踪文件改动。
- 每次推送仍必须执行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_pre_push_ci.ps1`，但该脚本只运行编码、冲突标记、版本一致性和 whitespace 基础检查；禁止使用 `--no-verify` 绕过 hook。
- 本地 pre-push 不运行 Backend、Frontend、Desktop、Android、Rust、coverage、打包或 E2E 套件，也不再按 `HEAD` 生成门禁认证。组件测试交给 GitHub Actions，开发者只需在提交前运行与改动直接相关的聚焦测试。
- 当前仓库协作只约束 `dev/windows` 与 `main`：Windows 改动先进入 `dev/windows` 并通过 Development CI；相同 SHA 进入 `main` 时可复用已成功的 Windows 组件结果，只补跑 main 的基础策略检查。无法确认同 SHA 成功结果、或包含非 Windows 所属路径时，仍按变更范围执行组件检查。完整跨平台质量门禁仅用于显式 full CI 或人工明确触发的验证。其他远端分支不在本文件治理范围内，不因日常任务主动清理、同步或改写。
- 普通 push 和 pull request 使用路径范围检查；完整跨平台质量门禁只在显式 full CI 或人工明确触发时执行。Windows 正式发布使用 Windows 专项门禁和最终产物校验，不再重复 Android、旧后端兼容矩阵或 GUI E2E。本地完整套件仅在人工明确需要时运行，不得作为每次 push 的前置条件。
- 普通 push 和 pull request 不运行打包或 GUI E2E。打包资产检查仅在手动 `packaged=true` 或正式发布 workflow 中运行；Tauri GUI E2E 不再作为 CI 或发布门禁。
- 不得在已知失败的远端 CI 基线上继续堆叠并推送提交。开始新提交前检查 `origin/main` 最近一次 CI；若失败，先定位并修复或明确说明与当前任务无关且获得用户决定。
- 普通 push 后必须确认该 `HEAD` 对应的 GitHub Actions run 已创建，并默认监控最多 15 分钟。期间失败时读取具体 job/step，修复根因并重新验证；达到时限仍在排队或运行时，报告 run URL 和当前状态即可结束本次交付，但不得宣称 CI 已成功。
- 显式 full CI、正式发布 workflow，或用户明确要求持续等待时，必须监控对应 run 到最终 `success`；失败时不得宣称任务完成。
- 只修改文档也不得自行跳过 hook；基础检查应保持在秒级。

## 本地检查

- 标准推送基础检查入口：`scripts/run_pre_push_ci.ps1`，该命令不得调用 `scripts/run_full_test_suite.ps1`。
- `scripts/run_full_test_suite.ps1 -Mode Fast` 保留为人工完整验证入口，不由 pre-push 自动调用。
- 手动运行 `Fast` 仍表示全组件检查，并包含 Rust、Backend 全量 coverage、Frontend 和 Desktop 标准检查；不要在普通开发 push 前运行它。
- `dev/windows` 不得把轻量 CI 的成功表述为 release-ready；需要完整跨平台验收时显式运行 full CI，需要验证安装包时显式运行 packaged 检查或发布 dry-run。
- 本地 Python 版本不能覆盖 GitHub Actions 的 Python 版本时，必须在交付说明中注明，并以推送后的真实 CI 结果作为最终依据。
