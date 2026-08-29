# Storydex Tauri 2 Stable

该目录是当前 Windows Stable 桌面壳源码。`tauri-preview` 是迁移期遗留目录名；默认 `dev`、`build:desktop`、`package:win` 和正式 Windows Release 均已指向这里。

Python 后端的兼容/测试代码仍可在独立边界运行，但 Electron/Python 桌面启动、打包和运行时已不再属于仓库产品路径，也不会进入这里生成的 Tauri Stable staging。

Tauri Core 启动打包的 `storydex-agentd`，等待其返回动态 loopback 端口和随机运行令牌，验证 `/api/v1/sys/health` 后创建 Vue 主窗口。渲染层只获得最小 `window.storydexDesktop` 适配和 updater capability，不获得任意 shell 或文件系统权限。

## 本地开发

从仓库根目录执行：

```powershell
npm ci --prefix apps/frontend
npm ci --prefix apps/desktop
cargo build --manifest-path apps/desktop/agent-runtime/Cargo.toml --locked -p storydex-agentd -p storydex-coomi-bridge
npm --prefix apps/desktop run dev
```

debug 模式默认读取 `apps/desktop/agent-runtime/target/debug/storydex-agentd.exe`，并要求同目录存在 `storydex-coomi-bridge.exe`。`STORYDEX_TAURI_SIDECAR_PATH` 仅用于显式指定隔离测试二进制；指定的 sidecar 旁边也必须放置 bridge。

## 打包

打包入口：

```powershell
npm --prefix apps/desktop run package:win
```

`scripts/prepare-preview.ps1` 构建 release sidecar、同步带 target triple 后缀的 `externalBin`、准备 MinGit 并构建 Vue。`scripts/package-preview.ps1` 注入 updater 公钥和私钥，调用固定 npm Tauri CLI，随后生成 Stable staging、NSIS、`.sig`、`latest.json` 和便携 ZIP。

虽然脚本名仍包含 `preview`，它们现在服务于 Stable。重命名属于独立机械清理任务，不影响运行时身份或发布契约。

正式 staging 包含：

```text
Storydex.exe
storydex-agentd.exe
storydex-coomi-bridge.exe
mingit/
```

不复制 Python、FastAPI/Uvicorn、Electron、Node、npm 或包管理器运行时。

## 生命周期验证

```powershell
npm --prefix apps/desktop run smoke:tauri
```

smoke 启动真实 staging，但将应用数据和 fixture workspace 指向新的操作系统临时目录。它验证动态健康端点、日志脱敏、窗口关闭、鉴权 shutdown 以及 Tauri/sidecar 进程完全退出；成功后清理临时目录，失败时保留诊断。

单独检查 staging：

```powershell
$env:STORYDEX_RUST_CANDIDATE_ROOT = "apps/desktop/candidate/staging"
npm --prefix apps/desktop run check:rust-candidate
```

不要将检查根指向 `apps/desktop`、旧 `apps/desktop/app` 或 `apps/desktop/release`。策略会对越界、根目录重叠和非 Rust Stable 资产直接失败。
