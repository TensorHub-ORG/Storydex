# Storydex 当前情况简要交接（2026-10-28）

> 最新进展：六章真实写作测试之后的修复见[连续写作可靠性记录](writing-reliability-20261029.md)，涵盖预算恢复、常驻设定、最近三章、已确认关系、整段改稿与失败稿恢复。下文为历史交接。

> 2026-10-29 后续：普通聊天流处理、章节索引/完成状态、图谱视图及审核名称已提交推送，见[本轮修复记录](iteration-fixes-20261029.md)。随后新增图谱持久身份、引文审核稳定性与可选自然写作指导，见[参考研究与实现记录](knowledge-and-writing-guidance-20261029.md)。原供应商长章稳定性仍待真实复测。下文保留 10 月 28 日交接时的事实与证据边界。

当前普通 Agent 已能连续讨论、保存设定、写短章、改稿并遵守审批；关闭页面后执行占用不释放的问题已修复并通过 pi 真实复测。下一步主要处理普通聊天长输出断流、章节列表与完成状态、图谱展示缺口。本次只整理交接文档，最新报告中的这些问题尚未修复。

## 代码与提交状态

- 仓库：`E:\Gitproject\Storydex-main`，当前分支 `main`。本次核对 HEAD、`origin/main`、`origin/dev/windows` 本地引用均为 `f978dd79f8100c9a6e33be116b2f6d949e87dfc5`，未重新 fetch 或查询远端 CI。
- 前一批已分三次提交推送：`15d8f35` 文件/WIKI 底层；`02cf18b` 工作台、审批、草稿、章节生成；`f978dd7` 文档、接口盘点、CI。详情见[交付记录](reliability-delivery-20261028.md)。
- 随后的普通 Agent 可靠性修复仍未提交、未推送。开始本次交接时共 8 项既有工作区改动；本次新增本交接文件，并更新旧验证文档中的断连验收状态。
- `core.hooksPath=.githooks`。提交、推送按根目录 `AGENTS.md` 执行：聚焦测试、提交前检查 main CI、推送前轻量检查；先 `dev/windows`，Development CI 通过后同 SHA 进入 `main`。

未提交的产品改动位置：

| 文件（相对仓库根目录） | 已实现内容 |
| --- | --- |
| `apps/desktop/agent-runtime/services/src/lib.rs` | 共用写作任务提示：创作/改稿默认工具落盘，讨论/示例可直接回答，按实际工具结果汇报 |
| `apps/desktop/agent-runtime/storydex-agentd/src/chat.rs` | 接入提示；监听响应流关闭，取消执行、完成收尾并释放占用 |
| `apps/desktop/agent-runtime/storydex-bridge/src/main.rs` | 默认提示复用，移除“一次修改后停止”的旧约束 |
| `apps/desktop/agent-runtime/engine/src/agent.rs` | 空响应最多纠正一次；截断/不完整响应不执行其工具；轮数耗尽明确失败，保留既有操作 |
| `apps/frontend/src/components/CoomiClaudeTurn.vue`、`apps/frontend/tests/AgentPanel.test.ts` | 工具成功/失败/未完成分开计数；调用次数不冒充文件数；“已完成”改“本轮结束”及测试 |

另有 `apps/desktop/tauri-preview/Cargo.toml` 原有修改，保留未动。未跟踪的[可靠性记录](agent-execution-reliability-20261028.md)包含实现与验证细节。

## 已验证到什么程度

- 此前本地聚焦测试：engine 14 项、agentd chat 24 项、bridge 1 项、前端 AgentPanel 39 项通过；类型检查、Clippy、格式和轻量预推送检查通过。
- 追加断连修复后，chat 模块 26 项通过（含新增两项），agentd Clippy、格式、差异检查通过；没有为此重跑全部组件。
- 上一轮[普通聊天实测](../output/playwright/pi-agent-nl-20261028/REPORT.md)：自然语言写作、续写、定点修改、讨论不写、拒绝/取消均通过；空回复/截断/轮数耗尽为 replay 注入，未覆盖真实供应商 HTTP/SSE 解析。
- 最新[连续写作报告](../output/playwright/pi-writing-multiturn-20261028/REPORT.md)：同一项目、同一持续会话，主线 21 个提示；加断连与探针共 25 条执行记录、94 个模型轮次、99 次工具调用，约 52 分钟。并非每轮首次成功，包含 3 次供应商失败及测试脚本中止。
- 最新实测确认：讨论与正式落盘能区分；角色卡、时间线、章纲及关系实际写入；三章约 1016/1164/1098 字符均保存；局部修改保留其他内容；“只影响后续”的设定变更没有回改前文；拒绝/取消不绕过。
- **断连 O1 已验收**：等待审批时关闭页面，执行自动以 `client_disconnected` 取消；重开后能切项目并完成新任务，不需手工 stop。保持连接等 30 秒再回答两问，正常继续、未误取消。旧文档中的“尚未浏览器复测”已更新。
- 本轮全部从普通聊天入口运行，不能用它否定第四轮显式章节生成入口中 6000 字成功的结果。两个入口的请求与预算不同。Tauri 原生窗口、打包、跨平台及长篇全书一致性仍未验收。

