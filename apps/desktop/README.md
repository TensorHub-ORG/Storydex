# Storydex Desktop（Tauri 2）

该目录提供 Storydex Windows 正式桌面应用。当前默认开发、构建、打包和更新入口均为 Tauri 2；`storydex-agentd` 是独立 Rust sidecar。旧 Electron/Python 桌面运行时已从仓库运行路径移除，Python 后端仅作为兼容/测试边界保留。

## 目录说明

- `tauri-preview/`：Tauri 2 Stable 源码。目录名是迁移期历史命名，不代表仍处于 Preview。
- `agent-runtime/`：`storydex-agentd`、Coomi Rust 及 Storydex 领域服务。
- `candidate/staging/`：打包后的最小运行目录，受 Git 忽略。
- `release/`：本地 NSIS、updater 和便携包产物，受 Git 忽略。
- `scripts/build-coomi-runtime.cjs`：构建 Rust Coomi bridge 并记录可追踪的构建身份。

## 开发模式

先安装前端和桌面构建依赖，并构建 debug sidecar：

```powershell
npm ci --prefix apps/frontend
npm ci --prefix apps/desktop
cargo build --manifest-path apps/desktop/agent-runtime/Cargo.toml --locked -p storydex-agentd -p storydex-coomi-bridge
npm --prefix apps/desktop run dev
```

Tauri debug 进程会从 `apps/desktop/agent-runtime/target/debug/` 找到 `storydex-agentd.exe` 及其同目录的 `storydex-coomi-bridge.exe`。也可以通过 `STORYDEX_TAURI_SIDECAR_PATH` 显式指定同名测试二进制；指定的 sidecar 旁边也必须放置 bridge，该变量不用于正式封装。

## 聚焦检查

```powershell
npm --prefix apps/desktop run test:unit
npm --prefix apps/desktop run check:release
npm --prefix apps/desktop run check:tauri
```

完成打包后可运行：

```powershell
npm --prefix apps/desktop run check:packaged
npm --prefix apps/desktop run smoke:tauri
```

smoke 使用操作系统临时目录和隔离 fixture，验证动态端口、健康检查、无令牌日志、窗口关闭、鉴权 shutdown 及 sidecar 进程树清理，不访问真实用户项目。

## Windows 打包

正式构建需要一对 Tauri updater 密钥：

- `STORYDEX_TAURI_UPDATER_PUBKEY`：编入客户端的公钥；
- `TAURI_SIGNING_PRIVATE_KEY` 或 `TAURI_SIGNING_PRIVATE_KEY_PATH`：给更新安装包签名；
- `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`：私钥密码；本地临时测试密钥可以无密码，正式 release workflow 要求生产私钥使用非空密码；
- `STORYDEX_WINDOWS_CERTIFICATE_THUMBPRINT`：可选 Authenticode 证书指纹。

私钥不得写入仓库、聊天、日志或发行包。普通代码修改、PR、合并和 Tauri 开发不需要生产私钥；只有生成可被现有客户端自动接受的正式更新时需要它。

```powershell
npm --prefix apps/desktop run package:win
```

构建流程会：

1. 构建 release `storydex-agentd`、`storydex-coomi-bridge` 和 Vue 生产资源；
2. 使用固定的 Tauri CLI 生成 NSIS 及 `.sig`；
3. 建立含 `Storydex.exe`、`storydex-agentd.exe`、`storydex-coomi-bridge.exe` 和 `mingit/` 的 staging；
4. 生成 `latest.json` 和 `Storydex-win-portable.zip`；
5. 执行 Rust-only 资产策略和更新产物校验。

产物位于 `apps/desktop/release/`：

```text
StorydexSetup-x64-<version>.exe
StorydexSetup-x64-<version>.exe.sig
latest.json
Storydex-win-portable.zip
```

正式发布工作流会进一步生成 `RELEASE_NOTES.md`、`SHA256SUMS.txt`、`BUILD_MANIFEST.json` 和 `DEPENDENCIES.json`。

## 应用内更新

Tauri updater 从以下地址读取静态清单：

```text
https://updates.septemc.com/storydex/windows/latest.json
```

客户端使用内置公钥验证 `latest.json` 中声明的签名。`.exe.sig` 与 updater 私钥用于证明更新包由 Storydex 发布且未被替换；这与可选的 Windows Authenticode 证书不是同一机制。

更新源应先上传安装包、`.sig` 和便携包，最后原子替换 `latest.json`。不再使用 Electron 的 `latest.yml`、blockmap 或 `electron-updater`。

## 运行时资产边界

`apps/desktop/candidate/runtime-policy.json` 约束正式 staging：

- 必须包含 `Storydex.exe`、`storydex-agentd.exe`、`storydex-coomi-bridge.exe` 和 MinGit；
- 不得包含 Python、FastAPI/Uvicorn、Electron、Node/npm 或包管理器运行时；
- 不得包含凭证、日志、测试结果或指向仓库外真实用户项目的链接；
- staging 不得与源码根、旧 Electron 根或 release 根错误重叠。

手工检查入口：

```powershell
$env:STORYDEX_RUST_CANDIDATE_ROOT = "apps/desktop/candidate/staging"
npm --prefix apps/desktop run check:rust-candidate
```
