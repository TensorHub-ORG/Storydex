# Storydex Agent Rules

本文件补充全局 Agent 规则，适用于整个仓库。

## CI 与提交

- 修改前先运行 `git status --short --branch`，不得覆盖其他会话或用户的未提交改动。
- 开始仓库任务时检查 `git config --local --get core.hooksPath`。结果不是 `.githooks` 时，先运行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_git_hooks.ps1`；该操作只修改当前仓库配置，不得改全局 Git 配置。
- 提交前运行与改动直接相关的测试。准备推送时，工作区必须没有已暂存或未暂存的跟踪文件改动。
- 每个新提交在推送前必须通过 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_pre_push_ci.ps1`。该命令按 `HEAD` 记录成功结果；禁止使用 `--no-verify` 绕过 pre-push hook。
- `run_pre_push_ci.ps1` 默认比较当前 `HEAD` 与 upstream 的 merge-base，并复用远端 `resolve_ci_scope.cjs` 只运行受影响组件；文档提交只跑 source policy。没有 upstream、变更路径未知或 CI 编排脚本发生变化时必须保守执行全量 `Fast`。
- 不得在已知失败的远端 CI 基线上继续堆叠并推送提交。开始新提交前检查 `origin/main` 最近一次 CI；若失败，先定位并修复或明确说明与当前任务无关且获得用户决定。
- amend、rebase、cherry-pick、历史拆分或其他会改变 `HEAD` SHA 的操作会使旧预检失效，必须对新 `HEAD` 重新执行预检。
- 推送后必须监控该 `HEAD` 的 GitHub Actions 到最终 `success`。失败时读取具体 job/step，修复根因并重新验证；CI 未结束或失败时不得宣称任务完成。
- 只修改文档也不得自行跳过 hook。预检脚本负责复用同一 `HEAD` 的成功认证，是否缩小远端检查范围由仓库 CI 决定。

## 本地门禁

- 标准推送门禁入口：`scripts/run_pre_push_ci.ps1`。
- 标准 CI 套件入口：`scripts/run_full_test_suite.ps1 -Mode Fast`。
- 直接调用 `run_full_test_suite.ps1 -Mode Fast` 仍表示全组件检查；组件裁剪由 pre-push 通过 `-Scope` 显式传入，不能根据未提交工作区自行猜测。
- `Fast` 表示不执行桌面打包与安装包 E2E，不表示只运行聚焦测试；它仍包含 Rust、Backend 全量 coverage、Frontend 和 Desktop 标准检查。
- 本地 Python 版本不能覆盖 GitHub Actions 的 Python 版本时，必须在交付说明中注明，并以推送后的真实 CI 结果作为最终依据。
