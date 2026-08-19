# Rust 后端候选公开接口清单

本清单由 `scripts/generate_rust_backend_interface_inventory.cjs` 从当前
FastAPI 路由、Vue 工作台 API 消费者和 `storydex-agentd` Axum 路由生成。
它会识别跨行泛型调用、模板字符串动态路径和章节模板的显式备用路径，
并为每个归一化契约记录 `implemented`、`pending` 或带依据的 `excluded`。
它是迁移追踪资料，不是把 Python Stable 标记为已迁移的声明；Stable 仍固定使用
`Electron + Python/FastAPI + Rust Coomi bridge`。

生成完整 JSON 清单：

```powershell
node scripts/generate_rust_backend_interface_inventory.cjs --output output/rust-backend-interface-inventory.json
```

检查清单发现的硬契约：

```powershell
node --test scripts/tests/rust-backend-interface-inventory.test.cjs
```

2026-08-19 当前生成快照为：130 个 Python 路由、96 个 Vue 消费契约和
63 个 Rust 路由；55 个 Vue 消费契约已由 Rust 覆盖，41 个仍待迁移，且不存在
“Vue 已消费但 Python Stable 无对应路由”的悬空契约。

当前所有权分组如下：

| 公开边界 | Rust 目标 | 已实现 | 待迁移 |
| --- | --- | ---: | ---: |
| 文件/工作区 | `coomi-services::workspace boundary` | 22 | 0 |
| Git | `coomi-services::storydex_project` | 14 | 0 |
| WIKI/Story Knowledge | `coomi-services::storydex_project + wiki projection` | 5 | 0 |
| 前端实际 Story 状态 | `coomi-services::story domain` | 4 | 0 |
| System | `storydex-agentd::system boundary` | 8 | 2 |
| Agent | `storydex-agentd` | 8 | 8 |
| Coomi 控制面 | `storydex-agentd + coomi-services` | 2 | 6 |
| Auth | `candidate auth boundary` | 0 | 9 |
| Help | `coomi-services::help domain` | 0 | 4 |
| Presets | `coomi-services::preset domain` | 0 | 12 |

脚本会把存在 Vue 消费者但没有 Rust 路由的契约标为 `pending`；只有在
`apps/frontend/src/api` 中没有消费者、且候选也未注册 Rust 路由时，
Python-only 路由才会标为 `excluded` 并记录排除依据。Rust 已实现但不属于
Python Stable 公开路由的候选专用接口也会单独统计。生成过程不会访问任何
真实用户项目。
