# Storydex 封装与发布要求

本文规定 Storydex 各平台正式版本的版本管理、质量门禁、封装产物、GitHub Release 发布和更新源同步要求，适用于所有后续发行版本。当前覆盖平台：

- **Windows 桌面版**：`apps/desktop`（Electron + electron-builder）
- **Android APK 版**：`apps/android`（Gradle + Termux 基座）+ `apps/android-frontend`（移动端 Vue）

## 1. 版本与分支要求

### 1.1 通用规则

1. 正式版本使用语义化版本号 `主版本.次版本.修订号`，Git 标签必须使用 `v` 前缀，例如桌面版 `2.0.4` 对应标签 `v2.0.4`。
2. Windows 与 Android 使用各自独立的版本号序列：桌面版延续 `2.x`，Android 测试版延续 `0.1.x`。
3. 正式标签只能指向已经推送到远程 `main` 的发布提交；不得从未提交、未推送或测试失败的工作区创建标签。

### 1.2 Windows 桌面版

1. `apps/desktop/package.json` 中的 `version` 与 `build.extraMetadata.version` 必须一致。
2. `apps/desktop/package-lock.json` 顶层版本与根包版本必须和桌面版本一致。
3. `README.md` 必须标识当前正式版本（release 徽章与版本摘要），并包含本次版本摘要。
4. 必须存在 `apps/desktop/build/release-notes-v<版本>.md`。

版本一致性检查（例如）：

```powershell
node scripts/validate_version_consistency.cjs --expected=2.0.4
```

### 1.3 Android APK 版

1. `apps/android/app/build.gradle` 的 `defaultConfig.versionCode` 与 `versionName` 必须与本次发布一致（versionCode 单调递增，versionName 使用语义化版本）。
2. `apps/website-overlay/storydex-android-download-v<版本>.js` 必须存在，且 `ANDROID_URL` 指向本次 APK 的更新源地址。
3. `.github/workflows/deploy-android.yml` 的默认输入（`tag`、`version`、`asset_name`）应更新为本次发布值。
4. APK 更新源目录：`/www/wwwroot/updates.septemc.com/storydex/android`。

## 2. 发布前质量门禁

发布提交必须通过以下检查：

### 2.1 Windows 桌面版

- UTF-8 文本编码、冲突标记、版本一致性和 `git diff --check`。
- 后端 Python 3.9/3.13 测试、覆盖率门禁、模块编译与导入检查。
- 前端类型检查、单元测试、覆盖率、回归测试与生产构建。
- 桌面端更新契约、发行配置、封装策略与更新辅助程序测试。
- Windows `win-unpacked` 构建、内置 Python 真实健康检查、后端资源、MinGit、更新配置和 Electron E2E 验证。
- NSIS 安装包、blockmap、`latest.yml`、便携 ZIP、校验值、依赖清单和构建 manifest 验证。

本地正式门禁入口：

```powershell
.\scripts\run_full_test_suite.ps1 -Mode Release
```

本地门禁采用分级策略：`Full` 生成目录包并运行 updater/端口避让快速 smoke；`Release` 只执行一次正式 NSIS pack，并对该次 pack 产生的同一份 `win-unpacked` 运行完整 Electron E2E。每个阶段的耗时与状态写入 `test-results/pipeline-timings.json`。

GitHub 正式发布同样只执行一次 Windows pack：复用质量门禁负责源码、覆盖率和跨平台测试，packaged E2E 延后到发布 Job，并直接验证该 Job 生成和签名的最终 `win-unpacked`，不得先测试另一份目录包再重新封装发布。

如果仅重新验证封装流程，也必须至少执行（例如）：

```powershell
npm --prefix apps/desktop run check:encoding
npm --prefix apps/desktop run check:release
npm --prefix apps/desktop run test:update-feed
npm --prefix apps/desktop run package:win
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/prepare_release_bundle.ps1 -Version 2.0.4
```

任何命令返回非零退出码时禁止发布。

### 2.2 Android APK 版

Android 发布提交必须通过：

- `git diff --check` 与 UTF-8 文本编码检查。
- `apps/android` 的单元测试（`gradlew test`），含 `app` 模块测试。
- 前端 `apps/android-frontend` 生产构建（由 Gradle `buildCoomiWeb` 任务在打包时强制执行）。
- Coomi Rust 引擎的 Android ARM64 交叉编译必须成功（`cargo build --release --target aarch64-linux-android`），产物 `libcoomi.so` 必须进入 APK。
- APK 构建命令（在 `apps/android` 下）：

