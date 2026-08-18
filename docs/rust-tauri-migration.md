# Storydex Rust 后端与 Tauri 桌面重构计划

- 状态：执行中（M3 已扩展 Rust WIKI/Git 基础边界与单片段 medium/long 本地切片，M6 已建立隔离 Tauri 2 预览骨架；完整差分、sidecar 生命周期与 12.0 验收仍未完成，Stable 未切换）
- 建立日期：2026-08-05
- 最近修订：2026-08-18
- 适用范围：Storydex 2.x 稳定维护、完整 Rust 后端候选、Tauri 2 桌面候选与旧运行时退出准备

## 0. 新对话执行总目标与硬边界

从 `e8a2e0267995b741f0b095a8b207af2a12abd42c` 起，后续新对话的单一工程目标不再是继续扩充 Agent 控制面 fixture，而是把尚未完成的迁移连续推进到“完整 Rust 后端 + Tauri 2 桌面候选可构建、可测试、可打包”的状态。实现可以跨越多个里程碑、并行处理独立工作流，不需要为每个小切片重新等待人工授权。

这里的“全部完成”指目标候选的代码、契约、迁移工具、打包入口、自动化验证和文档均闭环；不表示切换 Stable 发布通道。现有 Stable 生产链路继续使用 `Electron + Python/FastAPI + Rust Coomi bridge`，直到用户在后续独立请求中明确授权激活。

新对话已获授权的范围：

- 完成 Rust `storyGeneration`、WIKI/Story Knowledge、Git 收尾及其余 Python-owned 后端能力的迁移和差分。
- 建立完整 Rust 后端候选，迁移所有仍被前端和桌面使用的公开接口；不为无消费者的历史内部实现做逐行翻译。
- 接入独立 Electron Rust Beta 和 Tauri Preview/候选构建，完成桌面生命周期、安装、更新、回滚和打包态验证。
- 从目标 Rust/Tauri 候选的构建输入和运行时依赖中移除 Python/FastAPI/Uvicorn 与 Electron/Node；Stable 参考实现源码保留到正式激活获得单独授权。
- 复用 `apps/desktop/agent-runtime` 和现有 Vue/Vite 工作台，以 `/api/v1`、SSE、`.storydex` 文件格式、Git 副作用及前端可见行为为兼容边界。

仍然禁止：

- 不改变 Stable 的启动命令、默认运行时、正式更新源或正式安装资产，不向 Stable 用户发布候选实现。
- 不访问或修改真实用户项目；写入、升级和回滚验证只使用仓库 fixture、临时克隆或脱敏副本。
- 不引入单向项目格式升级，不删除人工回滚所需的 Stable 参考分支/源码/签名产物。
- 不用静默 fallback、吞错、宽松差分、降低覆盖率或跳过安全门禁来制造“完成”。

执行方向只规定目标和兼容边界，不规定每个内部模块的固定拆分。强模型应先读取现有调用链，随后自主拆分和并行推进可独立验证的工作流；不得为了“重构完整”增加无消费者的中间层、额外模型调用或重复上下文构建。里程碑是集成与验证门禁，不是要求串行等待的任务队列。

### 0.1 2026-08-18 当前执行状态

本轮实际进度按里程碑记录如下：

- **M0：当前 Agent Refactor 事实窗口已完成。** 固定 Windows 机器、同一 replay fixture、3 次丢弃预热、每实现/场景 20 个正式样本和 60 秒空闲 RSS；`end_to_end_relative_gate` 的 24 项检查全部通过。真实 LLM 总耗时仍只作 Provider 证据，不作为 Rust 本地收益。生产观察期和 Beta 前 release 组件稳定性复核仍未完成。
- **M1：Agent 外部语义已扩展冻结。** runtime manifest、health/Coomi status 和 `chat/stream` 状态机已建立；22 组 fixture 在原 20 组控制/会话场景上增加 story intent/context/permission/asset/update/knowledge 外部摘要及首个真实故事写入契约。Rust 已在隔离 fixture 执行单片段 `create_new` 短章节差分，并与 Python 共享 v2 candidate 长度档校准记录；medium/long 已接入同一 Rust 单片段本地链路，但 Python/Rust replay 差分尚未补齐。
- **M2：骨架、依赖审计和当前 Agent 切片已实现。** `storydex-agentd` 仍是未接入 Stable 的独立 loopback 服务。仓库固定 `cargo-deny 0.20.2`、官方 RustSec advisory-db、许可证 allowlist 和 crates.io source 门禁；无 advisory ignore。`ratatui 0.30.2` 已移除旧依赖图中的 `lru 0.12.5` 漏洞版本。
- **M3：受控 Agent Refactor 与首个真实故事切片达到当前 fixture parity，并已开始项目服务迁移。** 除既有读写、取消、审批、follow-up、会话和 replacement 外，黑盒故障模型已覆盖两个同时 pending approval、两个独立 agentd 的 mailbox 竞争以及父 agentd 崩溃后的 bridge 控制 EOF 取消；Rust `create_new` 单片段 short 已完成差分，medium/long 已完成分档门禁、动态 Provider 输出上限、SSE/accounting、校准隔离、Provider 失败、预取消和原子写入本地测试。Rust 还建立了 canonical JSON/source/graph checksum、原子 WIKI bundle、受边界约束的本地 Git primitives，以及 4 个 Git、2 个 WIKI 候选路由；revision、last-good、ChangeSet、完整 WIKI/Git 路由与其他故事模式仍未闭环。
- **M4-M5：公开接口迁移已有可重复清单，但完整 Rust 后端尚未形成。** 仓库生成器当前盘点 130 个 Python 路由和 78 个前端 API 消费签名，并保持零个“前端消费者无对应 Python 路由”；该清单只用于追踪所有权和待迁移契约，不把未实现路由标记为完成。
- **M6：隔离 Tauri 2 Preview 骨架已可本地 `cargo check/build/clippy`。** 预览 crate 使用独立应用标识、Vue `dist`、最小 `core:default` capability 和独立 Rust 候选资产门禁，Stable Electron 的入口、打包和更新源未改变。当前只暴露 `runtime_info`，尚未完成动态端口/token、sidecar 启动/监控/关闭、`window.storydexDesktop`、签名更新、安装/回滚及打包态 E2E，因此不能视为 Tauri 候选完成。

这里的“真实主链路”只指 Storydex 自身 `providers.json` 当前激活的 Provider。Replay 报告始终标记 `providerMode=replay`，不得替代 live 证据或被描述为实际 Provider 成功。

### 0.2 基线、分支与生产边界

- 新对话起始基线为 `e8a2e0267995b741f0b095a8b207af2a12abd42c`；该提交上的 `dev/windows` Development CI `32097308639` 与 `main` 完整 CI `32097720408` 均为成功基线。
- 每个准备同步到 `main` 的可验证交付块都必须先推 `dev/windows` 并等待该 HEAD 的 Development CI 最终 `success`，再推 `main` 并等待同一交付内容的完整 CI 最终 `success`；不得在已知失败的远端 CI 基线上继续堆叠推送。
- 本轮依赖审计、story 外部语义、控制面故障模型、M0 性能窗口、Windows session 路径竞态修复和文档属于同一 Refactor 收口块；不得拆出一个绕开完整契约证据的 Stable 切换提交。
- Stable 继续固定使用 `Electron + Python/FastAPI + Rust Coomi bridge`。`storydex-agentd` 只允许 Refactor CI 的隔离 fixture workspace，不得读取真实用户项目，不得静默 fallback，也未接入当前桌面启动或打包入口。
- 最新 live 脱敏决策报告为 `output/rust-migration-decision-live/eb2805f48eed-20260818T014444-m0-performance-gate/decision-report.json`；story 语义依据为 `output/rust-migration-decision-live/eb2805f48eed-20260817T213817-story-semantics/decision-report.json`。两者均经过正常 Python Backend HTTP/SSE + Rust bridge，而不是 `storydex-agentd`。

