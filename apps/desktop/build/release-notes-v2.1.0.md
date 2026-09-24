# Storydex v2.1.0

Storydex v2.1.0 是 Windows 桌面端运行时稳定性修复版本，解决预设参数文件缺失、正文发布被 Windows 文件占用阻断，以及旧 Python 后端残留导致的桌面端混用问题。

## 修复内容

- 激活预设只有 Markdown、缺少同名 `.preset.json` 时，Rust Agent 会使用 Markdown 内容构造临时预设，不再因为 sidecar 缺失直接拒绝启动，也不会自动覆盖用户文件。
- 预设管理列表明确标记缺少参数文件的 Markdown 预设，便于后续编辑并保存参数；不需要为恢复启动而删除或重建预设。
- 正文候选发布使用 Windows 文件占用重试策略，降低编辑器或杀毒软件短暂持有目标文件时的原子替换失败。
- Tauri 启动时校验 sidecar 的运行时身份和版本。检测到旧 Python 服务、未知后端或缺少 Rust 健康标识时，会停留在启动页并显示原因，禁止继续加载项目状态。
- Axios 和 Agent SSE 请求优先使用 Tauri 注入的动态 sidecar 地址，避免回退到本机残留的旧 Python/Vite 代理。
- Agent 错误界面补充写入校验、失败目标和暂存候选路径，`bounded_story_generation` 拒绝时可以直接定位原因和保留的候选文件。

## 升级说明

- v2.0.9 用户可直接使用完整安装包 `StorydexSetup-x64-2.1.0.exe` 覆盖安装；安装不会删除项目目录、会话记录、Provider 配置或预设文件。
- 若本机曾手动启动旧 Python 后端，完全退出旧进程后再启动 2.1.0；新版本会主动拒绝连接到旧运行时，而不是混用两个后端。
- 缺少 `.preset.json` 的 Markdown 预设可以继续使用。需要可编辑参数时，在预设管理中编辑并保存即可生成参数文件；无需手动删除预设。
- Windows 更新源：<https://updates.septemc.com/storydex/windows/latest.json>

## 发行资产

- `StorydexSetup-x64-2.1.0.exe`：Windows x64 NSIS 安装包。
- `StorydexSetup-x64-2.1.0.exe.sig`：Tauri updater 签名文件。
- `Storydex-win-portable.zip`：Rust-only 便携包。
- `latest.json`、`SHA256SUMS.txt`、`BUILD_MANIFEST.json` 和 `DEPENDENCIES.json`：更新清单、校验值、构建信息与依赖清单。