```powershell
$env:TERMUX_APP_VERSION_NAME = "0.1.4"   # 可选，build.gradle 已硬编码时省略
.\gradlew.bat :app:assembleRelease
```

### 2.3 失败恢复与阶段复用

封装流程禁止对确定性失败进行无条件自动重试。失败后必须根据阶段处理：

- 测试、源码检查、资源同步或内置 Python 校验失败：修复后重新执行完整门禁。
- Electron 目录包失败且准备后的输入未变化：只重跑 `npm --prefix apps/desktop run build:desktop:prepared`。
- NSIS 失败且准备后的输入未变化：只重跑 `npm --prefix apps/desktop run package:win:prepared`。
- packaged smoke/E2E 失败且封装产物未变化：只重跑 `test:smoke` 或 `test:e2e`。
- 发布 ZIP、manifest 或 checksum 阶段失败且 `apps/desktop/release` 未变化：只重跑 `prepare_release_bundle.ps1`。
- Android Gradle 构建失败：修复后重新执行完整 Android 门禁。

prepared 命令只允许复用当前工作区刚刚完成的 `prepare:package` 或 `prepare:package:assets` 结果。源码、依赖、前端构建、Coomi、内置 Python、MinGit 或文档资源任一发生变化后，prepared 状态立即失效，必须改用 `build:desktop` 或 `package:win` 完整入口。

便携 ZIP 默认使用 `Fastest` 压缩，并通过 ZIP 条目名称、数量和未压缩大小与 `win-unpacked` 逐文件核对，禁止再用全量解压作为常规存在性探测。只有明确需要优先减小便携包体积时，才使用 `prepare_release_bundle.ps1 -CompressionLevel Optimal`。

## 3. 构建环境要求

### 3.1 Windows 桌面版

- Windows x64 构建环境。
- Node.js 20，前端和桌面端均使用锁文件执行 `npm ci`。
- Python 3.9；通过 `scripts/bootstrap_python39.ps1 -InstallRequirements` 准备可迁移的内置运行时。
- `npm --prefix apps/desktop run check:embedded-python` 必须通过；内置运行时不得超过 512 MB，不得夹带 Storydex 未声明的 Conda/CUDA/MKL 载荷，并且必须能加载二进制依赖、启动后端和返回健康响应。
- Electron、electron-builder、Python 依赖和 Storydex Coomi 运行时均以仓库锁文件与固定校验为准。
- 正式包不得包含 `.env`、密钥、证书、用户配置、日志、测试结果、coverage、pytest 缓存或其他开发期临时文件。
- 发布流程不得从工作区外临时复制未经记录的二进制或依赖。

### 3.2 Android APK 版

- Windows 或 Linux x64 构建环境，安装 Android SDK（含 NDK，项目使用 `ndkVersion=27.0.12077973`）与 JDK 17。
- Rust 工具链，必须已安装 `aarch64-linux-android` target（`rustup target add aarch64-linux-android`）。
- NDK 交叉链接器由 Gradle 任务自动定位（`ndk/<版本>/toolchains/llvm/prebuilt/windows-x86_64/bin/aarch64-linux-android24-clang.cmd`）。
- `apps/android-frontend` 需先执行 `npm ci`；Gradle `buildCoomiWeb` 任务会调用其生产构建。
- 正式 APK 不得包含 `.env`、密钥、证书、用户配置、日志、测试结果或开发期临时文件。

## 4. 必需发行产物

### 4.1 Windows 桌面版

每个 Windows 正式版本必须包含：

| 文件                                      | 要求                                                      |
| ----------------------------------------- | --------------------------------------------------------- |
| `StorydexSetup-x64-<版本>.exe`          | Windows x64 NSIS 安装包                                   |
| `StorydexSetup-x64-<版本>.exe.blockmap` | 与安装包同版本的差分更新文件                              |
| `Storydex-win-unpacked.zip`             | 包含`Storydex.exe` 的便携包                             |
| `latest.yml`                            | `version`、`path`、`size`、SHA-512 必须与安装包一致 |
| `SHA256SUMS.txt`                        | 覆盖发布目录内全部正式文件                                |
| `RELEASE_NOTES.md`                      | 与版本对应的用户可读发行说明                              |
| `BUILD_MANIFEST.json`                   | 包含 Git 提交、构建时间、运行时版本和产物摘要             |
| `DEPENDENCIES.json`                     | 包含前端、桌面端和 Python 依赖清单                        |

