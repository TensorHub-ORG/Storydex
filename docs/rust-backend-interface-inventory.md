# Rust 后端公开接口覆盖清单

更新日期：2026-08-29

这是面向贡献者和维护者的可再生契约清单，不是普通用户文档。完整 JSON 只在本地 `output/` 生成，不提交到仓库。

本清单由 `scripts/generate_rust_backend_interface_inventory.cjs` 对比以下三类公开边界：

- `apps/frontend/src/api` 中 Vue 工作台真实消费的 API；
- `storydex-agentd` 当前注册的 Axum 路由；
- 当前仍存在的 Python/FastAPI 路由，用于迁移差分和识别无消费者的历史接口；它们不代表 Stable 运行时入口。

脚本会识别跨行泛型调用、模板字符串动态路径和章节模板的显式备用路径，并为归一化契约记录 `implemented`、`pending` 或带依据的 `excluded`。它不会启动服务、读取真实用户项目或把旧 Python 路由自动视为必须迁移的产品接口。

## 当前结论

2026-08-29 当前生成结果：

- Python 历史路由（仅作对照）：99；
- Vue 实际消费契约：96（其中 23 个是 Rust-only Agent 接口）；
- Vue 消费契约已由 Rust 覆盖：96；
- 待迁移：0；
- Rust 公开路由：104；
- 已实现契约：104；
- 无消费者且不再迁移的历史契约：22；
- Rust-only 路由（没有对应 Python 历史路由）：27。

`excluded` 不表示 Rust 后端缺失当前产品功能，而表示该 Python 历史路由在 `apps/frontend/src/api` 中没有消费者，且 Rust Stable 未注册对应路由。`frontendConsumersWithoutPythonRoute` 只用于标记 Rust-only 前端契约，不是缺口指标。Windows 正式运行时已经是 `Tauri 2 + storydex-agentd + Rust Coomi`；Python 路由只保留在兼容/测试边界的对照扫描中，Electron 和 Python 不进入默认开发、构建或发布路径。

当前所有权分组如下：

| 公开边界 | 已实现 | 待迁移 | 排除的历史接口 |
| --- | ---: | ---: | ---: |
| 文件/工作区 | 22 | 0 | 0 |
| Git | 14 | 0 | 0 |
| WIKI / Story Knowledge | 5 | 0 | 9 |
| Story 状态 | 4 | 0 | 14 |
| System | 10 | 0 | 1 |
| Agent | 16 | 0 | 0 |
| Coomi 控制面 | 8 | 0 | 0 |
| Auth | 9 | 0 | 3 |
| Help | 4 | 0 | 1 |
| Presets | 12 | 0 | 0 |

## 生成与检查

完整 JSON 是可再生运行产物，写入已被忽略的 `output/`，不应作为长期文档提交：

```powershell
node scripts/generate_rust_backend_interface_inventory.cjs --output output/rust-backend-interface-inventory.json
```

契约门禁：

```powershell
node --test scripts/tests/rust-backend-interface-inventory.test.cjs
```

门禁要求所有 Vue 消费契约均存在 Rust 路由，且 `contractsPending` 为 0；不要求每个前端契约都存在 Python 对照路由。新增或删除前端 API 时应先更新真实 Rust 路由，再重新生成清单；不得通过放宽解析或静默排除来掩盖缺口。
