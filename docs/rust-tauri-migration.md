# Storydex Rust 后端与 Tauri 桌面重构计划

- 状态：实施基线草案（Agent 重构执行版）
- 建立日期：2026-08-05
- 最近修订：2026-08-16
- 适用范围：Storydex 2.x 稳定维护、Agent Rust 重构、后续 Rust 后端与 Tauri 桌面迁移

## 0. 本轮 Agent 重构边界

本轮先处理 Agent 的实现语言和运行时边界，目标是“行为不变、实现可替换”，不是一次性重写整个后端。现有 Stable 生产链路继续使用 `Electron + Python/FastAPI + Rust Coomi bridge`；Rust Agent 先以旁路/Beta 形态运行，未达到 parity 前不得接管 Stable。

本轮范围：

- Agent 的意图、权限、TurnContract、上下文装配、工具循环、Provider 适配、SSE 生命周期、取消/恢复和错误透传。
- 复用并整理现有 `apps/desktop/agent-runtime` 的 engine、services、tools、security 和 bridge 能力，不重复实现已有稳定基础设施。
- 以现有 `/api/v1`、SSE、`.storydex` 文件格式、Git 边界和前端可见行为为兼容边界。

本轮不做：

- 不迁移 WIKI、Story Knowledge、普通项目服务或前端设计；这些仍由 Python/现有桌面壳负责。
- 不同时切换 Tauri；Tauri 仍以后端稳定观察周期为前置条件。
- 不把 123 个全后端路由或完整 Python 删除作为 Agent 首个切片的前置条件。

执行方向只规定边界，不规定每个内部模块的固定拆分。实现过程中以现有代码和测量结果决定 crate、进程和适配器的最小划分；不得为了“重构完整”增加无消费者的中间层、额外模型调用或重复上下文构建。外部技术资料在确有疑问时可用 `smartsearch` 查询，不是本计划的必需依赖。

### 0.1 2026-08-16 当前执行状态

本轮实际进度按里程碑记录如下：

- **M0：部分完成。** 已用 Storydex 当前生效的 `OPENCODE/deepseek-v4-flash` 配置跑通真实严格只读主链路；最近一次脱敏报告为 `status=passed`、HTTP `200`、2 个模型回合、1 次 `read_file`、0 重试、终态 `AgentCompleted`，RouteHints 为 `read + no_write`。意图否定语义和基线报告持久化缺陷已修复；取消、审批、恢复以及不稳定行为台账仍未收齐。
- **M1：部分完成。** 已建立 Agent runtime manifest，以及可指向任意 base URL 的 health、Coomi 状态黑盒契约和归一化比较入口。Rust sidecar 的 health/Coomi status 契约已用 Storydex 实际 `OPENCODE/deepseek-v4-flash` 配置通过；读写、SSE、取消/恢复的完整契约仍待补齐。
- **M2：骨架已实现并通过本地与远端验证。** `apps/desktop/agent-runtime/storydex-agentd` 是未接入 Stable 的独立 loopback 服务，具备动态端口、每次启动 token、统一 envelope、trace、panic 边界、任务注册表、`CancellationToken` 和受控关闭；格式、Clippy、154 项 workspace 测试、release 构建、真实启动/鉴权/退出及契约检查均已通过。`dev/windows` Development CI run `31945550643` 和 `main` CI run `31948178752` 均为 `success`。独立依赖审计仍未完成，因此不宣告 M2 的所有治理项已关闭。
- **M3：首个无副作用切片已落地，主链路迁移尚未开始。** Rust 已实现只读 `/api/v1/agent/coomi/status`，直接读取 Storydex Coomi Home 并返回脱敏的激活 Provider/模型与能力；尚未达到完整字段 parity，也尚未把 `/api/v1/agent/chat/stream` 或其他 Agent HTTP/SSE 接入 `storydex-agentd`。Electron Beta、Stable 切换和 Tauri 迁移均未开始。

这里的“真实主链路”特指 Storydex 自身 `providers.json` 中当前激活的 Provider；OpenCode 源配置中的 `ds/deepseek-v4-flash` 曾返回 HTTP `522`，它不是 Storydex 当前主链路的配置，不能作为迁移阻断依据。

### 0.2 2026-08-16 推送后同步与下一阶段入口