安装包和便携包都必须包含可启动的桌面应用、前端生产资源、后端服务、内置 Python 运行时、固定依赖和 MinGit。便携 ZIP 解压后必须能找到 `Storydex.exe`。

以下创作资源目录必须递归完整封装，并与仓库源文件逐文件一致：

- `docs/guide`：应用内使用指南。
- `docs/prompts`：指令仓库及分类提示词模板。
- `docs/skills`：新建小说项目时使用的详细通用内置技能模板。

封装校验不得只检查目录或 `README.md` 是否存在，必须比对递归文件列表和文件内容摘要，防止分类子目录或技能模板漏包。

### 4.2 Android APK 版

每个 Android 正式版本必须包含：

| 文件                                            | 要求                                        |
| ----------------------------------------------- | ------------------------------------------- |
| `Storydex-Android-arm64-v<版本>.apk`            | ARM64 APK（实际构建产物重命名而来）          |
| `Storydex-Android-arm64-v<版本>.apk.sha256`     | APK 的 SHA-256 校验值文件                    |
| `apps/website-overlay/storydex-android-download-v<版本>.js` | 官网下载按钮 overlay（随仓库提交） |

APK 必须内置：

- `apps/android-frontend` 生产构建产物（打包为 `web.zip`）。
- Coomi Rust 引擎的 `libcoomi.so`（ARM64 PIE 可执行文件重命名）。
- Termux bootstrap 运行时（`apt-android-7` 变体）。

## 5. Git 提交、标签与 GitHub Release

1. 检查 `git status`，确认提交内容只包含本次发行需要的源码、文档和版本文件。
2. 提交发布变更并推送 `main`。
3. 在发布提交上创建带说明的标签 `v<版本>`，并推送该标签。

### 5.1 Windows 桌面版

- 标签推送触发 `.github/workflows/release-windows.yml`；工作流必须先通过复用质量门禁，再构建和发布。
- GitHub Release 标题统一为 `Storydex v<版本>`，不得设置为草稿或预发布版本，除非发布计划明确要求。
- GitHub Release 中的资产数量、文件名、大小与校验值必须和本地封装结果一致。

推荐命令（例如）：

```powershell
git push origin main
git tag -a v2.0.4 -m "Storydex v2.0.4"
git push origin v2.0.4
```

### 5.2 Android APK 版

- Android APK 作为资产挂载到**桌面版对应的 GitHub Release**（例如 `v2.0.4`），与桌面资产一起发布。
- 发布 APK 由 `.github/workflows/deploy-android.yml` 手动触发（workflow_dispatch），输入 `tag`、`version`、`asset_name`、`sha256`：
  - `tag`：包含 APK 的 GitHub Release 标签（默认 `v2.0.4`）。
  - `version`：Android 语义化版本（默认 `0.1.4`）。
  - `asset_name`：APK 资产文件名（默认 `Storydex-Android-arm64-v0.1.4.apk`）。
  - `sha256`：APK 的 SHA-256（必填，与 release 资产校验一致）。
- 触发方式（本地或 Actions 页面）：

```powershell
$sha = (Get-FileHash .\Storydex-Android-arm64-v0.1.4.apk -Algorithm SHA256).Hash.ToLower()
gh release upload v2.0.4 Storydex-Android-arm64-v0.1.4.apk Storydex-Android-arm64-v0.1.4.apk.sha256
gh workflow run deploy-android.yml -f tag=v2.0.4 -f version=0.1.4 -f asset_name=Storydex-Android-arm64-v0.1.4.apk -f sha256=$sha
```

- workflow 会从 GitHub Release 下载 APK、校验 SHA-256、发布到 Android 更新源，并注入官网下载按钮 overlay。
- 本地构建的 APK 文件名（`storydex-app_<variant>-release_arm64-v8a.apk`）上传前必须重命名为 `Storydex-Android-arm64-v<版本>.apk`。

## 6. 更新源同步

### 6.1 Windows 桌面版

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

同步后必须验证（例如）：