### 0.3 2026-08-18 chat/SSE Refactor 差分与 live 证据

- 基础执行：只读成功与 Provider 请求不匹配报告均通过；成功场景为 `read_file` 一次、`AgentCompleted -> done`，错误场景为 `AgentError -> done`，两端均使用 `OPENCODE/deepseek-v4-flash` replay。
- 生命周期：手动 stop、执行超时均为 `AgentCancelled -> done`，客户端断连在观测流中不伪造业务终态；`RunAccepted` 后、runtime 启动前的 stop 及重复 stop 也已覆盖，只有首次请求被接受，且不会启动 bridge/模型。控制面 fixture 通过明确事件锁定触发点，避免依赖偶然调度时序。
- 审批控制面：`request_user_input` 允许、拒绝和超时均通过；同一 request 的重复决策以及超时后的 late decision 均返回未领取，不会重复恢复 waiter 或执行工具。Rust 不再额外向公开 SSE 暴露 `PermissionResolved`。
- Follow-up 控制面：持久 queued follow-up/resume、steer 中断后显式恢复、pending/steering 编辑与删除均通过；重复 enqueue/PATCH/DELETE 不增加 revision 或事件。存储写失败保持原 mailbox 障碍物不变并返回明确错误。直接消费 queued follow-up 时，Rust 与 Python Stable 一样不重复发出 `ContinuationStarted` SSE，但 mailbox 持久事件仍保留。
- 受限写：两份临时克隆均只修改 `.storydex/characters/fixture.md`，文件 SHA、Git HEAD/状态和变更路径一致；Python Stable 额外生成的 WIKI 投影文件作为明确的 Python-owned 派生副作用单独验证，未从报告中隐藏。
- 会话故障：损坏 binding、损坏 session、缺失 session 和 workspace mismatch 均 fail closed，原始文件保持不变；关键序列按故障发生边界保持 `RunAccepted -> TurnContract -> [AgentStarted] -> AgentError -> done`。
- Replacement：成功场景中旧 trace 为 `superseded/accepted`，旧 prompt 从 runtime session 移除，新 prompt 保留；隔离 bridge 启动失败场景中旧 trace 为 `completed/restored`、新 trace 为 `failed`，setup 后的 runtime-session SHA 和 2 条消息原样恢复。两端报告分别位于 `differential-replacement-current` 和 `differential-replacement-restore-current`。
- Story 外部语义：live 决策选择 `external_semantics_contract_gate`。`story-semantics` fixture 冻结 intent、permission、turnPlan、context、asset/update 和 knowledge policy；`storyGeneration` 只允许 `fragmentCount=1..20`、`chapterLengthTier=short|medium|long` 和受控 template id。三次差分均通过；Python-only 仍是 5 个 WIKI 派生路径和对应领域事件，Rust-only 为空。
- Story 真实写入：`story-create-new-short` fixture 使用显式 replay 和两份隔离临时项目，冻结一次逻辑调用、一次 Provider 尝试、零 transport retry，以及 `StoryProviderAttempt`、提交、测量、验证、调用计数、唯一终态和磁盘副作用。Python/Rust 均写入 `chapters/第1章 未命名/001.md`，Windows 文件为 4603 字节，SHA-256 为 `8b136a984e034b1919d681c6b9b14653e8821fe0a7d2983d6ed224ed147409ae`；双方还严格对比 `.storydex/memory/length_tier_calibration.json` 的 v2 candidate 样本、观察值、cold-start 状态和三档 band。Python-only 仅剩 5 个 WIKI 文件、`GitCommitPrompt` 和 `KnowledgeProjectionUpdated`，均被显式验证，未加入 ignore。
- 控制面韧性：`output/agent-runtime-contract/control-resilience-current/acceptance-report.json` 通过。两个独立 agentd 并发写入保留两条消息、revision/eventCount 均为 2；两个同时 pending approval 均仅接受一次；强杀父 agentd 后真实 bridge 通过控制管道 EOF 退出，孤儿数为 0。
- M0 性能与 live 决策：`output/agent-runtime-contract/m0-performance-current/performance-report.json` 的 24 项相对门槛全部通过。`output/rust-migration-decision-live/eb2805f48eed-20260818T014444-m0-performance-gate/decision-report.json` 经 Stable live 主链路选择 `end_to_end_relative_gate`，一次 `read_file`、Provider HTTP `200`、0 retry、0 fallback、终态 `AgentCompleted`。
- 依赖审计：`scripts/run_rust_dependency_audit.ps1` 对 advisories/licenses/sources 全部通过；本次本地 RustSec revision 为 `69f93e1d081d8b6fbee010e48f0b5e0d13661415`。CI 仍在每个相关开发/完整门禁中重新安装固定版本并获取官方数据库，不把本地 revision 当成永久快照。
- 差分入口为 `python apps/backend/scripts/run_agent_stream_differential.py --fixture-dir <fixture> --output-dir <report-dir>`；失败报告同样持久化。22 个受支持 fixture 已列入 `agent-chat-stream-v1.json` 和 runtime manifest。

### 0.4 M0 不稳定行为与兼容决策台账

