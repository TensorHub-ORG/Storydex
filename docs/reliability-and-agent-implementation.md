# Storydex 可靠性与 Agent 实施记录

日期：2026-10-27。按用户允许直接重构的要求实施。代码留在本地工作区，未提交或推送。

本记录描述第一轮实现；pi 实测后暴露的审批、大文件、章节篇幅等问题及后续修复，见 [实测问题修复与复测说明](reliability-fixes-20261027.md)。下面第一轮本地验证不能替代该次实测结果。

## 文档保存和编辑状态

- 保存只推进本次提交的基线与 SHA-256，不覆盖等待响应期间的新输入。标签切换保留草稿；关闭未保存标签先保存，失败或仍有新输入则保留标签。
- `/file/write` 要求 `expectedContentSha256`，缺失返回 428，磁盘版本变化返回 409 及当前磁盘文档。读写请求可绑定 `workspaceRoot`，旧项目响应不会覆盖当前项目编辑状态。
- 主工作台将未保存文档和冲突按项目根目录保存在 localStorage；打开、新建、初始化、欢迎页及重载流程保留并恢复草稿。存储失败会显示错误并中止切换。
- Agent 结束或刷新文件树后重新读取打开的文档：干净文件更新，有草稿时保留内容并显示冲突。来源文件被外部删除也保留草稿。
- 冲突提示提供“查看磁盘版本”和“用当前内容解决冲突”；后者以当前磁盘版本为新基线提交，若再次变化仍会报冲突。磁盘副本保留为只读标签。
- 独立预览窗口保留保存期间输入，支持版本冲突查看与解决；重新读取失败或发生冲突不清空草稿。大文件的分页片段只读，完整读取后才允许编辑。
- 删除设置保存成功后重复写文件、失败再直接写文件的逻辑，设置由后端接口统一保存，随后同步打开的设置文档。
- 编辑器写入、Agent 的 `write_file`/`edit_file`/`apply_patch`、WIKI 同步/审核及设置接口使用 `.storydex/.agent/locks/project-write.lock`，锁覆盖检查与写入；工具等审批时不占写锁。Agent 整文件替换需要当前读取版本，已读取文件的局部编辑拒绝陈旧版本。

## Agent 工作方式

普通任务不再经过关键词意图分类、固定资产澄清、按资产类型限制读取目录或“一次写入后停止”。删除无生产入口的专用原地章节改写流程及其专用测试。一般增删改查与跨文件任务交给模型，通过项目说明、工具结果和实际权限执行。

当前文件只作上下文；请求还携带所有非临时的未保存文档，包括内容与磁盘基线版本。模型得到项目目录约定、WIKI 来源职责、查找和修改步骤的说明，工具继续执行目录、只读、计划模式及审批边界。

Agent 面板故事设置中的“本次任务”默认普通任务。选择“生成新章节”时，单次请求使用 `taskMode=generate_chapter`，保留章节模板、分片和长度验证；发送后恢复普通任务。`ask_approval` 模式通过原有审批通道确认章节操作，计划模式保持只读。普通修改不触发章节字数门禁。

## WIKI 与关系

图谱 `includeReview` 参数使用布尔字符串；HTTP 错误及投影不可用状态都显示错误，不解释为无数据。

关系候选写入 `.storydex/knowledge/relations.json`：

```json
{
  "relations": [{
    "id": "relation-1",
    "subjectId": "source:项目实际节点ID",
    "predicate": "姐姐",
    "objectId": "source:另一个实际节点ID",
    "sourceRefs": [{ "path": "chapters/1.md", "excerpt": "可在原文中定位的关系证据" }]
  }]
}
```

节点 ID 从 `StorydexWikiQuery` 取得，不应按示例文本推测。`quote` 与 `excerpt` 均可作为引文。`StorydexWikiSync` 从当前来源生成持久化投影，并遵守工具权限和审批。只在文本中共同出现不能作为关系证据。

