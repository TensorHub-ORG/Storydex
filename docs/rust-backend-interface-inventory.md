# Rust 后端候选公开接口清单

本清单由 `scripts/generate_rust_backend_interface_inventory.cjs` 从当前
FastAPI 路由和 Vue 工作台 API 消费者生成。它是迁移追踪资料，不是把
Python Stable 标记为已迁移的声明；Stable 仍固定使用
`Electron + Python/FastAPI + Rust Coomi bridge`。

生成完整 JSON 清单：

```powershell
node scripts/generate_rust_backend_interface_inventory.cjs --output output/rust-backend-interface-inventory.json
```

检查清单发现的硬契约：

```powershell
node --test scripts/tests/rust-backend-interface-inventory.test.cjs
```

当前所有权分组如下：

| 公开边界 | Rust 目标 | 当前状态 |
| --- | --- | --- |
| Agent HTTP/SSE、控制面、Coomi 状态 | `storydex-agentd` + `coomi-services` | 已有 Agent fixture parity；持续扩展 |
| WIKI/Story Knowledge 投影与 Git | `coomi-services::storydex_project` + WIKI projection | primitives 已建立，路由差分进行中 |
| 文件/工作区边界 | `coomi-services::workspace boundary` | 复用安全路径契约，待 API 闭环 |
| Story 生成、章节、回滚 | `coomi-services::story domain` + `storydex-agentd` | short 已闭环，medium/long 正在接入 |
| Preset、Help、System、Auth | 对应 Rust domain modules | 清单已冻结，按前端实际消费者迁移 |

脚本会报告 Python 路由没有对应前端方法签名的内部接口，并将所有
Python-only 行为保留为显式待迁移项；不会访问任何真实用户项目。