| 编号 | 可观察差异或风险 | 当前定性与影响 | 复现证据 | 后续处理 |
| --- | --- | --- | --- | --- |
| AGENT-001 | Python 多数场景额外发出 `GitAutoCommit` 或 `GitCommitPrompt` | Python-owned 项目 Git 收尾；当前 Rust fixture 切片不复制该领域事件，不影响核心终态，但阻断 Stable 接管 | 各 `differential-*-current` 报告的 `eventKindDifferences` | 迁移项目/Git 服务时补齐，不扩大 ignore 字段 |
| AGENT-002 | scoped-write 后仅 Python 生成 `KnowledgeProjectionUpdated` 和 WIKI 派生文件 | 这是当前生产投影副作用，不是可删除噪声；Rust 仅比较目标文件并显式验证 Python 派生文件 | `differential-scoped-write-current` | WIKI/Story Knowledge 迁移前保持 Python-owned，Stable 切换前必须闭环 |
| AGENT-003 | approval 场景曾仅由 Rust 额外发出 `PermissionResolved` | 审批 HTTP 响应、bridge `ToolDone` 和终态已提供可观察结果，仓库无该事件消费者；本轮按 Python Stable 隐藏该额外 SSE，两个同时 pending approval 的黑盒案例也已通过 | approval 差分与 `control-resilience-current` | 保持公开事件面一致；剩余 permission mode 随对应业务切片处理 |
| AGENT-004 | queued follow-up 的 `ContinuationStarted` 曾在两端落点不同 | 本轮直接消费入口不再由 Rust 重复发 SSE，mailbox 的持久事件与 Python 一致；OS 文件锁已关闭两个独立进程的丢更新竞态 | followup 差分与 `control-resilience-current` | 保持当前唯一公开来源；生产桌面生命周期恢复仍在 Beta 前验证 |
| AGENT-005 | Provider 请求阶段错误会在已有 `ProviderStream` 后接受 replacement | 这是现行 Stable 边界；该错误不能冒充“启动失败恢复” | provider-error 与 replacement 报告 | 使用 bridge spawn 故障验证未接受恢复；保留两类独立 fixture |
| AGENT-006 | Rust 源码变化后旧 bridge 会被运行时指纹门禁拒绝 | 正确的 fail-closed 构建约束，不允许绕过 | `scripts/verify_coomi_runtime.py` 与 Backend 启动检查 | 每次 Rust 改动后重建当前提交产物，CI 继续校验 |
| AGENT-007 | Windows 扩展路径与普通盘符路径在 `resolve()`/`samefile()` 多次文件查询间遇到 session 原子替换会被误判为越界 | 原始 bound 路径先做严格的无 I/O 表示等价比较，只折叠 Windows 扩展前缀、分隔符和大小写，不折叠 `.`/`..`；表示不等价才调用 `resolve()`/`samefile()`，真实逃逸仍 fail closed | expected/扩展盘符/扩展 UNC 快路、父路径段强制慢路和 symlink 逃逸回归，以及最终 20 样本性能窗口 | 保留竞态与逃逸回归；不得用忽略 session restore 错误或创建新 session 替代 |
| AGENT-008 | Rust debug 的 `componentInit/sessionInit` 有长尾抖动 | 最新正式窗口只读 p95 比值为 `17.9373x/21.6833x`，取消为 `2.1476x/2.3881x`；端到端 24/24 仍通过，但只读两项已进入 `investigate_before_beta`，不能以 median 或端到端收益掩盖 | M0 performance report 的 `comparisons` 与 2 个 `diagnosticInvestigations` | Beta 集成前用 release 构建复核组件初始化稳定性并关闭两项调查；完成后可按 0.7 继续，不需重复授权 |
| AGENT-009 | stop、snapshot 与 finalizer 并发 read-modify-replace execution intent 时，Windows 曾返回 `PermissionError [WinError 5]`，且旧状态可能覆盖取消或迟到 stop 被虚假接受 | handle 统一按 state lock → intent lock 串行状态变更与 intent 写删；finalization 建立后 stop 返回未接受，关闭 intent 后写入显式失败；活动 intent 只有缺失可新建，权限错误、损坏 JSON 或非对象均 fail closed；`os.replace` 仅做 10/25/50ms 有界重试 | 正反序 snapshot/cancel、删除窗口 late cancel、严格读取、日志、瞬时冲突和永久失败回归，以及最终 M0 窗口 | 保持 fail closed；不得吞错、无限重试、按空对象重建损坏 intent 或删除 intent 掩盖 |

### 0.5 M2 依赖审计现状

- 仓库固定 `cargo-deny 0.20.2`，统一入口为 `scripts/run_rust_dependency_audit.ps1`；脚本校验工具版本并以 `--locked` 执行 advisories、licenses、sources 三类检查。
- `apps/desktop/agent-runtime/deny.toml` 只使用官方 `https://github.com/RustSec/advisory-db`，许可证采用显式 allowlist，source 只允许 crates.io；没有 advisory ignore。本次本地数据库 revision 为 `69f93e1d081d8b6fbee010e48f0b5e0d13661415`。
- `ratatui` 从 `0.29` 升为 `0.30.2`，依赖图中的 `lru` 从有漏洞的 `0.12.5` 收敛为 `0.18.2`。当前 advisories/licenses/sources 均通过；Development CI 与完整质量门禁均重新运行固定审计，不能以一次本地成功永久豁免后续依赖变化。

### 0.6 剩余工作流与集成顺序

以下是剩余工程工作流，不是要求逐项等待人工确认的微型切片。能独立验证的部分应并行开发，在依赖点按顺序集成。完成其中一个切片、一次提交或一次 CI 只表示该交付块关闭，不是暂停整个重构的理由；同一对话应继续领取下一块，直到满足 12.0 的全部完成定义或出现无法自行消除的真实外部阻塞：

1. **故事写入闭环：** 首个 Rust 单片段 `create_new` 短章节切片已完成 Provider completion、Unicode 字数/质量门禁、路径规划、原子写入、调用计数、SSE、唯一终态及 short candidate 长度档校准差分；继续扩展其他故事模式、medium/long 校准集成、失败/取消/写入故障契约，并接入后续 Knowledge/WIKI/Git 闭环。
2. **知识与 Git 闭环：** 迁移 Python-owned WIKI/Knowledge Projection、canonical checksum、领域事件、Git/ChangeSet/回滚和收尾行为；现有 5 个 Python-only WIKI 路径必须转为显式 parity，不能加入 ignore。
3. **完整后端闭环：** 以实际前端/桌面消费者和路由 manifest 为索引，迁移配置、项目、预设、搜索、索引、诊断、文件和其余公开 API；每个写路径都保留磁盘与 Git 差分证据。
4. **运行时闭环：** 在 release 构建调查并关闭 `componentInit/sessionInit` 长尾，补齐崩溃恢复、进程树清理、端口/认证、日志、升级和回滚。
5. **桌面闭环：** 先完成独立 Electron Rust Beta 集成证据，再完成 Tauri 2 sidecar、窄桌面适配层、最小 capabilities、签名更新和打包候选；两者可以提前并行搭骨架，但最终集成必须使用同一完整 Rust 后端。
6. **旧运行时退出准备：** 目标 Rust/Tauri 候选不得携带 Python/FastAPI/Uvicorn 或 Electron/Node 运行依赖；Stable 参考源码与发布资产在未获激活授权前保持隔离和可回滚。
7. **最终验证：** 完成 Rust、Frontend、Desktop、打包、契约、故障注入、覆盖率、安全与依赖审计，更新 manifest、架构文档和完成定义，只按实际结果声明完成。

### 0.7 新对话直接执行规则