- 本次 `git fetch origin --prune` 后，`main`、`origin/main`、`dev/windows` 和 `origin/dev/windows` 均指向 `d7909d6c6d152709bee7abe561b779f32dafb69b`，当时没有待合并的远端提交。
- `dev/windows` 上游已有的 `7e161e7 feat(windows): report daily active usage` 已保留并随分支快进进入 `main`；该提交不是 Agent parity 证据，后续差分仍只以 Agent 相关文件和契约为准。
- 最新真实主链路脱敏报告：`output/push-validation/opencode-deepseekv4flash/baseline-report.json`。该运行经过正常 Storydex Backend HTTP/SSE + Rust bridge，未由 `storydex-agentd` 接管。
- Stable 仍固定使用 `Electron + Python/FastAPI + Rust Coomi bridge`；`storydex-agentd` 只能在 Refactor/Beta 轨道使用，不得静默 fallback 或读取真实用户项目。

下一阶段按以下顺序推进：

1. 以 `apps/backend/api/routes_agent.py`、`services/agent_lifecycle_trace.py` 和现有基线报告为来源，冻结 `POST /api/v1/agent/chat/stream` 的请求、统一 envelope、首包、heartbeat、工具事件、阶段顺序和唯一终止事件契约。
2. 建立 provider replay 与确定性的只读 fixture，先让 Python Stable 产出规范化事件序列、trace/session、工具参数和错误边界。
3. 将现有 Rust `engine`/`storydex-bridge` Agent loop 接入 `storydex-agentd` 的 Refactor 路由，只做旁路差分，不修改 Stable 启动入口。
4. 对同一输入执行 Python/Rust 读请求差分；写入、取消、审批、恢复在临时 fixture 上逐项比较副作用和状态迁移。
5. 只有无解释差异和故障注入结果稳定后，才进入 Electron Beta 评估；Tauri 和 Stable 切换继续冻结。

## 1. 结论

Storydex 采用分阶段、可回滚的渐进式重构：

1. 先在 Electron 桌面壳不变的前提下，将 Agent 执行链逐步迁移到独立 Rust 服务（可复用现有 runtime；服务名沿用 `storydex-agentd` 仅作为目标形态）。
2. Agent Rust 版本经过差分、Beta 和稳定观察后，再评估其余后端切片；完整 Rust 后端稳定后，才迁移 Electron 到 Tauri 2。

在重构门禁完成前，后续 2.x 小版本继续使用当前 `Electron + Python/FastAPI + Rust Coomi bridge` 生产链路。Rust 和 Tauri 的未完成实现不得进入正式包、不得读取或写入真实用户项目，也不得通过静默 fallback 改变线上行为。

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
- 本轮不改变 Agent/WIKI 产品规则、提示词约束、权限含义或主要工作流；只迁移 Agent 的实现语言和运行时边界。
- 本轮不要求一次性移除 Python/FastAPI；旧实现作为 Stable 参考和可回滚版本保留到正式切换完成。
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
| Rust Beta | Electron + Rust `storydex-agentd` | 明确加入的测试者 | 仅备份后的测试项目 | 独立 beta 源 |
| Tauri Preview | Tauri + Rust `storydex-agentd` | 内部和预览测试者 | 仅备份后的测试项目 | 独立 preview 源 |

Stable 不得因其他轨道未完成而改变启动命令、依赖、安装资产或自动更新元数据。

### 4.2 分支与合并规则

- `main` 始终保持可发布，稳定功能和紧急修复优先。
- Windows 与 Android 分别在 `dev/windows`、`dev/android` 集成，两个分支只从 `main` 同步且不得互相合并；具体功能仍使用短生命周期分支。
- 未接入生产的 Rust 代码可以合入独立目录，但不得被当前启动脚本、Electron 主进程或打包脚本引用。
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

以下是长期完整目标；本轮只实现其中的 Agent 边界，不要求同时迁移图中的全部领域服务。

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

本轮的过渡边界更窄：`storydex-agentd` 首先只承载 Agent 的执行、权限、上下文、工具和 SSE 协调；WIKI、Story Knowledge、项目服务等仍留在 Python Stable。现有 `apps/desktop/agent-runtime` 已包含 engine、services、tools、security 和 bridge，优先通过复用或提取稳定接口接入，只有在所有权或测试隔离确有收益时才新建 crate。Agent 与 Python 领域服务之间使用明确的 HTTP/JSONL 或库接口，不以隐式跨进程状态作为兼容手段。

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

迁移顺序：

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

### 本轮 Agent 执行路径

上表是完整 Rust 后端的长期风险顺序；本轮 Agent 重构不需要等待 WIKI 或普通项目服务迁移完成。执行时保持以下方向即可，内部拆分可随证据调整：

