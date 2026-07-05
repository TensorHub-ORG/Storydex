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