- 新对话从本节、0.6、12.0 和 13 直接开始实现，目标是连续完成所有剩余工作流，而不是再输出一份建议计划。先阅读实际代码、配置、调用链和测试；确认事实后可自行调整内部拆分，不需要为常规工程取舍反复询问。
- 一个新对话可以并且应当按风险边界形成多次中文提交和多轮 `dev/windows -> Development CI success -> main -> 完整 CI success`。每轮成功后立即从最新成功 HEAD 继续下一交付块；不得因为已经完成一次 push、一次纵向切片或一次 CI 就提前结束仍可安全推进的迁移工作。
- 已关闭的 21 组控制/会话 fixture、RustSec 审计方案和 M0 24/24 性能窗口只作回归基线，不重复扩充或重跑大样本，除非相关代码变化或失败证据要求重新验证。
- Electron Rust Beta、Tauri Preview 和目标候选打包已在本计划范围内获授权，可以在各自前置契约满足后直接推进；这不授权 Stable 激活、真实用户项目访问或正式更新源变更。
- 真实主链路验收、性能测量和兼容验证已获直接执行授权：可以在仓库 fixture、临时克隆或脱敏副本中通过正常 Storydex API/SSE 调用 `OPENCODE/deepseek-v4-flash`，不需要为每次实测再次请求授权；仍禁止接触真实用户项目。重大架构或产品兼容决策以这些 live 结果为主要依据，只有出现新的不可逆格式、外部行为取舍、运行时切换或旧实现删除决策时才新增 live 决策报告。Replay 只用于确定性差分，绝不能冒充 live。
- 默认由主代理连续推进；确有独立边界时最多使用 2 至 3 个子代理并行审查、实现或测试。共享工作区中的编辑必须分区，主代理负责整合、复核和最终验证。
- 所有 commit 使用中文。每次 push 前运行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_pre_push_ci.ps1`，不得使用 `--no-verify`；本地只运行与改动直接相关的聚焦检查，完整门禁交给 GitHub Actions。
- 推送顺序固定为：中文提交 -> pre-push 基础检查 -> 推 `dev/windows` -> 等待 Development CI 最终成功 -> 推 `main` -> 等待完整 CI 最终成功。任一 CI 失败都先读取具体 job/step、修复根因并重新验证，不能宣称该交付块完成。
- 当前架构事实仍是 Stable 使用 `Electron + Python/FastAPI + Rust Coomi bridge`。只有目标候选的实现与验证完成后，才能写“技术迁移候选完成”；在用户另行授权 Stable 激活前，不能写“Stable 已切换”“Python/Electron 已从稳定版退役”。

## 1. 结论

Storydex 采用可并行实现、按依赖集成、可回滚的重构：

1. 以已经达到受控 parity 的 `storydex-agentd` 为基础，完成故事生成、Knowledge/WIKI、Git 和其余公开后端能力，形成不依赖 Python fallback 的完整 Rust 后端候选。
2. Electron Rust Beta、完整后端迁移和 Tauri 适配可按边界并行实现；集成验证仍按“Rust 外部契约 -> Electron Beta -> Tauri 候选”排序，避免同时调试多个未知边界。
3. 新对话完成目标是可测试、可打包、可回滚的 Rust/Tauri 候选。Stable 激活是独立发布决策，不包含在本次工程授权内。

在用户另行授权 Stable 激活前，后续 2.x 小版本继续使用当前 `Electron + Python/FastAPI + Rust Coomi bridge` 生产链路。Rust 和 Tauri 候选不得进入 Stable 正式包、不得读取或写入真实用户项目，也不得通过静默 fallback 改变线上行为。

后端运行时切换应作为独立的主版本或明确的迁移版本发布，不应夹入修复型补丁版本。Tauri 切换不得与 Rust 后端首次正式切换发生在同一个版本中。

## 2. 目标与非目标

### 2.1 目标

- 保持现有 Vue 工作台的用户可见功能、交互路径和数据语义基本不变。
- 保持 `/api/v1` HTTP 接口、SSE 事件、错误 envelope 和状态码兼容。
- 保持已有小说项目无需转换即可由新旧后端读取，迁移期不引入单向磁盘格式升级。
- 保持 Agent、WIKI、Story Knowledge、字数门禁、回滚和 Git 边界的既有硬性规则；本轮允许改变 Agent 的实现语言，但不改变这些规则的语义。
- 降低冷启动、空闲内存、安装体积和本地计算耗时。
- 将并发、取消、恢复、文件事务和进程生命周期变为可测试的显式状态模型。
- 最终移除正式包中的嵌入式 Python、FastAPI/Uvicorn 依赖和 Electron/Node 桌面运行时。

### 2.2 非目标

- 不在迁移期间重做前端设计或改变主要工作流。
- 不改变 Agent/WIKI 产品规则、提示词约束、权限含义或主要工作流；迁移实现语言和运行时边界，不借重构改产品语义。
- 不从当前 Stable 构建和源码中提前删除 Python/Electron；目标候选必须摆脱这些运行时依赖，旧实现作为 Stable 参考和人工回滚基础保留到正式激活另行获批。
- 不把本地应用拆成网络微服务。
- 不为迁移方便降低现有测试、覆盖率、安全或发布门禁。
- 不以大量 `serde_json::Value` 逐句翻译 Python 动态字典；领域层应使用明确类型。
- 不承诺 LLM 网络请求本身会因桌面框架变化而显著加速。

## 3. 历史基线与本轮基线规则

| 项目 | 当前基线 |
| --- | --- |
| Python 生产代码 | 约 153 个文件、77,436 行 |
| Python 测试 | 64 个文件、24,576 行 |
| HTTP 路由 | 约 123 个路由装饰器入口 |
| 后端测试收集 | 946 项 |
| 后端覆盖率基线 | 行 84.7%，分支 70.8% |
| 关键大模块 | `story_project_service.py` 约 7,200 行；`story_wiki_service.py` 约 4,200 行；`routes_agent.py` 超过 6,600 行 |
| Agent Runtime | `apps/desktop/agent-runtime` Rust workspace，经 JSONL bridge 调用 |
| 正式桌面运行时 | Electron 40 + 内嵌 Python 3.9 + MinGit + Rust bridge |

上表是 2026-08-05 的历史盘点，不作为本轮 Agent parity 的最终数字。本轮 Agent 行为起点以 `40d37b0972f68e197b364c2570a88c7a42d8de40` 及 `docs/Agent运行链路信息完整性与性能治理.md` 中已有基线为准，并在首个切片前重新生成相关行为、性能和资源数据。全后端 123 路由的完整 manifest 可以作为后续 Rust 后端迁移工作，不应阻塞 Agent 首个切片。历史盘点中的完整测试未在当时的观察窗口内结束，因此任何“全套通过”都必须以实际可重复的命令和结果为准。

## 4. 发布隔离策略

### 4.1 四条运行轨道

| 轨道 | 运行时 | 使用者 | 用户数据权限 | 是否进入稳定更新源 |
| --- | --- | --- | --- | --- |
| Stable | Electron + Python + Rust bridge | 正式用户 | 正常读写 | 是 |
| Refactor CI | Rust 后端测试进程 | CI 和开发者 | 仅临时 fixture | 否 |
| Rust Beta | Electron + Rust `storydex-agentd` | CI 和明确隔离的开发验证 | 仅 fixture 或脱敏项目副本 | 独立 beta 源 |
| Tauri Preview | Tauri + Rust `storydex-agentd` | CI 和内部预览验证 | 仅 fixture 或脱敏项目副本 | 独立 preview 源 |

Stable 不得因其他轨道未完成而改变启动命令、依赖、安装资产或自动更新元数据。

### 4.2 分支与合并规则

- `main` 始终保持可发布，稳定功能和紧急修复优先。
- Windows 与 Android 分别在 `dev/windows`、`dev/android` 集成，两个分支只从 `main` 同步且不得互相合并；具体功能仍使用短生命周期分支。
- Rust/Tauri 候选可以接入明确隔离的 beta/preview 启动和打包入口，但不得被当前 Stable 启动脚本、默认 Electron 主进程、正式打包配置或正式更新源引用。
- 早期 Rust CI 使用独立 workflow，不加入现有 stable release 汇总；它只在重构相关路径变化时运行，且不使用 `continue-on-error` 掩盖失败。
- 当 Rust 测试稳定且对应切片声明完成后，再将该检查升级为必需门禁。
- 发布候选冻结后，不合入触碰当前生产启动、更新、项目写入和 Agent 执行链的重构改动。

### 4.3 小版本功能同步规则

重构期间出现稳定版缺陷或新的小版本需求时：

1. 先在当前生产实现中复现，并增加面向外部行为的回归测试。
2. 在当前生产实现中修复并按正常小版本发布，不等待未发布的 Rust 实现。
3. 将新增契约 fixture 同步给 Rust 实现；已标记为 parity 的切片必须在合并前同步修复。
4. Rust 尚未覆盖的切片只记录迁移欠账，不阻塞稳定版热修复。
5. 不允许只在 Rust 旁路实现面向正式用户的新功能。

这样会产生一段时间的双实现成本，但可以避免为了追赶重构而冻结必要的小版本修复。

## 5. 目标架构

以下是新对话需要实现并验证的完整目标；内部模块可以按实际所有权调整，但进程、安全和兼容边界保持不变。

```text
apps/frontend (Vue 3 + Vite)
        |
        | HTTP / SSE，迁移期保持 /api/v1 契约
        v
storydex-agentd (独立 Rust 进程)
        |
        +-- api          路由、envelope、SSE、认证、trace
        +-- application  用例、执行协调、取消和恢复
        +-- domain       Story、Knowledge、WIKI、Preset 等强类型规则
        +-- infrastructure
        |      文件事务、Git、索引、配置、日志、HTTP provider
        +-- coomi        复用 apps/desktop/agent-runtime 的稳定接口

