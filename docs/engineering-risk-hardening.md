# 工程风险加固记录

审计基线：`91caf7d10186e3d5a879b25ee4c0823ee210698a`（2026-07-31）。本记录只覆盖工程边界、构建、依赖和测试门禁，不改变 WIKI 增量同步或知识图谱布局的产品算法。

## 已完成

### Git 仓库边界

- Git 服务在每次内部命令前解析 `rev-parse --show-toplevel`，并用真实路径、规范化大小写和 worktree 语义与当前 Storydex 项目根比较。
- 项目位于父仓库内部但自身不是仓库根时明确返回 `git_service_error`，不会 fallback 到父仓库；暂存、提交、状态、diff、分支和恢复均受同一边界检查保护。
- 文件参数拒绝绝对路径、盘符路径、`..` 越界和解析到项目外的符号链接；worktree `.git` 文件和 Windows 大小写路径有集成覆盖。
- Git 服务集成测试 11 个通过；工作区 API 还覆盖了父仓库错误 envelope、trace、无 staged 文件和无提交副作用。

### 覆盖率 ratchet

- `scripts/check_coverage.cjs` 是普通 CI、发布 CI 和本地命令共用的唯一校验器；缺报告、非法 JSON、缺字段、测试命令非零均 fail closed。
- `coverage-baseline.json` 保存版本基线。普通 PR/push 使用 `advisory` 模式：覆盖率下降会生成可见 warning，但不会把已通过的功能测试、类型检查和构建判红；发布 CI 使用零容差 `release` 模式，继续执行硬性 ratchet。
- `ci` 模式保留给本地提交前硬性复核，并允许配置的测量误差；基线不会由 CI 自动降低，任何口径迁移必须在本文件记录原因。
- 当前验证：前端 254/254 测试通过；普通 CI 会报告当前覆盖率与版本基线的差异，发布前仍需补足测试或按正式口径审查并更新基线。

### Electron 与打包

- Electron 升级到 40.10.6，electron-builder 升级到 26.15.3，保留最小兼容范围；已消除 Electron 运行时 high、builder 旧版 tar critical 和 js-yaml high。
- `asar: true` 已启用。前端、主进程、preload 和生产 Node 依赖进入 `app.asar`；Python、后端、文档、MinGit、图标和更新 helper 仅按需要进入 `app.asar.unpacked`。
- 打包同步会从复用的 Python 环境排除 pytest、coverage、hypothesis、iniconfig、pluggy 等测试发行包；最终资产扫描还会检查 archive/unpacked 中的缓存、测试结果、环境文件、私钥头和其他凭证文件。公共 CA `.pem` 证书按内容识别，不会误删 TLS 运行时证书。
- 实际 `win-unpacked` 资产验证通过，Electron E2E 3/3 通过：updater 从 asar 加载、后端端口避让、流式交互、会话恢复、项目隔离和退出清理均正常。

## 依赖审计与剩余风险

### npm

审计命令：`npm audit --omit=dev --json` 与 `npm audit --json`，分别在 `apps/frontend`、`apps/desktop` 执行。两个生产树当前均为 0 high/critical。

前端完整开发树仍报告 6 个 high，路径均为测试工具链，不进入最终桌面产物：

1. `@vue/test-utils`（直接开发依赖） -> `js-beautify` -> `editorconfig`/`glob` -> `minimatch` -> `brace-expansion`。
2. `js-beautify`。
3. `editorconfig`。
4. `glob`。
5. `minimatch`。
6. `brace-expansion`。

审计建议把 `@vue/test-utils` 降到 2.4.0；该版本会破坏当前依赖 Vue `defineExpose` 的测试行为，已用 2.4.11 保持兼容。临时缓解是将该链限制在开发安装并禁止进入生产 bundle；后续条件是上游发布同时修复链且通过现有 211 个前端测试。

桌面完整构建树仍报告 16 个 high，均来自 electron-builder 26.15.3 的构建链，不进入最终 `app.asar` 或 `app.asar.unpacked`：

