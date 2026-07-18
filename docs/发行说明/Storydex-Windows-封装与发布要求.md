# Storydex Windows 封装与发布要求

本文规定 Storydex Windows 正式版本的版本管理、质量门禁、封装产物、GitHub Release 发布和更新源同步要求。适用于 `v1.0.0` 及后续正式版本。

## 1. 版本与分支要求

1. 正式版本使用语义化版本号 `主版本.次版本.修订号`，Git 标签必须使用 `v` 前缀，例如版本 `1.0.0` 对应标签 `v1.0.0`。
2. `apps/desktop/package.json` 中的 `version` 与 `build.extraMetadata.version` 必须一致。
3. `apps/desktop/package-lock.json` 顶层版本与根包版本必须和桌面版本一致。
4. `README.md` 必须标识当前正式版本，并包含本次版本摘要。
5. 必须存在 `apps/desktop/build/release-notes-v<版本>.md`。
6. 正式标签只能指向已经推送到远程 `main` 的发布提交；不得从未提交、未推送或测试失败的工作区创建标签。

版本一致性检查：

```powershell
node scripts/validate_version_consistency.cjs --expected=1.0.0
```

## 2. 发布前质量门禁

发布提交必须通过以下检查：

- UTF-8 文本编码、冲突标记、版本一致性和 `git diff --check`。
- 后端 Python 3.9/3.13 测试、覆盖率门禁、模块编译与导入检查。
- 前端类型检查、单元测试、覆盖率、回归测试与生产构建。
- 桌面端更新契约、发行配置、封装策略与更新辅助程序测试。
- Windows `win-unpacked` 构建、内置 Python、后端资源、MinGit、更新配置和 Electron E2E 验证。
- NSIS 安装包、blockmap、`latest.yml`、便携 ZIP、校验值、依赖清单和构建 manifest 验证。

本地正式门禁入口：

```powershell
.\scripts\run_full_test_suite.ps1 -Mode Release
```

如果仅重新验证封装流程，也必须至少执行：

```powershell
npm --prefix apps/desktop run check:encoding
npm --prefix apps/desktop run check:release
npm --prefix apps/desktop run test:update-feed
npm --prefix apps/desktop run package:win
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/prepare_release_bundle.ps1 -Version 1.0.0
```

任何命令返回非零退出码时禁止发布。

## 3. 构建环境要求

- Windows x64 构建环境。
- Node.js 20，前端和桌面端均使用锁文件执行 `npm ci`。
- Python 3.9；通过 `scripts/bootstrap_python39.ps1 -InstallRequirements` 准备可迁移的内置运行时。
- Electron、electron-builder、Python 依赖和 Storydex Coomi 运行时均以仓库锁文件与固定校验为准。
- 正式包不得包含 `.env`、密钥、证书、用户配置、日志、测试结果、coverage、pytest 缓存或其他开发期临时文件。
- 发布流程不得从工作区外临时复制未经记录的二进制或依赖。

## 4. 必需发行产物

每个 Windows 正式版本必须包含：

| 文件 | 要求 |
| --- | --- |
| `StorydexSetup-x64-<版本>.exe` | Windows x64 NSIS 安装包 |
| `StorydexSetup-x64-<版本>.exe.blockmap` | 与安装包同版本的差分更新文件 |
| `Storydex-win-unpacked.zip` | 包含 `Storydex.exe` 的便携包 |
| `latest.yml` | `version`、`path`、`size`、SHA-512 必须与安装包一致 |
| `SHA256SUMS.txt` | 覆盖发布目录内全部正式文件 |
| `RELEASE_NOTES.md` | 与版本对应的用户可读发行说明 |
| `BUILD_MANIFEST.json` | 包含 Git 提交、构建时间、运行时版本和产物摘要 |
| `DEPENDENCIES.json` | 包含前端、桌面端和 Python 依赖清单 |

安装包和便携包都必须包含可启动的桌面应用、前端生产资源、后端服务、内置 Python 运行时、固定依赖和 MinGit。便携 ZIP 解压后必须能找到 `Storydex.exe`。

以下创作资源目录必须递归完整封装，并与仓库源文件逐文件一致：