迁移阶段桌面壳：Electron
最终桌面壳：Tauri 2，仅负责窗口、更新、权限、系统集成和 sidecar 生命周期
```

`storydex-agentd` 保持为独立进程，而不是把全部 Agent 工作直接放进 Tauri Core。这样可以隔离 Agent、Shell、Git 或第三方 Provider 异常，并保留独立重启、日志、健康检查和执行恢复能力。

Rust 后端建议从一个 workspace、一个核心 library 和一个服务 binary 开始。只有当编译边界、测试隔离或所有权确有收益时才继续拆 crate，避免用 crate 数量代替模块设计。

`storydex-agentd` 当前只承载受控 Agent 切片；新对话应把 WIKI、Story Knowledge、项目服务和其余实际使用的后端能力迁入同一明确的 Rust 领域/应用/基础设施边界。现有 `apps/desktop/agent-runtime` 已包含 engine、services、tools、security 和 bridge，优先复用或提取稳定接口，只有在所有权、编译边界或测试隔离确有收益时才新建 crate。迁移期与 Python Stable 的比较通过 HTTP/SSE、fixture 和显式文件契约完成，不建立需要长期保留的隐式跨进程状态。

## 6. 兼容契约

### 6.1 HTTP

- 保持 `/api/v1` 路径、HTTP method、状态码和 Content-Type。
- 保持 `ok/data/error/trace/audit` envelope 结构、camelCase 字段和缺省值语义。
- 区分字段缺失、`null`、空数组和空对象，不做宽松归一化掩盖差异。
- 保持验证错误、业务错误和未知错误的错误码边界。
- `traceId`、时间戳和临时绝对路径可以在差分测试中按明确规则替换，其他字段必须比较。

### 6.2 SSE 与执行状态

- 冻结事件名称、JSON payload、首包、heartbeat、阶段顺序和终止事件。
- 冻结 stop、cancel、steer、follow-up、disconnect、resume 和 approval 的状态迁移。
- 每次 Execution 只能出现一个传输层终止结果。
- 客户端断连不得默认为业务取消；两者必须保持当前契约。
- 事件流差分按有序状态机验证，不仅比较最终文本。

### 6.3 项目文件与 Story Knowledge

- 冻结 `.storydex` 目录、文件名、schemaVersion、revision、稳定 ID 和证据引用语义。
- 冻结 UTF-8/BOM、换行、JSON 排序中影响 checksum 的部分和 Unicode 字数算法。
- WIKI 继续是 Story Knowledge 的投影，不得变为第二事实源。
- Knowledge Command 的校验、待复核和拒绝写入规则保持不变。
- 迁移期若不可避免地扩展 schema，必须先做到新版本读新旧、仍写旧格式；确认旧版本回滚窗口结束后才能启用新写格式。

### 6.4 文件、路径与 Git

- Windows 盘符、大小写、UNC、符号链接、junction、`..` 和父仓库边界必须保持 fail closed。
- 写入继续使用临时文件、flush、原子替换和可验证的回滚步骤。
- Git 操作只允许当前 Storydex 项目仓库，不得吸收父仓库。
- 差分测试中的写操作只能在同一 fixture 的两份临时克隆中执行，禁止对真实项目双写。

### 6.5 Agent 行为契约

Agent 重构的 parity 以外部可观察行为为准，而不是以 Python/Rust 内部结构相同为准。至少保持以下语义：

- intent、权限模式、目标路径、上下文来源和 TurnContract 的约束一致；自然语言不能扩大已编译权限。
- 工具名称、调用顺序、参数校验、revision/span、截断状态、文件集合和 Git 变更集一致。
- SSE 事件名称、阶段顺序、heartbeat、唯一终止事件，以及 follow-up、steer、cancel、approval、resume 和 disconnect 状态迁移一致。
- Provider 的 HTTP 状态、异常类型、阶段、trace/session 和可安全展示的错误信息不能被压成笼统成功或失败。
- 生成文本允许因模型采样而不同，但必须满足相同的文件写入边界、字数/质量门禁、章节标记和项目语义约束。

读操作可比较规范化响应；写操作必须在两份相同的临时 fixture 上比较文件、revision、Git 状态和事件流。任何差异都要解释为 bug、已确认的不稳定行为或明确的兼容决策，不能扩大忽略字段来消除差异。

### 6.6 桌面与更新

- 保持目录选择、打开文件、文件定位、预览窗口、标题栏、单实例和退出清理行为。
- Tauri 更新使用独立的签名密钥和更新源验收，不复用未经转换的 Electron `latest.yml`/blockmap 契约。
- Stable、beta 和 preview 更新源必须隔离，稳定用户不得被自动切换到预览运行时。

## 7. 实施里程碑

### M0：先稳定当前实现

本轮 M0 只为 Agent 重构建立足够的事实基线，不要求先完成整个 Python 后端的治理。

工作项：

- 建立 Agent 不稳定行为台账，记录复现步骤、期望行为、影响范围、关联测试和临时规避方式。
- 优先覆盖 Agent 的 SSE、工具循环、权限、上下文、follow-up/steer/cancel/approval/resume、文件写入和错误透传。
- 修复 P0/P1 根因；无法立即修复的行为必须明确标为实验功能或阻断迁移，不得默默冻结成 Rust 契约。
- 固化 Agent 相关的 Python、Rust、前端和桌面聚焦测试命令及合理超时。
- 记录 Agent 冷启动、首个 SSE、工具轮、取消响应、上下文装配和 provider 错误基线；WIKI/全后端性能基线按原计划后续维护。

退出条件：

- 没有未处置的 P0；P1 均有复现测试和明确决策。
- 当前 Stable 质量门禁连续稳定通过，且不存在依赖偶然时序的测试。
- 性能基准可以在隔离目录重复运行并输出机器、数据集和统计口径。

### M1：冻结外部契约

工作项：

- 为 Agent HTTP/SSE、工具、错误和文件边界建立独立 manifest；全后端 123 路由 manifest 不作为本轮前置条件。
- 建立可接受任意 base URL 的 Agent 黑盒 HTTP/SSE 契约套件，覆盖读、受限写、审批、取消和恢复。
- 建立典型小说项目 fixture，记录执行前后目录清单、规范化 JSON、文件 hash、Git 状态、工具序列和变更集。
- 将前端 Agent API 类型与响应 schema 做字段级映射检查；OpenAPI 只作为索引，不能替代行为测试。

退出条件：

- 同一契约套件可以对当前 Python 后端独立运行。
- 关键写入流程可以在临时项目中得到确定性的前后差异。
- SSE 关键流程具有事件顺序和超时上限测试。

### M2：Rust 后端骨架

工作项：

- 建立不接入 Stable 的 Rust Agent 服务骨架；可以复用现有 `apps/desktop/agent-runtime` workspace，也可以在 `apps/backend-rs` 建立边界，具体选择以现有 crate 所有权和测试隔离为准。
- 建立 Agent core 与服务 binary 的最小边界，不为尚未迁移的 WIKI/项目服务预留空壳模块。
- 使用 Tokio、Axum、Serde、Tower 和 tracing 实现 health、版本、trace、统一错误和受控关闭。
- 默认只监听 `127.0.0.1` 的动态端口，使用每次启动生成的认证 token。
- 接入结构化日志、panic 边界、任务注册表、CancellationToken 和 shutdown deadline。
- CI 构建、格式化、Clippy、单元测试和依赖审计先独立运行。

退出条件：

- Rust 服务可独立启动、健康检查和确定性退出。
- 未被当前 Electron、打包脚本或稳定更新源引用。
- 基础错误 envelope 与 Python 契约一致。

### M3：按风险迁移业务切片

风险清单（用于安排依赖和验证，不是强制串行顺序）：

| 顺序 | 切片 | 原因 |
| --- | --- | --- |
| 1 | 纯函数、schema、字数和路径辅助逻辑 | 无外部副作用，适合建立 Rust 测试模式 |
| 2 | health、help、bootstrap、只读配置和只读项目接口 | 易做黑盒差分 |
| 3 | 搜索、索引、文件读取和诊断 | 可用固定大型 fixture 做性能比较 |
| 4 | 配置、预设和项目文件写入 | 开始验证原子写入与兼容格式 |
| 5 | Git、快照、ChangeSet 和回滚 | 副作用高，必须使用临时仓库 |
| 6 | Story Knowledge、WIKI 冷构建和增量同步 | 硬性语义多，要求 canonical checksum |
| 7 | 故事生成、字数控制、autopilot 和语义预算 | 领域规则密集，必须保持调用次数与写入门禁 |
| 8 | Agent 编排、SSE、follow-up、steer、approval、取消与恢复 | 并发和状态风险最高，最后迁移 |

每个切片必须完成：

1. Python 黑盒契约。
2. Rust 单元与集成测试。
3. Python/Rust 差分测试。
4. 失败注入、取消和恢复测试。
5. 对应性能基准。
6. 端点 manifest 状态更新。

任何切片未达到 parity 前都不得通过“返回空数据”“捕获所有异常”“自动重试”或跳过写入来假装兼容。

### 加速执行路径

上表中的第 8 项 Agent 控制面已经提前完成受控 parity，后续不应回退到从第 1 项重新串行搬运。实际执行使用以下方式：

1. 以 0.6 的七个剩余工作流为主线；首个 `storyGeneration create_new/short` 纵向切片已经关闭，下一步扩展其余故事模式并关闭 `Knowledge/WIKI -> Git` 真实写入链，同时盘点公开路由消费者、Rust 模块边界和 Tauri 窄适配层。
2. 领域规则、文件事务、API 适配、桌面生命周期和打包可以由边界清晰的并行任务推进；共享 schema、磁盘格式和 SSE 状态机由主集成线统一裁决。
3. 每个工作流达到可验证边界就更新 manifest 和差分证据，不要求每个函数或路由单独提交，也不等待重复人工批准。
4. 已有实现直接复用并补契约；无实际消费者的内部接口可以记录后删除，不为达到文件数量或 crate 数量制造空壳。

### M4：Electron + Rust Beta

M4 验证完整 Rust 后端候选在隔离 Electron Beta 中的桌面生命周期；Stable Electron 继续使用 Python 后端，不受影响。

工作项：

- 让 Electron 在独立 beta 构建中管理完整 `storydex-agentd`；不得在同一 beta 请求中按失败情况静默回落到 Python。Stable 仍固定使用原链路。
- 复用现有 `/api/v1` 和 SSE，首轮不改为 Tauri IPC 或自定义事件协议。
- 验证端口避让、启动超时、日志轮换、进程树清理、崩溃提示和执行恢复。
- 使用大型合成 fixture 或脱敏项目副本做长会话、中文路径、断网、Provider 错误和强制退出测试。
- beta 失败必须明确报告 Rust 后端错误，不得静默启动 Python。

退出条件：

- 当前前端和桌面实际使用的后端 manifest 项达到 parity；无消费者的历史内部接口有明确处置记录。
- Python/Rust 黑盒差分无未解释差异。
- 完整前端、桌面和封装 E2E 在 Rust beta 包上通过。
- 同一候选内容至少两次连续完成独立打包态验证，没有 P0/P1 数据损坏、执行丢失或更新阻断问题；基于真实测试者的时间观察仍属于未来 Stable 激活门禁，不阻塞本次候选实现完成。

### M5：完整 Rust 后端候选收口

M5 在开发/beta 轨道收口完整 Rust 后端，不切换 Stable 默认执行链。后端候选必须可以独立承担当前产品能力，但正式激活、Stable 更新和真实用户灰度仍需用户另行授权。

工作项：

- 关闭公开 HTTP/SSE manifest、磁盘格式、Git 副作用、故事生成、Knowledge/WIKI、执行恢复和项目服务的全部已知差异。
- 生成不携带 Python 后端、不提供静默 fallback 的独立 Rust beta/候选包。
- 验证旧 Python Stable 与 Rust 候选双向读取同一 fixture，无需数据降级。
- 保留 Python Stable 的源码、签名安装包和更新元数据，以支持未来激活后的人工回滚。
- 更新 README、架构文档、依赖/许可证清单、诊断指南和删除清单。

退出条件：

- 当前产品消费者不存在需要回退 Python 才能完成的功能。
- Rust 候选包、契约、覆盖率、安全审计、故障注入和性能门禁全部通过。
- Python runtime 已从目标候选构建输入中删除，但 Stable 构建保持不变。

### M6：Tauri Preview 与桌面切换

最终集成前置条件：M5 的公开接口和 sidecar 生命周期已稳定。Tauri 骨架、前端适配和 CI 可以与 M3-M5 并行开发，但不得用未闭环的后端行为完成最终验收。

工作项：

- 使用 Tauri 2 加载现有 Vue/Vite 构建，不重做页面和 store。
- Tauri Core 只管理窗口、系统菜单、更新、权限和 `storydex-agentd` sidecar。
- 为当前 `window.storydexDesktop` 建立窄适配层，逐项替换目录选择、文件定位、预览窗、标题栏和更新能力。
- 配置最小 capabilities；前端不得获得任意 shell 或任意文件系统权限。
- Windows 设置最低 WebView2 版本并验证 bootstrapper；固定 WebView2 runtime 仅在实际兼容性数据证明必要时采用。
- 建立新的 Tauri 签名更新产物和独立 preview feed，验证安装、升级、失败恢复和密钥备份。
- 对 WebView2 的中文输入、拖放、字体、编辑器 selection、SVG 图谱、SSE 和多窗口做视觉及行为回归。

退出条件：

- 同一 Rust 后端在 Electron 和 Tauri 壳下通过相同应用 E2E。
- 同一内容的 Tauri preview 至少两次连续完成独立打包态候选验证；真实发布周期观察留给未来 Stable 激活决策。
- 更新、安装、签名和回滚均有打包态自动化证据。
- Electron 稳定版仍可读取 Tauri preview 写入的项目数据。

### M7：清理旧运行时

- 从目标 Rust/Tauri 候选构建、安装目录和运行进程中删除嵌入式 Python、requirements、FastAPI/Uvicorn 入口及其打包校验。
- 从目标 Tauri 候选构建中删除 Electron 主进程、preload、electron-builder 和旧 update helper 依赖。
- 删除只为双实现服务的临时适配层和迁移开关。
- 保留必要的历史契约 fixture，防止以后破坏旧项目兼容性。
- 更新覆盖率基线时分别记录旧口径、新口径和变化原因，禁止直接降低门槛。
- 当前 Stable 源码和发布配置在未获激活授权前不得删除；应与目标候选路径明确隔离，并形成未来正式删除清单。

## 8. 测试与门禁

### 8.1 测试层级

| 层级 | 目的 |
| --- | --- |
| Rust 单元测试 | 领域规则、状态机、路径和序列化 |
| Rust 集成测试 | 文件事务、Git、索引、Coomi 和 Provider adapter |
| 黑盒 API 契约 | 任意实现的 HTTP/SSE 外部行为 |
| Python/Rust 差分 | 同一输入、fixture、事件和磁盘副作用 |
| 前端测试 | 保证 UI 不依赖后端内部实现 |
| 桌面打包 E2E | 启动、更新、端口、流式、恢复和退出清理 |
| 故障注入 | 断连、取消、进程退出、磁盘错误、Git 错误和无效模型响应 |

### 8.2 差分原则

- 读接口可以并行调用两个实现并比较结果。
- 写接口必须使用两份相同的临时项目克隆，分别执行后比较结果。
- 只允许对 traceId、时间戳、随机端口和临时根路径做显式规范化。
- JSON 数组顺序、SSE 事件顺序、Git changed paths 和写入文件集合默认视为有语义。
- 差异必须被解释并形成 ADR 或修复，不得扩大 ignore 列表绕过。

### 8.3 覆盖率

- Python 覆盖率基线继续由现有 release gate 维护，不因新增 Rust 代码降低。
- Rust 覆盖率在 M2 建立独立基线，在 M3 每个切片完成后 ratchet 上调。
- API、路径安全、文件事务、执行协调、WIKI 写入和 Agent 状态机属于 critical 模块，不能仅依靠总覆盖率。

### 8.4 Agent 真实主链路

- 真实模型验证使用完整 Agent 场景和真实工具/文件流程，不用一 token、空请求或短 health prompt 代替主链路。
- 至少覆盖只读、跨文件读取、章节中段读取、受限角色字段编辑，以及可用时的 follow-up、approval、cancel、resume 和错误场景。
- provider 不可用时，使用脱敏的 replay/fixture 继续做本地差分；报告必须保留真实 HTTP 状态，不能换模型或静默 fallback 后宣称 live 通过。
- live 结果用于验证 provider 适配和端到端行为；Rust 本地逻辑的日常门禁不得依赖外部 provider 始终在线。

## 9. 性能验收

M0 先固定机器、数据集、预热方式、样本数和统计口径，再提交具体数值目标。目标提交后，除非记录测量口径变化，不得在切换前放宽。

指标用于识别真正的本地瓶颈，不为测量本身增加额外模型调用、重复上下文或不必要的串行层；只有可重复的收益才进入切换门禁。

至少跟踪：

- 进程启动到 `/sys/health` 可用的 p50/p95。
- 空闲 60 秒后的完整进程树 RSS。
- 请求接受到首个 SSE 事件的 p50/p95。
- heartbeat 偏差和 cancel/stop 确认延迟。
- 小型与大型项目的文件树、全文搜索和诊断耗时。
- WIKI 冷构建、无变更同步和增量同步耗时。
- 故事上下文装配和本地字数/质量门禁耗时。
- 安装包、解包目录和更新下载体积。

切换门禁：

- 用户可感知的首个 SSE、取消响应和普通文件操作不得超出基线允许的测量误差。
- 本地 CPU/文件密集路径必须出现可重复的实质改善，否则不得仅凭语言变化宣称性能完成。
- LLM 总响应时间单独记录，但不作为 Rust 本地性能结论的主要依据。

### 9.1 2026-08-18 M0 Agent Refactor 性能窗口

统一入口：

```powershell
python apps/backend/scripts/run_agent_refactor_performance.py `
  --warmups 3 `
  --samples 20 `
  --idle-seconds 60 `
  --output output/agent-runtime-contract/m0-performance-current/performance-report.json