1. `@electron/asar` -> `glob`/`minimatch`。
2. `@electron/universal` -> `@electron/asar`/`dir-compare`/`minimatch`。
3. `app-builder-lib` -> `@electron/asar`/`@electron/universal`/`dmg-builder`/`ejs`/`electron-builder-squirrel-windows`。
4. `brace-expansion` -> 多个 `minimatch` 实例。
5. `dir-compare` -> `minimatch`。
6. `dmg-builder` -> `app-builder-lib`。
7. `ejs` -> `jake`。
8. `electron-builder`（直接开发依赖） -> `app-builder-lib`/`dmg-builder`。
9. `electron-builder-squirrel-windows` -> `app-builder-lib`/`electron-winstaller`。
10. `electron-winstaller` -> `@electron/asar`/`temp`。
11. `filelist` -> `minimatch`。
12. `glob` -> `minimatch`。
13. `jake` -> `filelist`。
14. `minimatch` -> `brace-expansion`。
15. `rimraf` -> `glob`。
16. `temp` -> `rimraf`。

npm 建议降级到 electron-builder 22.14.13，会回到已知的旧 tar critical/high，不能采用。临时缓解是只用可信源码和同步后的明确资源运行构建，并由 `check:packaged`、签名校验和 E2E 共同约束输入；后续条件是 electron-builder 上游提供不回退安全性的构建链修复。

### Python

生产锁已从 FastAPI 0.115.2/Starlette 0.40.0 升级到 FastAPI 0.128.8/Starlette 0.49.3，并将 idna 升级到 3.18；生产和测试安装现在都使用带哈希 lock。`pip-audit -r requirements.lock` 仍报告：

- `click` 8.1.8，修复版本 8.3.3；项目支持的 Python 3.9 解析结果仍只能使用 8.1.8。
- `starlette` 0.49.3，报告的修复版本为 1.0.1/1.1.0/1.3.x；这些版本超出当前 FastAPI/Python 3.9 兼容范围，不能直接升级。
- `python-dotenv` 1.2.1，修复版本 1.2.2；这是 Python 3.9 可用的最新兼容版本，仍需等待上游提供 3.9 可用的修复版本。
- `requests` 2.32.5，修复版本 2.33.0；该版本要求 Python 3.10，不能在当前嵌入式 3.9 runtime 中替换。
- `urllib3` 2.6.3，修复版本 2.7.0；同样受 Python 3.9 runtime 约束。

测试锁另有 `pytest` 8.4.2 的报告（修复版本 9.0.3），它只在 `requirements-test.lock` 中，不进入生产 runtime。缓解措施是保持 Python 3.9 运行时最小化、使用哈希锁、避免不可信输入触发上述库的高风险解析路径，并在升级嵌入式 Python 到 3.10+ 时重新评估全部五个生产包。

## CI 与可复现性

- `checkout`、`setup-node`、`setup-python`、artifact 和 release action 均固定到 commit SHA，并保留版本注释；普通 CI 权限为 `contents: read`，发布 job 才需要 `contents: write`。
- workflow 没有 `continue-on-error`；质量汇总 job 检查所有依赖 job 必须为 `success`。报告上传使用 `if: always()` 只用于保留诊断，不改变门禁结果。
- npm 使用 lockfile 的 `npm ci`；Python 生产和测试均分别使用 `--require-hashes` lock。`requirements-test.txt` 仅作为源输入，CI 和本地测试使用 `requirements-test.lock`。
- 仓库忽略构建产物、Playwright/test-results、coverage、缓存、日志、`.env` 和凭证扩展名；打包同步和最终扫描提供第二道边界。

## 保留的未处理风险

1. WIKI 增量同步仍可能全量扫描或完整重建；本轮没有修改该产品性能模块。
2. `apps/frontend/src/utils/forceLayout.ts` 在大型知识图谱下仍存在 O(N²) 和主线程卡顿风险；本轮没有修改该产品性能模块。

这两项需要后续产品性能专项和基准数据，不应通过本轮工程门禁变更掩盖。
