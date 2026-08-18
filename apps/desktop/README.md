# Storydex Desktop (Electron)

该目录提供 Storydex 的桌面开发壳。目标是直接以桌面应用方式运行 Vue 工作台，并在应用内部启动后端内核。

## 开发模式

请在项目根目录运行一键桌面开发脚本：

```powershell
.\scripts\run_desktop_dev.bat
```

开发模式下会：

1. 通过 `scripts\bootstrap_python39.ps1` 准备项目内 `.python39` 运行时。
2. 安装或复用前端与桌面壳 npm 依赖。
3. 启动前端 Vite 开发服务。
4. 启动 Electron 桌面窗口。
5. 由 Electron 主进程自动拉起后端 uvicorn 内核（18081）。

只准备依赖、不启动 Electron：

```powershell
.\scripts\run_desktop_dev.bat --prepare-only
```

## 编译桌面应用

在项目根目录执行：

```powershell
.\scripts\build_desktop_app.bat
```

输出目录：

- `apps/desktop/release/win-unpacked/`

如果需要生成安装包：

```powershell
npm --prefix apps/desktop run package:win
```

完整入口会依次构建前端和 Coomi、同步桌面资源、验证内置 Python，然后执行一次 Electron pack。正常封装使用完整入口，不需要手工组合子命令。

封装过程按以下层级验证：

- `run_full_test_suite.ps1 -Mode Full`：生成目录包并运行快速 packaged smoke。
- `run_full_test_suite.ps1 -Mode Release`：生成 NSIS、校验正式产物并运行完整 packaged E2E。

如果封装阶段失败，但源码、前端构建、Coomi 二进制和打包资源均未改变，可以只重跑失败阶段：

```powershell
# Electron 目录包失败
npm --prefix apps/desktop run build:desktop:prepared

# NSIS 安装包失败
npm --prefix apps/desktop run package:win:prepared

# 快速 smoke 或完整 E2E 失败
npm --prefix apps/desktop run test:smoke
npm --prefix apps/desktop run test:e2e
```

一旦源码、依赖、前端产物、Coomi 或内置运行时发生变化，必须重新执行 `build:desktop` 或 `package:win` 完整入口，禁止复用旧的 prepared 资源。

便携 ZIP 默认使用 `Fastest` 压缩，以避免对 Electron、Python 和 MinGit 中已经压缩过的二进制进行长时间重复压缩。如果发布场景更重视体积，可以单独执行 `prepare_release_bundle.ps1 -CompressionLevel Optimal`，但该模式预期耗时更长。

## 差分更新（增量更新）

桌面应用内置基于 `electron-updater` 的差分更新：NSIS 打包会同时产出 `StorydexSetup-x64-<version>.exe.blockmap` 与 `latest.yml`，客户端更新时对比新旧 blockmap，只下载有变化的数据块。

发布一个新版本时，把以下产物上传到更新服务器同一目录：

- `StorydexSetup-x64-<version>.exe`
- `StorydexSetup-x64-<version>.exe.blockmap`
- `latest.yml`

注意事项：

1. 更新源地址默认取 `package.json` 中 `build.publish` 的 generic URL，打包前请改成实际的服务器地址；运行时也可以用环境变量 `STORYDEX_UPDATE_URL` 覆盖。
2. 服务器上需要保留旧版本的 `.exe.blockmap`，否则客户端会回退为全量下载。
3. 应用内入口：系统设置 → 更新与关于 → 检查更新 / 下载更新（增量）/ 重启并安装。
4. 自动更新仅对打包后的版本生效，开发模式（`npm run dev`）不支持。

## Rust/Tauri 候选资产门禁

Rust/Tauri 预览构建必须输出到与 Stable Electron 隔离的 candidate staging
目录。候选目录不能包含 Python/FastAPI/Uvicorn、Electron 或 Node 运行时，
也不能通过符号链接指向仓库外的真实用户项目。Stable 的 `electron/`、`app/`
和 `release/` 目录不会被该门禁扫描。

先生成候选资产，再从仓库根目录执行：

```powershell
$env:STORYDEX_RUST_CANDIDATE_ROOT = "apps/desktop/candidate/staging"
npm --prefix apps/desktop run check:rust-candidate
```

也可以显式传入目录并输出机器可读报告：

```powershell
node apps/desktop/scripts/validate-rust-candidate-assets.cjs `
  --root apps/desktop/candidate/staging `
  --json
```

策略清单位于 `apps/desktop/candidate/runtime-policy.json`。该检查只约束候选
构建输入，不改变 Stable Electron 的启动、打包或更新配置；候选目录不存在、
越出仓库边界或与 Stable 资产根重叠时会直接失败。