1. 先把现有 Python Agent 的意图、权限、TurnContract、上下文、工具循环和生命周期行为固化为可运行 fixture。
2. 在现有 Rust runtime 上补齐缺失的强类型领域模型和适配器，优先复用 provider、tools、security、session 和 bridge，不重复造轮子。
3. 让 Rust Agent 在 Refactor/Beta 轨道与 Python Stable 做同输入差分；读请求比较响应和事件，写请求比较临时项目副作用。
4. 只有 parity 和故障注入结果稳定后，才扩大到 Electron Beta；Tauri 和非 Agent 后端继续按后续里程碑处理。

这不是必须逐项打勾的流水线。若现有实现已覆盖某一层，直接保留并补契约；若某一层尚无真实消费者，不为满足目录结构而新增实现。

### M4：Electron + Rust Beta

本轮 M4 只验证 Agent Rust 服务；未迁移的 WIKI、Story Knowledge 和普通项目接口继续由 Python 提供。

工作项：

- 让 Electron 在独立 beta 构建中同时管理现有 Python 后端和 `storydex-agentd`，只将 Agent 表面切到 Rust；Stable 仍固定使用 Python Agent 链路。
- 复用现有 `/api/v1` 和 SSE，首轮不改为 Tauri IPC 或自定义事件协议。
- 验证端口避让、启动超时、日志轮换、进程树清理、崩溃提示和执行恢复。
- 使用复制后的大型真实项目样本做长会话、中文路径、断网、Provider 错误和强制退出测试。
- beta 失败必须明确报告 Rust 后端错误，不得静默启动 Python。

退出条件：

- Agent manifest 项达到 parity；未迁移的全后端 manifest 不计入本轮 Beta 门禁。
- Python/Rust 黑盒差分无未解释差异。
- 完整前端、桌面和封装 E2E 在 Rust beta 包上通过。
- 至少两个连续发布候选周期没有 P0/P1 数据损坏、执行丢失或更新阻断问题。

### M5：Rust 后端正式切换

M5 的完整版本仍属于后续全后端迁移。本轮只有在 Agent 的 Beta 和稳定观察完成后，才允许讨论将 Electron 默认 Agent 执行链切换到 Rust；不得借此同时切换其他后端或 Tauri。

工作项：

- 在独立的主版本或迁移版本中，将 Electron 默认 Agent 执行链切换为 `storydex-agentd`；未迁移的后端接口继续由 Python 提供。
- 正式包不同时捆绑 Python 后端作为静默 fallback。
- 保留上一个 Python 稳定版本的签名安装包、源码分支和更新元数据，以支持人工回滚。
- 因为磁盘格式保持向后兼容，用户回滚旧版本时不需要数据降级。
- 更新 README、架构文档、依赖清单、许可证清单和故障诊断指南。

退出条件：

- Rust 后端至少经过一个稳定发布观察周期。
- 没有需要回退 Python 才能完成的正式功能。
- Python runtime 可以从下一阶段的构建输入中删除。

### M6：Tauri Preview 与桌面切换

前置条件：M5 已完成，不与 Rust 后端首次切换并行。

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
- Tauri preview 至少经过两个连续发布候选周期。
- 更新、安装、签名和回滚均有打包态自动化证据。
- Electron 稳定版仍可读取 Tauri preview 写入的项目数据。

### M7：清理旧运行时

- 删除嵌入式 Python、requirements、FastAPI 入口和 Python 打包校验。
- 删除 Electron 主进程、preload、electron-builder 和旧 update helper。
- 删除只为双实现服务的临时适配层和迁移开关。
- 保留必要的历史契约 fixture，防止以后破坏旧项目兼容性。
- 更新覆盖率基线时分别记录旧口径、新口径和变化原因，禁止直接降低门槛。

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
- Electron 和 Python 旧运行时被从正式构建、依赖和文档中完整移除。

## 13. 第一批可执行工作

第一批工作只增加 Agent 证据和旁路基础设施，不改变 Stable 生产运行时：

1. 记录 Agent 外部行为和已知不稳定点，形成最小可运行的读/写/取消/恢复 fixture。
2. 盘点并复用现有 Rust runtime，确定 Agent 服务与 Python 领域服务的最小边界。
3. 建立 Python/Rust 黑盒差分和 provider replay；随后实现 Rust Agent 的第一条无副作用切片。
4. 用完整主链路、故障注入和资源基线决定是否扩大切片；达到 parity 后再进入 Electron Beta。

不要求先完成全后端 123 路由 manifest，也不要求先改 Tauri；这些工作在 Agent 稳定后按需要推进。

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
