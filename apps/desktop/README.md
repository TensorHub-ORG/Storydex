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
npm run package:win
```

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