```

口径固定为当前 Windows 机器、debug Rust 二进制、同一只读/取消 replay、每个样本全新进程、workspace、Coomi home 和 session；3 个预热样本不进入统计，每个实现/场景保留 20 个正式样本。`totalMs` 只作确定性本地 replay 对照，真实 Provider 网络时间不进入 Rust 收益判断。

| 指标 | Python Stable median / p95 | Rust Refactor median / p95 |
| --- | ---: | ---: |
| 启动至 health（只读样本） | `4029.092 / 4372.584ms` | `745.265 / 783.702ms` |
| 首个 SSE（只读） | `742.054 / 816.627ms` | `687.411 / 710.613ms` |
| TurnContract（只读） | `3507.095 / 3763.722ms` | `756.070 / 779.379ms` |
| ToolStart（只读） | `3595.997 / 3853.454ms` | `806.414 / 1018.609ms` |
| 只读终态 | `6619.417 / 7016.476ms` | `822.676 / 1035.383ms` |
| stop HTTP accepted | `44.203 / 54.135ms` | `16.922 / 26.916ms` |
| stop 至 AgentCancelled | `2893.959 / 3017.809ms` | `29.410 / 40.098ms` |
| 60 秒空闲进程树 RSS | `95.65MiB` | `23.17MiB` |

Storydex Stable live 决策报告 `output/rust-migration-decision-live/eb2805f48eed-20260818T014444-m0-performance-gate/decision-report.json` 选择 `end_to_end_relative_gate`：用户可见 p95 允许因子 `1.10`；本地端到端里程碑要求 Rust median 不高于 Python 的 `0.80`、p95 不高于 Python；60 秒 RSS 不高于 Python 的 `0.80`。报告中 24 项 gate 全部通过。

最终窗口中 Rust debug 的 `componentInit/sessionInit` p95 比值分别为：只读 `17.9373x / 21.6833x`，取消 `2.1476x / 2.3881x`。只读两项已写入 `diagnosticInvestigations`，必须在 release/Beta 前以 release 构建继续调查；它们不改变当前端到端门槛 24/24 通过，也不能被端到端结果掩盖。该窗口只关闭 M0 的可重复本地统计和门槛决策，不构成 release-ready、Stable 接管或 Tauri 依据。

## 10. 主要风险与处置

| 风险 | 处置 |
| --- | --- |
| 把当前不稳定行为误当兼容契约 | M0 先复现和定性；明确区分 bug、实验功能和正式行为 |
| 小版本修复导致双实现漂移 | 生产实现先修复并新增黑盒 fixture，Rust parity 切片同步修改 |
| Python 字典被直接翻译成动态 JSON | API 边界允许动态值，领域层使用 Rust enum/struct 和验证构造器 |
| Windows 路径或 Git 边界回归 | 使用真实临时 worktree、junction、父仓库和大小写矩阵测试 |
| SSE 看似有最终结果但时序错误 | 用状态机断言首包、heartbeat、工具事件、取消和唯一终止事件 |
| 文件部分写入或回滚失败 | 事务清单、临时文件、原子替换、故障注入和目录级结果比较 |
| Agent 崩溃带走桌面 UI | 保持 `storydex-agentd` 独立进程，桌面壳只管理生命周期 |
| 双后端静默 fallback 隐藏缺陷 | beta 和 stable 都使用显式运行时；启动失败直接可见 |
| Tauri WebView 差异造成 UI 回归 | 后端先稳定，Tauri 单独 preview；建立 WebView2 打包 E2E |
| 更新迁移造成用户无法升级 | 独立 feed、强制签名、升级/失败恢复测试和旧签名安装包保留 |
| 完整测试过慢降低反馈质量 | M0 分出快速契约层、切片层和发布层，同时保留完整 release gate |

## 11. 停止与回滚条件

出现以下任一情况时停止扩大迁移范围，先修复当前里程碑：

- 用户项目出现不可逆数据修改或旧版本无法读取。
- Agent 执行出现重复写入、丢失终止状态或无法取消。
- WIKI/Story Knowledge 出现第二事实源、无证据关系或稳定 ID 漂移。
- 路径逃逸、父仓库误操作、凭证泄漏或更新签名失效。
- Python/Rust 差分只能通过宽松忽略关键字段才能通过。
- beta 需要频繁依赖 Python fallback 才能完成核心流程。

正式切换后的回滚方式是安装上一个签名稳定版本并继续读取同一项目数据，而不是在新版本内部静默切换后端。任何单向数据迁移都会破坏该回滚路径，因此必须推迟到旧运行时退出支持窗口之后。

## 12. 完成定义

### 12.0 新对话工程完成定义

新对话只有在以下结果全部具备时，才可以声明“剩余重构工程已完成”：

- `storyGeneration`、Knowledge/WIKI、Git、其余公开后端 API 和桌面生命周期都由目标 Rust 后端承担，不依赖 Python fallback，并通过契约、磁盘副作用和故障注入验证。
- Electron Rust Beta 与 Tauri 2 候选均可从干净环境构建、启动、运行主要工作流、退出和打包；目标 Tauri 候选的安装/运行资产不包含 Python 或 Electron/Node 运行时。
- Vue 工作台保持现有主要功能和数据语义；项目 fixture 可在 Python Stable、Electron Rust Beta 和 Tauri 候选之间双向读取，不发生单向升级。
- 所有新增或迁移代码经过与风险相称的聚焦测试，相关 manifest、覆盖率、安全/依赖审计、打包证据和文档已更新；无法运行的外部验证必须明确记录，不能按意图推定成功。
- 最后一个交付 HEAD 已按 0.7 的顺序通过 `dev/windows` Development CI 和 `main` 完整 CI。
- Stable 的默认运行时、正式包和更新源仍未改变。工程完成后应把“Stable 激活与真实用户灰度”列为唯一独立发布决策，不把它伪装成尚未完成的代码迁移。

单个 Rust 切片、局部 parity、一次 Development CI 或一次 `main` 完整 CI 成功都不能单独满足本节。只要上列任一工程项仍未完成且没有真实外部阻塞，新对话就应继续实现、验证并进入下一轮提交和推送；不得把“本轮切片完成”回答成“剩余任务需要以后另开计划”。

### 12.1 Agent Rust 重构完成定义

只有同时满足以下条件，才可把本轮 Agent 重构标为完成：

- Agent 的 HTTP/SSE、权限、工具、文件、session 和错误行为通过 Python/Rust 差分；差异有明确解释。
- 读写 fixture、取消/审批/恢复、路径安全和 provider 错误场景均有可重复证据。
- Electron Stable 未被旁路实现静默替换；Agent Beta 的启动、崩溃、恢复和回滚可见且可控。
- 至少一轮完整真实主链路通过；provider 不可用时明确记录为外部阻塞，不以 replay 冒充 live 成功。
- 性能结论来自本地基线和真实收益，不以增加模型调用、上下文或隐藏重试换取表面指标。

Rust 后端完成需要同时满足：

- 公开 HTTP/SSE 契约、项目磁盘格式和 Git 副作用通过差分验证。
- Agent、WIKI、故事生成、回滚和执行恢复无功能缺口。
- Rust beta 与正式打包 E2E 通过。
- 性能数据达到 M0 固定的目标。
- 正式版本不依赖 Python fallback。

Tauri 迁移完成需要同时满足：

- Vue 工作台功能和视觉回归通过。
- sidecar 生命周期、更新、签名、安装和恢复通过打包态验证。
- Tauri 和上一个 Electron 稳定版可以互相读取项目数据。
- Electron 和 Python 旧运行时被从目标 Tauri 候选的构建、依赖和运行资产中完整移除；Stable 参考实现及其文档在正式激活前保持隔离。

## 13. 新对话连续执行批次

新对话不要重新做规划盘点或扩充已关闭的控制面场景，直接从最新成功 HEAD 接续以下交付批次。序号表示依赖和集成顺序，不表示每项完成后暂停等待授权：

1. **已完成：** bridge `complete` 的显式 replay 透传和聚焦测试，以及 Rust 单片段 `create_new` 短章节纵向切片：一次 Provider completion、机械质量/Unicode 字数门禁、程序化安全路径、原子写入、`StoryGenerationValidation`、`StoryCallAccounting`、`TextChunk` 和唯一终态。
2. **已完成：** 使用同一临时项目和 Provider replay 冻结 Python Stable 真实契约，Rust 已差分到章节文件 SHA、Git status、SSE、调用次数和 v2 short candidate 长度档校准一致；Python-only WIKI/Git 派生行为保持显式，未被忽略。
3. **当前执行：** Knowledge/WIKI 与 Git 第一子批已建立 canonical checksum、原子 bundle、安全路径、本地 Git init/status/commit/restore primitives，并接入 4 个 Git、2 个 WIKI fixture 候选路由；130 个 Python 路由和 78 个前端消费签名的接口清单也已生成。继续关闭 revision、last-good、领域事件、ChangeSet、回滚以及 WIKI 冷构建/增量同步和全部实际消费者路由，现有局部路由不得表述为完整 parity。
4. **当前并行：** 单片段 `create_new` medium/long 已接入分档门禁、动态 Provider 输出上限、SSE/accounting 和 v2 分档校准，并覆盖 Provider 失败、预取消和原子写入保护；紧接着补齐 Python/Rust replay 磁盘 SHA/事件/校准差分、`modify_existing`、多片段及写入故障 fixture，再迁移配置、项目、预设、搜索、索引、诊断、文件和其余仍有消费者的公开后端 API。
5. 在完整 Rust 后端契约闭环后完成独立 Electron Rust Beta 的启动、认证、端口、进程树、日志、崩溃恢复、更新和回滚验证；Beta 不得静默回退 Python，Stable 配置保持不变。
6. **骨架已建立、继续执行：** 隔离 Tauri 2 壳现已可 `cargo check/build/clippy`，使用独立应用标识、Vue `dist`、最小 capability 和 Rust 候选资产门禁；下一步实现 `window.storydexDesktop` 窄适配层及动态端口/token、sidecar 生命周期，再用同一完整 Rust 后端完成 capabilities、安装、签名更新、候选打包和双向项目兼容验证。当前骨架不得提前接入 Stable 配置或正式更新源，也不得冒充打包态验收完成。
7. 完成旧运行时退出准备和 12.0 最终验证，确认目标候选资产不含 Python/FastAPI/Uvicorn 与 Electron/Node 运行依赖，同时保留 Stable 参考实现和人工回滚资产。
8. 每个可集成交付块执行相关聚焦测试、中文提交并按 0.7 推送；一轮远端门禁成功后继续下一块，可以在同一新对话中多次提交和推送。不要积累一个无法定位失败原因的超大最终提交，也不要把完整目标重新拆成需要用户逐项批准的小计划。

## 14. 参考边界

- 当前后端入口：`apps/backend/main.py`
- 当前 Agent/SSE 路由：`apps/backend/api/routes_agent.py`
- 当前 Agent 编排与契约：`apps/backend/services/agent_intent_routing.py`、`apps/backend/services/agent_capability_policy.py`、`apps/backend/services/storydex_orchestration_service.py`、`apps/backend/services/storydex_context_assembler_service.py`
- 当前 Rust bridge：`apps/backend/services/coomi_bridge_client.py`
- 当前 Rust Agent runtime：`apps/desktop/agent-runtime/engine`、`services`、`tools`、`security`、`storydex-bridge`
- 当前桌面启动与更新：`apps/desktop/electron/main.cjs`
- 当前发布门禁：`.github/workflows/quality-gate.yml`
- 当前本地完整检查：`scripts/run_full_test_suite.ps1`
- 当前覆盖率基线：`coverage-baseline.json`
- Tauri Process Model：https://v2.tauri.app/concept/process-model/
- Tauri Sidecar：https://v2.tauri.app/develop/sidecar/
- Tauri Updater：https://v2.tauri.app/plugin/updater/