- 新增关系待审列表、确认和拒绝接口；`expectedFingerprint` 过期返回 409。
- 审核保存在 `.storydex/knowledge/relation-reviews.json`。决策绑定候选与来源内容指纹；无变化同步、重新读取和强制重建保留决策，来源变化重新待审。
- 确认要求真实端点、非空谓词和可匹配的来源引文。删除的候选保留为过期记录；旧投影中的关系迁为待复核记录，便于整理来源。
- 同步错误保留上次图谱，并写入过期/错误状态；候选修复后可以恢复同步。
- `GET /story/evolution-snapshot` 读取旧状态面板实际使用的 `.storydex/memory/current/` 文件，JSON 无效时报告错误。

## 字体与接口证据

`EditorPane` 显式将父级属性传到主容器，倍率影响实际字号变量。组件测试验证 150% 对应 24px。

接口盘点扩大到全部前端 `.ts`/`.vue`；当前 107 个前端契约中 104 个有 Rust 路由，仍缺失的三个旧 Story 状态接口见 [接口清单](rust-backend-interface-inventory.md)。静态门禁明确记录已知缺口，不再声称功能全部覆盖。

## 本地验证

仅运行聚焦单元、组件和进程内 HTTP 路由测试，不使用真实模型或浏览器。以下为最终相关运行结果，重叠测试不累加成总数：

| 检查 | 结果 |
| --- | --- |
| 前端 workspace、helpers、字体、WIKI、预览组件 | 5 文件 / 43 测试通过 |
| 前端 workspace、Agent stores、AgentPanel、followup | 6 文件 / 113 测试通过 |
| 补充 Agent 草稿上下文及单次章节模式后 | agentStore 35 测试通过 |
| 最后补充文件读取竞态后 | workspace 与 helpers 21 测试通过 |
| 最后补充 HTTP 成功但投影过期的错误展示后 | StoryStatePanel 14 测试通过 |
| 前端 `npm run type-check` | 通过 |
| Rust chat（含模型任务契约、章节审批） | 23 测试通过 |
| Rust agent_control | 11 测试通过 |
| Rust knowledge | 5 测试通过 |
| Rust 项目写锁 | 1 测试通过 |
| Rust 文件工具 | 8 测试通过 |
| Rust reliability HTTP 路由 | 2 测试通过 |
| Rust 工作区项目创建/切换/写入 HTTP 路由 | 1 测试通过 |
| Rust 章节生成模块 | 15 测试通过 |
| Rust story helpers / 设置与系统持久化路由 | 2 + 1 测试通过 |
| Rust bridge WIKI 权限 | 1 测试通过 |
| Rust agentd / bridge Clippy（all-targets、no-default-features、warnings 为错误） | 通过 |
| Rust agentd 格式检查 | 通过 |
| 接口清单 Node 测试 | 3 测试通过 |
| 轻量仓库检查（编码、冲突标记、版本、Git 空白） | 通过 |

Rust 聚焦命令在 `apps/desktop/agent-runtime` 运行，使用 `cargo test -p <包> --lib <过滤词> --no-default-features`；bridge 使用 `--bin storydex-coomi-bridge`。前端在 `apps/frontend` 使用 `npm exec vitest run <测试文件>`。完整接口 JSON 可用清单中的命令重新生成到 `output/rust-backend-interface-inventory.json`。

## 剩余问题和证据范围

- `POST /story/character-conflicts/resolve`、`POST /story/rollback`、`POST /story/rollback/undo` 仍未迁入 Rust。本轮没有实现占位响应。
- 没有运行真实模型、浏览器/E2E、Tauri 原生生命周期、更新器、完整 Git 流程或跨平台打包。测试证明协议和状态行为，不证明模型在所有自然语言任务中的完成质量。
- 主工作台草稿依赖本机 WebView localStorage；独立预览窗口本轮处理保存/刷新竞态，未新增跨窗口关闭后的草稿恢复。
- shell、外部程序及 Git 操作不统一遵守项目写锁；版本校验不能表述为任意来源的绝对原子并发保证。`apply_patch` 使用补丁上下文并持锁，未新增跨轮读取版本账本。
- 旧投影写入接口仍存在；标准关系维护应通过候选与审核账本，再执行同步。直接修改投影不能代替来源记录。
- F12 性能问题保持线索，本轮未做归因或性能改造。
- 原有 `apps/desktop/tauri-preview/Cargo.toml` 用户改动已保留。计划文件位于仓库忽略的 `docs/plans/` 下，本轮已更新本地内容；本实施记录位于可跟踪的 `docs/`。