```powershell
$base = 'https://updates.septemc.com/storydex/windows'
Invoke-WebRequest -UseBasicParsing "$base/latest.yml?verify=1"
Invoke-WebRequest -UseBasicParsing -Method Head "$base/StorydexSetup-x64-2.0.4.exe"
Invoke-WebRequest -UseBasicParsing -Method Head "$base/StorydexSetup-x64-2.0.4.exe.blockmap"
```

`latest.yml` 中的 `version` 必须为正确版本号，例如， `2.0.4`，`path` 必须为 `StorydexSetup-x64-2.0.4.exe`（例如）。

### 6.2 Android APK 版

正式发布必须同步到：

```text
/www/wwwroot/updates.septemc.com/storydex/android
```

公网地址：

```text
https://updates.septemc.com/storydex/android/
```

同步顺序（由 `deploy-android.yml` 执行）：

1. 确保远程 `android` 目录存在。
2. 上传 APK 到临时文件名，再原子重命名为正式文件名，避免下载到半状态文件。
3. 上传 overlay 脚本到官网 `storydex.septemc.com/assets/`，并调用 `scripts/inject_storydex_android_download.py` 注入 `index.html` 下载按钮。
4. 保留上一正式版本的 APK，支持必要回退。

更新源至少必须公开：

- `Storydex-Android-arm64-v<版本>.apk`

同步后必须验证（例如）：

```powershell
$apk = 'https://updates.septemc.com/storydex/android/Storydex-Android-arm64-v0.1.4.apk'
Invoke-WebRequest -UseBasicParsing -Method Head "$apk?verify=1"
```

## 7. 发布后验收

- GitHub Release 已公开且标签、标题、说明和资产完整（Windows + Android 资产齐全）。
- Windows 更新源的 `latest.yml`、安装包和 blockmap 均返回成功状态。
- 对 `latest.yml` 声明的安装包大小和 SHA-512 重新校验。
- Android 更新源的 APK 可公开下载，且 SHA-256 与 release 资产一致。
- 官网下载按钮 overlay 指向最新 APK，注入后的 `index.html` 状态正确。
- 使用上一正式安装版执行一次“检查更新 -> 下载 -> 安装 -> 重启”验证。
- 使用便携 ZIP 在新的目录执行冷启动，确认前端、后端、内置 Python、Coomi 和 Git 功能可用。
- 记录 Git 提交、标签、GitHub Actions 运行链接、发行资产 SHA-256 与更新时间。

## 8. 失败处理与回退

- 质量门禁或封装失败：修复后重新提交，禁止移动已经公开使用的正式标签。
- GitHub Release 创建失败：保留标签和日志，修复工作流后重新运行；不得手工上传来源不明的替代文件。
- 更新源同步失败：保持旧 `latest.yml` 不变，先补齐安装包和 blockmap，再原子切换元数据。
- 新版本存在阻断问题：将 `latest.yml` 原子恢复到上一稳定版本，并在 GitHub Release 中明确状态；后续使用新的修订版本发布，不覆盖已经发布的二进制。
- Android 发布失败（APK 未上传、SHA-256 不符或 overlay 注入失败）：保持远程已有 APK 不变，修复后重新触发 `deploy-android.yml`；不得手工覆盖远程 APK 为未校验文件。
- 私钥、令牌和服务器凭据只能存放在 GitHub Environment/Secrets 中，禁止写入仓库、日志、发行包或说明文档。

## 9. 发布基线

### 9.1 Windows 桌面版

- 版本：`2.0.4`
- 标签：`v2.0.4`
- Release 标题：`Storydex v2.0.4`
- 安装包：`StorydexSetup-x64-2.0.4.exe`
- 更新目录：`/www/wwwroot/updates.septemc.com/storydex/windows`
- 更新元数据：`https://updates.septemc.com/storydex/windows/latest.yml`
- 必须内置：`docs/guide`、`docs/prompts`、`docs/skills`

### 9.2 Android APK 版

- 版本：`0.1.4`
- APK：`Storydex-Android-arm64-v0.1.4.apk`
- 更新目录：`/www/wwwroot/updates.septemc.com/storydex/android`
- 更新地址：`https://updates.septemc.com/storydex/android/Storydex-Android-arm64-v0.1.4.apk`
- 官网下载按钮 overlay：`apps/website-overlay/storydex-android-download-v0.1.4.js`
- 发布工作流：`.github/workflows/deploy-android.yml`（workflow_dispatch）
