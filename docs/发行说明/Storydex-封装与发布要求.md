# Storydex 封装与发布要求

更新日期：2026-08-29

本文是贡献者和发布维护者参考，规定当前 Storydex 正式版本的版本管理、质量门禁、发行产物、更新源和回滚要求。历史 Electron/Python 发布规则不适用于新版本；已发布版本的用户变更记录以 GitHub Releases 为准，内部迁移记录不随公开仓库保存。

当前覆盖平台：

- **Windows 桌面版**：Vue 3 + Tauri 2 + 独立 `storydex-agentd` Rust sidecar；
- **Android APK 版**：`apps/android` + `apps/android-frontend` + 独立 Android Rust runtime。

## 1. 版本与分支

### 1.1 通用规则

1. 正式版本使用语义化版本号，Git 标签使用 `v` 前缀，例如 `2.0.5` 对应 `v2.0.5`。
2. Windows 与 Android 维护各自的版本序列。发布时读取当前源码值，不在本文长期硬编码某个历史版本。
3. 正式标签只能指向已经推送到远端 `main`、且该 HEAD 完整 CI 为 `success` 的提交。
4. 禁止从脏工作区、未推送提交或已知失败的 CI 基线创建标签。

### 1.2 Windows 版本文件

以下位置必须一致：

- `apps/desktop/package.json` 的 `version`；
- `apps/desktop/package-lock.json` 的根包版本；
- `apps/desktop/tauri-preview/tauri.conf.json` 的 `version`；
- `apps/desktop/build/release-notes-v<版本>.md`；
- 发布标签 `v<版本>`。

检查入口：

```powershell
node scripts/validate_version_consistency.cjs --expected=<版本>
```

### 1.3 Android 版本文件

- `apps/android/app/build.gradle` 的 `versionCode` 必须单调递增，`versionName` 必须为合法语义化版本。
- `deploy-android.yml` 的输入、APK 文件名和官网 overlay 必须与本次发布计划一致。
- Android APK 仍作为指定 GitHub Release 的独立资产发布，不与 Windows 版本号强制相同。

## 2. 质量门禁

### 2.1 本地开发与 push