- `docs/guide`：应用内使用指南。
- `docs/prompts`：指令仓库及分类提示词模板。
- `docs/skills`：新建小说项目时使用的详细通用内置技能模板。

封装校验不得只检查目录或 `README.md` 是否存在，必须比对递归文件列表和文件内容摘要，防止分类子目录或技能模板漏包。

## 5. Git 提交、标签与 GitHub Release

1. 检查 `git status`，确认提交内容只包含本次发行需要的源码、文档和版本文件。
2. 提交发布变更并推送 `main`。
3. 在发布提交上创建带说明的标签 `v<版本>`，并推送该标签。
4. 标签推送触发 `.github/workflows/release-windows.yml`；工作流必须先通过复用质量门禁，再构建和发布。
5. GitHub Release 标题统一为 `Storydex v<版本>`，不得设置为草稿或预发布版本，除非发布计划明确要求。
6. GitHub Release 中的资产数量、文件名、大小与校验值必须和本地封装结果一致。

推荐命令：

```powershell
git push origin main
git tag -a v1.0.0 -m "Storydex v1.0.0"
git push origin v1.0.0
```

## 6. Windows 更新源同步

正式发布必须同步到：

```text
/www/wwwroot/updates.septemc.com/storydex/windows
```

公网地址：

```text
https://updates.septemc.com/storydex/windows/
```

同步顺序必须为：

1. 确保远程目录存在。
2. 先上传安装包和对应 blockmap。
3. 将新的 `latest.yml` 上传为临时文件。
4. 最后在服务器端原子重命名为 `latest.yml`，避免客户端读到半更新状态。
5. 保留上一正式版本的安装包和 blockmap，以支持差分更新与必要回退。

更新源至少必须公开：

- `StorydexSetup-x64-<版本>.exe`
- `StorydexSetup-x64-<版本>.exe.blockmap`
- `latest.yml`

同步后必须验证：

```powershell
$base = 'https://updates.septemc.com/storydex/windows'
Invoke-WebRequest -UseBasicParsing "$base/latest.yml?verify=1"
Invoke-WebRequest -UseBasicParsing -Method Head "$base/StorydexSetup-x64-1.0.0.exe"
Invoke-WebRequest -UseBasicParsing -Method Head "$base/StorydexSetup-x64-1.0.0.exe.blockmap"
```

`latest.yml` 中的 `version` 必须为 `1.0.0`，`path` 必须为 `StorydexSetup-x64-1.0.0.exe`。

## 7. 发布后验收

- GitHub Release 已公开且标签、标题、说明和资产完整。
- 更新源的 `latest.yml`、安装包和 blockmap 均返回成功状态。
- 对 `latest.yml` 声明的安装包大小和 SHA-512 重新校验。
- 使用上一正式安装版执行一次“检查更新 -> 下载 -> 安装 -> 重启”验证。
- 使用便携 ZIP 在新的目录执行冷启动，确认前端、后端、内置 Python、Coomi 和 Git 功能可用。
- 记录 Git 提交、标签、GitHub Actions 运行链接、发行资产 SHA-256 与更新时间。

## 8. 失败处理与回退

- 质量门禁或封装失败：修复后重新提交，禁止移动已经公开使用的正式标签。
- GitHub Release 创建失败：保留标签和日志，修复工作流后重新运行；不得手工上传来源不明的替代文件。
- 更新源同步失败：保持旧 `latest.yml` 不变，先补齐安装包和 blockmap，再原子切换元数据。
- 新版本存在阻断问题：将 `latest.yml` 原子恢复到上一稳定版本，并在 GitHub Release 中明确状态；后续使用新的修订版本发布，不覆盖已经发布的二进制。
- 私钥、令牌和服务器凭据只能存放在 GitHub Environment/Secrets 中，禁止写入仓库、日志、发行包或说明文档。

## 9. v1.0.0 本次发布基线

- 版本：`1.0.0`
- 标签：`v1.0.0`
- Release 标题：`Storydex v1.0.0`
- 安装包：`StorydexSetup-x64-1.0.0.exe`
- 更新目录：`/www/wwwroot/updates.septemc.com/storydex/windows`
- 更新元数据：`https://updates.septemc.com/storydex/windows/latest.yml`
- 必须内置：`docs/guide`、`docs/prompts`、`docs/skills`