## 待处理问题与建议顺序

1. **P1：普通聊天长输出断流。** 当前模型为 `cline / deepseek-v4.1-flash-1017`，协议 `openai_responses`；“自动”实际为 high，失败请求输出上限 8192。3000 字章节连续两次断流，角色卡回合也曾失败。报错 `upstream stream closed before a terminal event`。同上游直接探针也在约 67 秒失败，支持上游链路问题，但尚不能确定具体网关限制，或证明调低推理、扩大预算、自动重试就能解决。先检查既有配置、结束事件和超时行为；不要把整轮工具执行直接重放。定位入口：`services/src/provider.rs`、`storydex-bridge/src/main.rs`、`engine/src/types.rs`（均在 `apps/desktop/agent-runtime/`）。
2. **P2：章节索引与文件支持不一致。** 模型写了 `chapters/第一章-显影.md` 等扁平文件；索引把 README 当章节，按路径排成第一/第三/第二。`storydex-agentd/src/story.rs` 的 `chapter_state`、`extract_chapter_number`、`update_chapter_completion` 已核对：只识别 ASCII 数字，完成状态要求目录。排序问题有真实接口证据；完成状态问题有源码依据，pi 的实际请求先被 `workspace_not_found` 拦住，不能记成已经复现 `chapter_not_directory`。优先补齐列表过滤、章节数字排序及扁平文件完成状态的一致性。
3. **图谱与审核展示缺口。** 3 张角色卡和放进 characters 目录的“人物关系.md”生成了 4 个角色节点；柳素贞、顾周氏、顾秉之未成为独立节点。10 条关系审核持久化，但角色视图只有 4 条边；需要区分实体缺失、来源文档分类和视图过滤，不能直接认定其他关系丢失。审核卡片显示 `source:` 内部 ID 是明确 UI 问题。定位：`services/src/storydex_project.rs`、`apps/frontend/src/components/StoryStatePanel.vue`。不要仅因正文提到一个人就擅自采用自动抽取系统。
4. **体验与内容小问题。** 非 Git 项目仍显示 `+0 -0`；存在年龄/错字/数字重复等内容摩擦，有的已修正、有的只在倒查时指出。只读倒查未自动改稿本身不算执行失败。部分回合重复读取、等待较久；统计约 1039 万输入 tokens，其中 98.8% 缓存，不能当成同量未缓存成本；输出约 10.24 万，推理约 6.41 万，口径可能包含关系，不要相加推算账单。

历史另列任务仍在：计划模式允许模型自行退出的产品定义；角色冲突裁定、故事回档、撤销回档三个旧接口；原生预览、多窗口并发及性能归因。不要把它们写成已修复。

## 接手时的约束与现成环境

- 用户明确倾向简洁提示和现有文件工具，让模型理解意图；不希望引入正文识别器、强制完成工具、聊天自动存章节或额外质量裁判系统。已有字数设计是目标引导，加用户自选的严格检查开关。
- 当前请求只是整理交接。后续修复在原实现上推进，保留所有既有修改；不要重做已通过项，不要擅自推送。
- 本次整理时确认端口仍监听：agentd `127.0.0.1:18126`（PID 25996），Vite `127.0.0.1:5190`（PID 1536）。进程状态可能变化，接手再检查；本次未停止服务。
- 隔离小说项目：`E:\Storydex-WritingE2E-20261028\青梧照相馆`。证据目录：`output/playwright/pi-writing-multiturn-20261028/`，重点读 `REPORT.md`、`evidence/turns/`、`evidence/graph-final2.json`、`evidence/story-chapters.json`、`evidence/disconnect/`。
- `output/` 是本地忽略目录，报告不会随 Git 推送，勿提交原始会话、凭证、日志或隔离项目。本次未复制任何密钥。
- 用户本轮真实测试交给 pi；后续没有明确授权时，不自行启动 Playwright/E2E 或付费模型长测。修复后先跑直接相关的聚焦测试，再给 pi 小范围复测提示。
