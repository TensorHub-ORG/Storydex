# Storydex 仓库 Agent 规则

本文件适用于整个仓库。子目录中的 `AGENTS.md` 可补充更具体的目录规则。
这里只记录项目级约束；个人偏好和本机工具规则放在用户级配置。

## 修改与安全

- 修改前运行 `git status --short --branch`，不得覆盖无关的用户改动。
- 开始仓库任务时检查 `git config --local --get core.hooksPath`；结果不是 `.githooks` 时运行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_git_hooks.ps1`，只修改当前仓库配置。
- 不得未经确认执行删除、`reset --hard`、强制推送或全局 Git 配置修改。

## 测试、提交与推送

- 提交前运行与改动直接相关的聚焦测试。
- 推送前运行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_pre_push_ci.ps1`；不得使用 `--no-verify`。
- 该脚本只做轻量基础检查，不运行 Backend、Frontend、Desktop、Android、Rust、coverage、打包或 E2E，也不等同于完整跨平台验证；`scripts/run_full_test_suite.ps1 -Mode Fast` 仅在人工明确需要时运行。
- 普通 push 和 pull request 不运行打包或 GUI E2E，按变更路径执行组件 CI；打包资产和完整跨平台门禁只在显式要求时运行。
- 只修改文档时也不能跳过 hook；基础检查应保持在秒级。

## 分支与 CI

- 只治理 `dev/windows` 和 `main`：Windows 改动先在 `dev/windows` 通过 Development CI，再以相同 SHA 进入 `main`；无法复用成功结果或包含非 Windows 路径时按变更范围检查。其他远端分支不在本文件治理范围内，不因日常任务主动清理、同步或改写。
- 开始新提交前检查 `origin/main` 最近一次 CI；已知失败基线先处理，或取得用户决定后再继续。
- 普通推送后确认当前 SHA 的 GitHub Actions run 已创建，默认监控最多 15 分钟；失败时读取具体 job/step，达到时限仍未完成时报告 run URL 和当前状态，不得宣称成功。
- 显式 full CI、正式发布或用户明确要求持续等待时，监控到最终 `success`。
- 本地 Python 版本与 GitHub Actions 不一致时，在交付说明中注明，以远端 CI 为最终依据。
