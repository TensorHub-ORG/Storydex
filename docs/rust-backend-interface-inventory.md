# Rust 后端公开接口覆盖清单

更新日期：2026-10-27

这是可再生的静态接口清单，用来发现前端调用与 Rust 路由的差异。路由存在不代表请求参数、权限、持久化和界面行为已经通过验证。

## 扫描范围

`scripts/generate_rust_backend_interface_inventory.cjs` 对比以下边界：

- `apps/frontend/src` 下所有 `.ts` 和 `.vue` 文件，包括组件内直接调用；
- `storydex-agentd` 注册的 Axum 路由；
- Python/FastAPI 历史路由，仅用于迁移对照。

脚本识别跨行泛型调用、模板字符串动态路径和章节模板的显式备用路径。每个归一化契约记录 `implemented`、`pending` 或有依据的 `excluded`。它不启动服务、不读取用户项目，也不校验任意动态构造的 URL 或参数类型。

## 当前结果

2026-10-27 本地生成结果：

| 指标 | 数量 |
| --- | ---: |
| Python 历史路由 | 99 |
| Vue 消费契约 | 107 |
| 已有 Rust 路由的 Vue 消费契约 | 104 |
| 缺少 Rust 路由的 Vue 消费契约 | 3 |
| Rust 公开路由 / 已实现契约 | 108 |
| 排除的历史契约 | 15 |
| 无 Python 对照的 Vue 消费契约 | 23 |
| 无 Python 对照的 Rust 路由 | 27 |

原文的“96/96、待迁移 0”只扫描 `src/api`，遗漏了组件内调用，不能作为产品功能完整的证据。

| 公开边界 | 已实现 | 缺失 | 排除的历史接口 |
| --- | ---: | ---: | ---: |
| 文件/工作区 | 22 | 0 | 0 |
| Git | 14 | 0 | 0 |
| WIKI / Story Knowledge | 8 | 0 | 0 |
| Story 状态 | 5 | 3 | 10 |
| System | 10 | 0 | 1 |
| Agent | 16 | 0 | 0 |
| Coomi 控制面 | 8 | 0 | 0 |
| Auth | 9 | 0 | 3 |
| Help | 4 | 0 | 1 |
| Presets | 12 | 0 | 0 |

已知缺口均来自 `StoryStatePanel.vue` 的旧功能：

- `POST /story/character-conflicts/resolve`：角色状态冲突裁定；
- `POST /story/rollback`：故事回档；
- `POST /story/rollback/undo`：撤销故事回档。

这三个缺口仍保留为 `pending`，没有改成排除或实现占位成功响应。它们与本次补齐的 WIKI 关系确认/拒绝接口不同。

`excluded` 表示在扫描范围内没有找到 Vue 消费者、Rust 也未注册的 Python 历史契约。`frontendConsumersWithoutPythonRoute` 表示 Rust 独有契约，不是缺口指标。Windows 默认运行链路为 `Tauri 2 + storydex-agentd + Rust Coomi`。

## 生成与门禁

```powershell
node scripts/generate_rust_backend_interface_inventory.cjs --output output/rust-backend-interface-inventory.json
node --test scripts/tests/rust-backend-interface-inventory.test.cjs
```

JSON 为被忽略的本地产物，不提交。测试检查扫描范围、新增关系审核路由和上述三个已知缺口的精确集合；出现新缺口或修复旧缺口时都要求更新相应断言。门禁通过表示结果符合这份已知基线，不能称为“所有接口已覆盖”。

参数和行为需要单独验证。本次组件测试核对 `includeReview=true/false`、错误展示；Rust 路由测试核对图谱查询、关系审核指纹冲突和落盘结果。完整证据范围见 [可靠性与 Agent 实施记录](reliability-and-agent-implementation.md)。