提交前运行与改动直接相关的聚焦测试。每次 push 必须执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_pre_push_ci.ps1
```

该入口只执行编码、冲突标记、版本一致性和 whitespace 基础检查。普通 push 前不得自动运行全组件、打包或 E2E 套件。

Windows 桌面文档或发布配置相关的常用聚焦检查：

```powershell
npm --prefix apps/desktop run check:encoding
npm --prefix apps/desktop run check:release
npm --prefix apps/desktop run test:unit
npm --prefix apps/desktop run check:tauri
```

### 2.2 GitHub Actions

- `dev/windows` 的 Windows 改动必须通过 Windows Development CI；
- `main` 按改动范围执行组件门禁；同一 SHA 已通过 `dev/windows` 时可复用 Windows 结果；正式发布 workflow 使用 Windows 专项门禁；
- push 后必须监控对应 HEAD 的工作流到最终 `success`，失败时修复具体 job/step 根因。

本文只定义 `dev/windows` → `main` 的 Windows 集成路径；Android 发布仍按对应 workflow 的检查执行，不在此扩展其他分支治理规则。

`.github/workflows/release-windows.yml` 复用 `quality-gate.yml` 的 Windows 专项配置，然后只对该 release job 生成的 Tauri 产物执行签名和资产校验。Tauri GUI lifecycle smoke 不再作为 CI 或发布阻断项；不得用另一份本地或早期构建冒充最终发布产物。

本地 `scripts/run_full_test_suite.ps1 -Mode Fast|Full|Release` 仅保留为人工完整验证入口，不是普通 push 前置条件。

## 3. Windows 构建环境与凭据

### 3.1 构建环境

- Windows x64；
- Node.js 20，并对前端和桌面端执行锁文件安装；
- 仓库规定的 Rust 工具链和两个锁定 Cargo workspace；
- Tauri CLI 使用 `apps/desktop/package.json` 固定的 npm 依赖；
- 系统具备 Windows WebView2 构建条件，安装器使用 WebView2 bootstrapper；
- MinGit 来源必须是仓库记录的 `apps/desktop/vendor/mingit` 或显式受控来源。

正式 runtime 不得包含 Python、FastAPI/Uvicorn、Electron、Node、npm、测试框架、缓存、日志、`.env` 或凭据。

### 3.2 Tauri updater 密钥

保留应用内自动更新时，正式构建必须配置：

- `STORYDEX_TAURI_UPDATER_PUBKEY`；
- `TAURI_SIGNING_PRIVATE_KEY`；
- `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`。

私钥只用于给更新安装包生成 `.sig`，不用于修改、提交或合并源码。私钥不得提交、不得输出到日志或聊天，必须建立离线备份；丢失私钥后，已安装客户端无法自动接受使用新私钥签名的后续版本。

质量门禁可以生成一次性临时密钥验证打包流程，但临时密钥不得用于公开版本。

### 3.3 Windows Authenticode

`STORYDEX_WINDOWS_CERTIFICATE_THUMBPRINT` 是可选的 Windows 代码签名证书指纹：

- 不配置时仍可使用 Tauri updater 签名并发布；
- 安装包可能显示未知发布者或触发 SmartScreen；
- 单独配置指纹但未在 runner 安全导入对应证书，不能完成 Authenticode 签名。

Updater 签名和 Authenticode 是两个独立机制，不得混为一谈。

## 4. Windows 构建与必需产物

构建入口：

```powershell
npm --prefix apps/desktop run package:win
```

该命令必须构建 Vue、release `storydex-agentd`、Tauri NSIS、签名 updater 资产和 Rust-only portable staging，并执行资产策略检查。

每个 Windows 正式版本至少包含：

| 文件 | 要求 |
| --- | --- |
| `StorydexSetup-x64-<版本>.exe` | Windows x64 NSIS 安装包，同时作为 updater 安装包 |
| `StorydexSetup-x64-<版本>.exe.sig` | Tauri updater 对应签名，内容必须与 `latest.json` 一致 |
| `latest.json` | 包含版本、Windows x86_64 下载 URL 和签名 |
| `Storydex-win-portable.zip` | 包含最小可运行目录的便携包 |
| `RELEASE_NOTES.md` | 对应版本的用户可读发行说明 |
| `SHA256SUMS.txt` | 覆盖正式 release bundle |
| `BUILD_MANIFEST.json` | Git 提交、版本、构建时间、runtime 和资产摘要 |
| `DEPENDENCIES.json` | 前端、Tauri 和 Rust 依赖清单 |

便携 ZIP 解压后至少必须包含：

```text
Storydex.exe
storydex-agentd.exe
storydex-coomi-bridge.exe
mingit/cmd/git.exe
```

Vue 资源由 Tauri 打包；使用指南和指令模板由 `storydex-agentd` 编译期嵌入。最终资产检查必须验证没有 Python/Electron/Node runtime、凭据、日志、测试结果或真实用户项目内容。

不再生成或发布 Electron 的 `latest.yml`、`.blockmap` 或 `win-unpacked` ZIP。

## 5. Windows Release 工作流

标签 push 或手动触发 `.github/workflows/release-windows.yml`：

1. 运行 Windows 专项质量门禁；
2. 校验版本和 Tauri release 配置；
3. 读取 updater 密钥，构建 NSIS、`.sig`、`latest.json` 和便携包；
4. 校验最小 Rust runtime、更新签名、安装包和便携包资产；
5. 如配置 Authenticode 指纹，额外验证 `Storydex.exe` 和安装包签名；
6. 生成 release bundle、校验值、依赖清单和构建 manifest；
7. dry-run 只上传短期 Actions artifact；正式模式创建 GitHub Release 并同步 VPS 更新源。

手动触发时应先使用 `dry_run=true` 验证正式密钥构建，不发布 GitHub Release、不替换线上 `latest.json`。只有人工安装和升级验收完成后，才能使用标签 push 或 `dry_run=false` 正式发布。

## 6. Windows 更新源

更新目录：

```text
/www/wwwroot/updates.septemc.com/storydex/windows
```

公开入口：

```text
https://updates.septemc.com/storydex/windows/latest.json
```

同步顺序必须为：

1. 确保远程目录存在；
2. 上传安装包、对应 `.exe.sig` 和便携 ZIP；
3. 将新的 `latest.json` 上传为带 run id 的临时文件；
4. 最后在服务器端原子重命名为 `latest.json`；
5. 回读 `latest.json`，核对版本、URL 和签名；
6. 对安装包、`.sig` 和便携包执行公网可用性检查。

不得先替换 `latest.json` 再补传二进制。应保留至少一个已验证的上一签名安装版本，用于人工回滚和灾难恢复。

## 7. 发布前人工验收

自动化成功不等于生产发布完成。Windows 首个 Tauri Stable 版本至少验证：

- 全新安装、首次启动、打开项目和退出清理；
- 便携包冷启动及 `Storydex.exe`、`storydex-agentd.exe`、`storydex-coomi-bridge.exe`、MinGit 完整性；
- 正常 HTTP/SSE Agent 主链路和关键文件/Git/WIKI 操作；
- 应用内检查、下载、签名验证、安装和重启；
- 从上一已发布版本到当前 Tauri Stable 的真实安装升级（若上一版本仍为 Electron，需在隔离测试机验证迁移）；
- 下载失败、签名失败或安装失败后的可恢复性；
- 使用上一个已签名版本进行人工回滚；
- 升级和回滚后同一测试项目的数据、会话和 Git 状态兼容；
- updater 私钥离线备份和恢复演练。

以上验收必须使用隔离测试机或 VM 和测试项目，不得拿唯一真实创作项目直接做升级/回滚试验。

## 8. Windows 失败处理与回滚

- 质量门禁、构建、签名、资产校验或 smoke 失败：不得创建或更新正式 Release。
- GitHub Release 创建失败：保留日志和标签状态，修复 workflow 后重跑，不上传来源不明的替代文件。
- VPS 同步失败：保持旧 `latest.json` 不变，补齐新资产后再原子切换。
- 新版本存在阻断问题：将 `latest.json` 原子恢复到上一可用签名版本，并明确发布状态；后续使用新的修订版本，不覆盖已发布二进制。
- updater 私钥疑似泄露：立即停止发布，评估密钥轮换兼容方案；不得简单生成新密钥后继续推送，因为旧客户端只信任已内置公钥。
- 当前源码不再保留 Electron 桌面运行时、Python Agent 入口或旧打包专属 CI；历史版本升级/回滚验证只针对已发布资产，不得把历史兼容路径重新加入默认构建。

## 9. Android APK

Android 发布提交必须通过：

- UTF-8、冲突标记、版本一致性和 `git diff --check`；
- Android 单元测试及相关前端测试；
- `apps/android-frontend` 生产构建；
- Coomi Rust ARM64 交叉编译；
- Gradle release APK 构建和产物校验。

必需资产：

| 文件 | 要求 |
| --- | --- |
| `Storydex-Android-arm64-v<版本>.apk` | ARM64 正式 APK |
| `Storydex-Android-arm64-v<版本>.apk.sha256` | APK 的 SHA-256 |
| 对应官网 overlay | 下载 URL 与实际 APK 一致 |

Android 更新目录为 `/www/wwwroot/updates.septemc.com/storydex/android`。`deploy-android.yml` 应下载 GitHub Release 中的 APK、校验 SHA-256、上传临时文件并原子替换，再更新官网下载入口。失败时保持现有线上 APK 和下载入口不变。

## 10. 凭据边界

以下内容只能保存在受保护的 GitHub Environment/Secrets 或离线安全介质：

- Tauri updater 私钥及密码；
- Windows 代码签名证书及访问凭据；
- VPS SSH 私钥和主机信息；
- GitHub、Provider 或其他生产令牌。

任何凭据都不得进入仓库、归档文档、Actions artifact、测试输出、发行包或用户可见错误信息。
