# Storydex Agent 运行链路、信息完整性与性能治理计划

分析日期：2026-08-07

分析范围：Storydex Agent HTTP/SSE 入口、意图控制层、TurnContract/上下文装配、Coomi Python service、Rust bridge、模型/工具循环、session 持久化、上下文压缩、文件读取、FTS/Wiki 检索和本地运行记录。

信息来源：仅基于当前本地代码、测试、本机 Storydex/Coomi 运行数据和临时项目复现，未进行外部检索。性能数字是本机基线，不是跨机器 SLA。

关联文档：[Wiki 与 Agent 读取/检索硬缺陷分析](../Wiki与Agent读取检索硬缺陷分析.md)

实施状态更新：2026-08-07

| 变更包 | 状态 | 说明 |
|---|---|---|
| P0-1 会话握手与恢复 | 已完成 | 严格恢复、首次落盘后绑定、session schema 校验、原子保存及失败关闭均已落地 |
| P0-2 有界读取完整性协议 | 已完成 | revision/span/总量/继续游标、长单行续读、UTF-8 安全截断及完整 prompt 约束均已落地 |
| P0-3 全文分块检索 | 已完成 | v3 全文 chunk 索引、revision/span、原子发布、状态区分及真实 Agent 验收均已落地 |
| P1 / P2 | 未实施 | 保留本文后续计划，供新对话继续处理 |

三个 P0 变更包均已完成。性能治理专项尚未完成：重复目录扫描、跨回合证据复用、bridge 生命周期和 Provider 增量协议仍属于 P1/P2。

## 1. 结论

当前 Agent **不是每轮完全失忆**：只要 runtime session 能被正确加载，用户消息、assistant 消息、工具消息、Plan、Loop 和累计 usage 会跨回合保存在 Coomi session 中。

修复前，系统存在三组需要优先处理的 P0 信息完整性问题；当前实现已按本文第 9 节完成对应治理：

1. **P0-1：会话握手和恢复不可靠。** 已绑定 session 加载失败时会静默创建空 session；首次 session 尚未落盘就发送 `session_bound`。这会造成无感知断档或 binding 指向不存在的历史文件。
2. **P0-2：有界读取没有完整性协议。** `read_file` 默认只给 500 行、最多 2,000 行且可能再被 48,000 字节截断，却不告诉模型总量、剩余范围和下一页位置；长中文单行还存在 UTF-8 非字符边界截断风险。意图控制层另只读取用户请求前 2,000 字符，可能漏掉尾部权限约束。
3. **P0-3：FTS 把长文件中部永久排除在索引外。** 当前 120,000 字符限制使用头尾拼接，不是全文分块。中部事实不是“排名低”，而是根本不存在于索引中。

性能下降主要来自四处：

- 每回合 TurnContract/Wiki/FTS 重复遍历目录并 `stat` 大量文件。
- 每回合启动并关闭 Rust bridge，重新加载 MCP、Hooks、Memory、Tools 和系统提示。
- 每个模型工具轮都重发系统提示、完整活动 history 和全部工具 schema。
- 缺少跨回合 Evidence Ledger；文件未变化时，模型仍可能重复读取相同范围。

建议严格按 `P0 -> P1 -> P2` 推进。P0 先消除无感知信息丢失；P1 再消除重复扫描、重复读取和无效 Provider 调用；P2 最后改造长生命周期 runtime 和增量模型协议。

## 2. 优先级定义

| 级别 | 判定标准 | 本计划目标 |
|---|---|---|
| P0 | 会静默丢历史、漏权限约束、或使项目中真实证据不可读取/不可召回 | 消除确定性硬损失；失败必须可观察、可恢复 |
| P1 | 信息理论上可取，但默认链路重复扫描、重复读取、错误降为空结果，或产生明显额外模型调用 | 降低首 Token 延迟、工具轮数和无效 Token；建立证据复用 |
| P2 | 需要调整进程生命周期、Provider 会话协议或统一架构才能继续降低固定成本 | 复用运行时资源；实现增量请求和稳定性能 SLA |

本文把 P0 收敛为三个可独立评审、按顺序实施的变更包。意图控制层的 2,000 字符截断并入 `P0-2`，因为它与 `read_file` 属于同一根因：调用方使用了有界文本，但没有显式完整性和拒绝协议。

## 3. 当前实际运行链路

```text
前端 POST /agent/chat + SSE
  -> 建立 Git before 快照
  -> 意图分类 LLM
       只接收用户请求前 2,000 字符
  -> build_turn_contract()
       章节状态/目标/权限/上下文装配
       多处重复目录扫描和 stat
  -> 复杂任务并行启动独立规划 LLM
       正文可能走 bounded/semantic 专用生成
       其他任务进入通用 Coomi Agent
  -> Python 启动 Rust storydex-bridge 子进程
       读取 binding 中的 runtime session id
  -> Rust 加载或创建 Session
       当前加载失败会静默创建新 Session
  -> Rust 立即发送 session_bound
       Python 随即持久化 Storydex -> runtime binding
  -> Rust 重建 Memory/MCP/Hooks/Tools/Provider/System Prompt
  -> 最多 100 轮模型/工具循环
       每轮发送 system prompt + 完整活动 history + 全部 tools
  -> 保存 runtime session
  -> Wiki/Memory 投影、Git after 快照、Trace/Execution Log 落盘
  -> Python 关闭 Rust bridge
```

关键入口：

- `apps/backend/api/routes_agent.py:6370`：SSE 主入口。
- `apps/backend/services/storydex_intent_service.py:991`：意图模型只接收 `prompt[:2000]`。
- `apps/backend/services/storydex_orchestration_service.py:71`：TurnContract 入口。
- `apps/backend/services/coomi_agent_service.py:455`：Coomi Agent 执行入口。
- `apps/backend/services/coomi_agent_service.py:553`：每回合启动 bridge。
- `apps/backend/services/coomi_agent_service.py:707`：回合结束关闭 bridge。
- `vendor/coomi-rs/storydex-bridge/src/main.rs:608`：session 加载失败 fallback。
- `vendor/coomi-rs/storydex-bridge/src/main.rs:619`：过早发送 `session_bound`。
- `vendor/coomi-rs/engine/src/agent.rs:281`：每个工具轮重建完整模型请求。

## 4. 已确认的证据基线

### 4.1 信息完整性复现

| 场景 | 当前结果 | 性质 |
|---|---|---|
| binding 存在、session 文件缺失 | Rust 创建新空 session 并继续 | 静默历史丢失 |
| session JSON 损坏或不兼容 | Rust 创建新空 session 并继续 | 静默历史丢失 |
| `session_bound` 后、Memory/MCP/Hooks/Provider 初始化失败 | Python 已写 binding，session 文件可能不存在 | 悬空 binding |
| 160,007 字符文件的中部唯一词 | FTS 命中数为 0 | 确定性证据丢失 |
| `read_file` 读取超过 500 行 | 默认只返回前 500 行，无 `hasMore` | 模型无法证明已读完整 |
| 工具输出超过 48,000 字节 | 直接按字节调用 `String::truncate` | 中文边界可能 panic |
| 超过 2,000 字符的用户请求，尾部含“不要修改” | 尾部不进入意图分类 LLM | 控制层和执行层认知不一致 |

本机实际项目中发现过两个 `HistoryExists=False` 的 binding；对应 Trace 都记录为 `storydex_coomi_bridge_error`。这证明悬空 binding 不是纯理论分支。

### 4.2 性能基线

300 章节临时项目，暖态单次 `build_turn_contract()`：

```text
3.5593 秒
26,547 次 Path.stat
7 次 list_chapter_states
```

300 章节项目，暖态单次 Wiki 查询：

```text
0.2269 秒
_collect_sources 调用 1 次
```

当前配置：

```text
CONTEXT_LRU_ENABLED=False
JIT_CONTEXT_LOADING_ENABLED=False
CONTEXT_TOKEN_BUDGET_REAL=False
```

一个本机真实长 session：

```text
5 条真实用户消息
49 条 assistant 消息
115 条 tool 消息
累计 input_tokens: 3,083,229
cached_input_tokens: 2,152,448
活动上下文估计: 140,777 tokens
compaction_count: 0
session 文件约 515 KB
```

同一 session 的重复工具特征：

```text
read_file: 61 次
唯一 read_file 调用签名: 29 个
全部工具重复签名: 46 个
read_file 输出总量: 约 105,519 字符
StorydexVersionStatus: 9 次，输出约 25,693 字符
```

这些调用中有些发生在文件写入之后，不能把全部重复签名判为浪费。真正缺失的是 `revision + source span + evidence cache`，系统目前无法区分“必须重读”和“文件未变、可复用”。

## 5. 问题总表

| ID | 级别 | 问题 | 直接后果 | 推荐变更包 |
|---|---|---|---|---|
| S-01 | P0 | session 加载错误被 `unwrap_or_else` 吞掉 | 历史无感知归零 | P0-1 |
| S-02 | P0 | 首次保存前发送 `session_bound` | binding 指向不存在文件 | P0-1 |
| S-03 | P0 | SessionStore 直接覆盖目标 JSON | 进程中断可能产生部分文件 | P0-1 |
| R-01 | P0 | `read_file` 无总量、范围、下一页和 revision | 模型不知道是否读完，无法可靠续读 | P0-2 |
| R-02 | P0 | 48,000 字节直接截断 Rust String | UTF-8 边界风险；返回内容完整性未知 | P0-2 |
| R-03 | P0 | 意图控制层只看 prompt 前 2,000 字符 | 尾部权限/目标约束不参与路由 | P0-2 |
| F-01 | P0 | FTS 长文件只索引头尾 120,000 字符 | 中部证据永久不可召回 | P0-3 |
| F-02 | P0 | 检索异常与零命中均可表现为空块 | 模型误把系统失败当“项目没有证据” | P0-3 |
| P-01 | P1 | TurnContract 重复章节遍历 | 模型调用前出现秒级固定延迟 | P1-1 |
| P-02 | P1 | Wiki/FTS 查询前重复全树扫描 | 无变化查询仍为 O(文件数) | P1-1 |
| P-03 | P1 | 缺少跨回合 Evidence Ledger | 文件未变仍重复读相同范围 | P1-2 |
| P-04 | P1 | 被动 FTS 查询词过窄 | 普通主题请求可能完全不检索 | P1-3 |
| P-05 | P1 | 复杂任务独立规划 LLM 不参与主 Agent 决策 | 额外 Provider 调用，质量收益不确定 | P1-4 |
| P-06 | P1 | Token/LRU/JIT 策略开关未启用 | 字符预算与真实模型预算脱节 | P1-5 |
| P-07 | P1 | 压缩只保留最多 20,000 token 用户消息，其余依赖摘要 | assistant/tool 的精确证据可能丢失 | P1-6 |
| P-08 | P1 | 早期工具结果会被统一截断文本替换 | 摘要模型也可能看不到原始证据 | P1-6 |
| A-01 | P2 | 每回合重启 bridge 并重载运行时组件 | 冷启动、连接和缓存成本重复 | P2-1 |
| A-02 | P2 | 每个工具轮重发完整活动 history/tool schema | Token、序列化和 Provider prefill 成本累积 | P2-2 |
| A-03 | P2 | Wiki、FTS、上下文和工具各自维护读取规则 | 同一问题的范围、错误和排序语义不一致 | P2-3 |

## 6. P0-1：会话握手与恢复

### 6.1 根因

Storydex 会话和 Coomi runtime session 是两个独立持久化对象：

```text
Storydex session id
  -> workspace 内 binding JSON
       -> runtimeSessionId
            -> runtime home/sessions/<uuid>.json
```

当前 Rust 逻辑在收到 `runtime_session_id` 后执行：

```rust
store.load(id).unwrap_or_else(|_| Session::new(...))
```

这把至少三种完全不同的情况合并为“创建新会话”：

- 文件不存在。
- JSON 损坏或写到一半。
- schema/字段不兼容，反序列化失败。

随后 Rust 在首次 `store.save()` 前就发送 `session_bound`。Python 收到事件立即写 binding；而 Memory、MCP、Hooks、Provider 都在这个事件之后初始化。任一初始化失败，都可能留下一个有效 binding JSON，但其目标 session 文件从未存在。

此外，当前 `SessionStore::save()` 使用 `fs::write` 直接覆盖目标文件，不具备“旧版本或新版本二选一”的原子发布保证。并发未提交补丁已经让 `run_turn` 失败时也尝试保存中断 session，这是有价值的局部修复，但仍未覆盖：

- `session_bound` 后、`run_turn` 前的初始化失败。
- 已绑定 session 的缺失/损坏/不兼容。
- session JSON 被直接覆盖时进程终止。

### 6.2 目标行为

新会话：

```text
创建 Session
  -> 原子保存最小合法 Session 成功
  -> 发送 session_bound
  -> Python 原子写 binding
  -> 初始化 runtime 组件
  -> run_turn
  -> 每个可靠检查点原子保存
```

已有会话：

```text
严格 load(runtimeSessionId)
  -> 成功：验证 id/cwd/schema 后继续
  -> 不存在：返回 session_restore_failed(missing)
  -> 损坏：返回 session_restore_failed(corrupt)
  -> 不兼容：返回 session_restore_failed(incompatible)
```

禁止自动新建并冒充旧会话恢复成功。用户主动“新建会话”或“清除会话”时才允许创建新的 runtime session。

### 6.3 最小完整改动

1. 将 `store.load(id).unwrap_or_else(...)` 改为严格分支；保留底层错误原因和 session path。
2. 新 session 创建后立即执行一次原子 `save`，成功后才发 `session_bound`。
3. 将 `SessionStore::save()` 改为同目录临时文件写入、flush、原子替换；替换失败时保留旧 session。
4. Python 只在收到“已持久化”的 `session_bound` 后更新 binding；事件至少携带 `runtimeSessionId`、`sessionPath` 和 session schema/version。
5. 对已有 binding 的 restore 失败发明确 `AgentError`/Trace 诊断，不删除旧 binding、不自动覆盖旧 history。
6. 保留当前“run_turn 失败后保存中断上下文”的补丁，并让保存失败和运行失败两个错误都可见。

主要文件：

- `vendor/coomi-rs/engine/src/session.rs`
- `vendor/coomi-rs/storydex-bridge/src/main.rs`
- `apps/backend/services/coomi_agent_service.py`
- `apps/backend/tests/test_coomi_agent_service_comprehensive.py`
- Rust bridge/session 单元测试

### 6.4 改动规模和兼容性

预计规模：**小到中等，约 3 个生产文件和聚焦测试**。不需要改 Agent 推理算法、前端会话 ID 或现有正常 session JSON 的主要字段。

对正常运行的影响：

- 正常的新会话和可读取旧会话行为不变，只多一次很小的首次落盘。
- 正常回合结束保存改为原子替换，可靠性提高；磁盘写入次数基本不变。
- 只有原本被静默掩盖的异常会改变表现：现在明确报“历史恢复失败”，不再假装是新对话。这是预期的 fail-closed 行为。
- Windows 上需要验证同目录临时文件替换和杀毒软件短暂占用场景；不得用静默重试后新建 session 掩盖替换失败。

### 6.5 回归测试与完成定义

- 新 session 首次保存失败时，不发送 `session_bound`，Python 不生成 binding。
- `session_bound` 后立即终止 bridge，binding 指向的 session 文件仍存在且可反序列化。
- 缺失、损坏、不兼容三种 restore 分别返回可观察错误，均不创建新 UUID。
- 已有正常 session 恢复后，历史消息数量、tool call id、provider reasoning item、Plan 和 Loop 不变。
- `run_turn` 失败时保存已发生的 user/assistant/tool 消息，同时原始 Provider 错误仍向上返回。
- 模拟保存中断后，目标路径要么是旧完整 JSON，要么是新完整 JSON，不能是半个 JSON。
- Storydex 会话删除、清空、回滚和 workspace 隔离测试继续通过。

P0-1 完成定义：任何 binding 都只能指向已成功落盘且可加载的 session；任何恢复失败都不能被解释为“空历史的新 session”。

## 7. P0-2：有界读取完整性协议

### 7.1 根因

当前 Rust `read_file`：

- 先 `read_to_string` 读取整个文件。
- 默认从第 1 行取 500 行，最多 2,000 行。
- 把带行号文本拼成一个字符串。
- 超过 48,000 字节后统一 `String::truncate(48000)`。

返回值没有 `totalLines`、`hasMore`、`nextOffset`、`revision`、实际覆盖范围或输出二次截断状态。模型只看到一段看似正常的文本，无法区分：

- 文件刚好只有这些行。
- 后面还有大量未读行。
- 2,000 行已取完，但 48,000 字节又截掉了后半段。
- 文件在两次分页之间已经被修改。

如果文件只有一条超过 48,000 字节的中文长行，行号分页也无法访问同一行后半段。并且 Rust `String::truncate` 要求目标是 UTF-8 字符边界，固定字节位置可能 panic。

控制层存在同类问题：意图模型固定接收 `prompt[:2000]`，但主 Agent 接收完整 prompt。于是权限、目标和执行层可能依据不同文本作决定。

### 7.2 目标协议

建议 `read_file` 保留现有 `path/offset/limit` 参数兼容性，但返回结构化 envelope：

```json
{
  "path": "chapters/chapter-001/content.md",
  "revision": "sha256-or-catalog-revision",
  "span": {
    "startLine": 1,
    "endLine": 420,
    "startByte": 0,
    "endByte": 47210
  },
  "totalLines": 1820,
  "totalBytes": 208311,
  "hasMore": true,
  "nextOffset": 421,
  "nextByteOffset": 47210,
  "truncated": true,
  "truncationReason": "max_output_bytes",
  "content": "..."
}
```

具体约束：

- `hasMore=false` 才能表示到达 revision 对应文件结尾。
- `nextOffset/nextByteOffset` 必须指向返回内容之后的第一个未读位置。
- 长单行必须能用 byte cursor 继续读取；cursor 必须落在 UTF-8 字符边界。
- 下一页请求若 revision 已变化，返回 `revision_conflict`，不能把两个版本的片段拼成一个“完整文件”。
- `truncated` 必须区分行数上限、字节上限和调用方预算上限。
- UI/Trace 可以预览截断，但模型真实收到的内容和审计记录都要保存准确 span。

意图控制层采用相同原则：

- 权限、否定词、目标路径、写入范围等确定性规则必须扫描完整用户请求。
- 意图 LLM 与主 Agent应使用同一个 prompt revision/hash。
- 如果 API 需要最大请求限制，应在入口明确拒绝超限并返回 `request_too_large`，不能静默切掉尾部。
- 若模型上下文确实容纳不了完整请求，应先做可观察的结构化分片/约束提取，并把“输入未完整分析”作为阻断错误，不能继续授权写操作。

### 7.3 最小完整改动

1. 修复通用输出截断函数：只在合法 UTF-8 字符边界截断，并返回截断元数据。
2. 为 `read_file` 增加结构化返回、revision、总量、实际 span 和继续游标。
3. 保留旧 `offset/limit` 入参；若已有 prompt/测试依赖带行号文本，把带行号文本放进 `content`，不要在首个 PR 同时改变行号语义。
4. 增加同一长行继续读取能力；不得仅增加 `hasMore` 却让长单行后半段仍不可达。
5. 意图分类移除无声明的 2,000 字符切片；在请求入口增加显式最大值与错误类型。
6. 对写权限判定增加完整 prompt 的确定性校验，使 LLM 分类错误不能越过用户尾部的“不要修改”。

主要文件：

- `vendor/coomi-rs/tools/src/lib.rs`
- `apps/backend/services/storydex_intent_service.py`
- `apps/backend/api/routes_agent.py`
- 工具协议、意图分类和 SSE 错误测试

### 7.4 改动规模和兼容性

预计规模：**中等**。文件读取实现本身局部，但工具返回从“裸文本”升级为“带元数据的文本/JSON envelope”会改变模型看到的格式，需要同步系统提示和测试。

对正常运行的影响：

- 短文件仍一次读完，内容不应变化；仅多出少量元数据 Token。
- 长文件会更可靠地触发继续读取，工具轮数可能暂时增加，但这是把此前隐藏的未读内容显式化，不是性能回退。
- 意图模型接收完整请求后，超长请求输入 Token 会增加。应通过入口上限和一次确定性约束扫描控制，而不是恢复静默截断。
- 结构化 envelope 可能影响模型已形成的工具使用习惯。建议保留现有行号文本作为 `content`，并在灰度期记录分页完成率。
- 不能直接把 48,000 改成更大数字；这只延后问题，不能解决完整性和 UTF-8 风险。

### 7.5 回归测试与完成定义

- 500、2,000、2,001 行文件分别验证 `span/totalLines/hasMore/nextOffset`。
- 超过 48,000 字节的 ASCII、中文、emoji 内容均不 panic、不产生无效 UTF-8。
- 单行 100,000 字节文件可通过连续 cursor 无重叠、无缺口地读完。
- 读取第一页后修改文件，读取第二页返回 revision 冲突。
- 多页拼接结果与同一 revision 的原文件完全一致。
- 用户请求前 2,000 字符要求写入、尾部要求“不要修改”时，最终 `canWrite=false`。
- 尾部目标路径、章节号和输出格式约束进入 TurnContract。
- 明确超限请求返回 4xx/结构化错误，不进入 Agent 执行。

P0-2 完成定义：任何有界输入或输出都必须告诉调用方“读取了哪个版本的哪一段、是否还有内容、如何继续”；权限控制层不得比执行层少看一段用户请求。

## 8. P0-3：全文分块检索和可观察错误

实施状态：**已完成**。本节 8.1 记录修复前根因，8.2 至 8.5 是已经落地并通过真实 Agent 验收的目标和完成标准。

### 8.1 根因

`RetrievalService` 当前使用：

```python
FTS5_INDEX_CHAR_LIMIT = 120_000
read_text_limited(path, FTS5_INDEX_CHAR_LIMIT, preserve_tail=True)
```

`preserve_tail=True` 的结果是“文件头 + 截断标记 + 文件尾”，不是全文 chunk。任何位于被丢弃中部的实体、伏笔、约束或唯一词都不会写入 FTS。搜索时无法通过提高 `top_k`、换排序算法或让 Agent 多试几次找回它。

被动上下文还会在检索异常时 `except Exception: return "", []`。这使“索引不可用”和“项目没有命中”表现相同，Agent 可能基于系统故障断言事实不存在。

### 8.2 目标索引

建议建立新版本 chunk FTS 数据库，不原地修改 v2：

```text
documents
  path, revision, size, mtime_ns, indexed_at, chunk_count

chunks
  path, chunk_id, start_char, end_char,
  start_line, end_line, content_or_tokens, revision
```

分块规则：

- 优先按段落/自然边界分块，目标约 2,000 到 4,000 Unicode 字符。
- 相邻块保留约 300 到 500 字符重叠，避免跨边界短语丢失。
- 超长单段/单行必须强制切分，不能退化为一个超大 chunk。
- JSON/YAML 等结构化文件应保留可定位 source span；后续可增加字段级路径，但不作为首个 PR 的前置条件。
- 每个结果返回准确 `path + revision + start/end line/char + snippet`。
- 删除、重命名和 revision 变化按文件事务替换其全部 chunks。

查询结果必须区分：

```text
ok + hits                 正常命中
ok + no_hits              索引完整但无命中
index_building            新索引尚未可用
index_stale               工作区 revision 新于索引
index_error               构建或查询失败
```

### 8.3 迁移策略

1. 使用新数据库名或 schema version，例如 `retrieval.fts5.v3.db`，保留 v2 便于回滚。
2. 在临时数据库完整构建 v3；通过完整性检查后原子发布，不让查询看到半个索引。
3. v3 未就绪时，可以继续提供 v2 的命中作为 `partial/legacy` 结果，但必须明确标记覆盖不完整，不能报告“全文无命中”。
4. 发布成功后，新查询只走 v3；稳定一个版本后再清理 v2 缓存。
5. 首个 PR 先保证全文覆盖和 span 正确；dirty queue/保存事件增量优化放在 P1-1，避免把正确性修复和事件架构改造绑成一个大提交。

### 8.4 改动规模和兼容性

预计规模：**中等偏大，但边界清晰**。主要集中在 Python RetrievalService、上下文 assembler、StorydexProjectSearch 和测试，不需要改作品文件格式。

对正常运行的影响：

- 项目源文件不变；变化的是 `.storydex/.cache` 下可重建索引。
- 首次构建时间和索引体积会增加，因为终于索引全文。可通过分块事务、后台预热和进度事件控制用户感知。
- 现有搜索排序会从“每文件一行”变为“每 chunk 一行”，需要先按 chunk 排名，再按文件/相邻 span 合并，避免同一文件占满前 N 条。
- 返回精确 span 会增加少量 payload，但会减少 Agent 为定位命中而再次盲读整个文件的工具轮数。
- 检索异常从静默空结果改为明确错误，某些此前“看似正常但证据为空”的回合会停止或降级提示。这是正确性提升，不应视为兼容性故障。

### 8.5 回归测试与完成定义

- 160,007 字符文件头、中、尾三个唯一词都能命中。
- 命中 snippet 覆盖唯一词，source span 可从原文件精确复原。
- 跨 chunk 边界短语能命中，合并后无重复/错序。
- 超长单行、中文、emoji、JSON/YAML 均能建立索引。
- 文件更新、删除、重命名后，旧 chunk 不残留。
- 构建中断后仍可使用上一版完整索引；不会暴露半构建数据库。
- 索引异常和零命中返回不同状态；被动上下文 Trace 保留异常原因。
- 同一路径多个 chunk 命中时，结果聚合不会挤掉其他高相关文件。

P0-3 完成定义：所有允许检索的项目文本在索引中都有可证明的覆盖范围；系统只有在索引 revision 完整且查询成功后，才允许报告“没有命中”。

## 9. P0 实施顺序和提交边界

推荐拆成三个独立 PR，按以下顺序合并：

1. **PR-P0-1 Session integrity**：严格恢复、首次落盘后绑定、原子保存、失败测试。
2. **PR-P0-2 Bounded read protocol**：UTF-8 安全截断、分页/revision、意图完整输入。
3. **PR-P0-3 Chunk FTS**：v3 分块索引、精确 span、错误状态和迁移。

原因：P0-1 规模最小且防止对话历史继续丢失；P0-2 为后续 Evidence Ledger 提供 span/revision 协议；P0-3 复用同一 revision/span 语义。不要并行发明三套 revision 格式。

共享数据约定应在 P0-2 开始前定稿：

```text
SourceRevision:
  path
  revision id
  size
  mtime_ns
  optional content hash

SourceSpan:
  start/end line
  start/end byte or char
  revision id
```

### 9.1 P0-1 与 P0-2 实施结果

P0-1 已完成的生产改动：

- 删除已绑定 session 加载失败后创建空 session 的静默 fallback。缺失、损坏、ID 不匹配或 schema 不兼容现在均明确失败。
- 新 session 先持久化成功，再发送 `session_bound`；事件携带 `persisted=true`、`sessionSchemaVersion=1` 和实际 `sessionPath`。
- Python 在写 Storydex binding 前校验 runtime UUID、schema、持久化状态和目标文件；Windows canonical path 的 `\\?\` 表示差异使用 `Path.samefile()` 判定。
- 执行前严格校验 binding 所属 workspace/session、runtime UUID、session 路径和 history 文件是否存在，失败时返回 `StorydexCoomiSessionRestoreError`，不删除或重建历史。
- SessionStore 改为同目录临时文件写入、`sync_all` 和 rename 发布；session schema 版本在 JSON 持久化边界注入和校验。
- 保留 `run_turn` 失败后保存已产生中断上下文的行为。

P0-2 已完成的生产改动：

- `read_file` 返回版本化 JSON envelope，包含 `protocolVersion`、`path`、`revision`、`span`、`totalLines`、`totalBytes`、`hasMore`、`nextOffset`、`nextByteOffset`、`truncated`、`truncationReasons` 和 `content`。
- 保留 `path/offset/limit`，新增 `byte_offset/expected_revision`；任何继续读取都必须携带前一页 revision，文件变化时返回结构化 `revision_conflict`。
- 超长单行可以按 byte cursor 连续读取；所有页的 byte span 可无重叠、无缺口地覆盖同一 revision。
- 通用工具输出截断改为 UTF-8 字符边界安全，并修复尾部换行产生虚假额外行的问题。
- 意图层移除内部 `prompt[:2000]`，使用入口已限制为 12,000 字符的完整 prompt；完整请求任意位置出现明确“不修改项目文件”时，确定性覆盖为 `canWrite=false`。

对正常运行的影响：

- 正常的新会话仅增加一次很小的首次持久化；正常旧会话继续复用原 runtime session。
- 只有原先被静默掩盖的 session 异常改为显式失败，避免带着空历史继续运行。这是预期的 fail-closed 行为。
- 短文件仍可一次读取完成，但工具输出多出完整性元数据；长文件为保证读全可能增加工具轮数。
- 超长请求会让意图模型看到更多输入 Token，但不再遗漏尾部权限约束；API 的 12,000 字符入口上限保持不变。
- 没有修改前端会话 ID、Agent 推理循环、Provider 配置格式、FTS 或 P1/P2 运行时架构。

真实 OpenCode 验收使用现有 `OPENCODE / deepseek-v4-flash` 配置的隔离副本，未改变用户当前激活 Provider，也未把 API key 写入日志或报告。最终报告：

```text
status: passed
意图分类: 8,883 ms, method=llm, canWrite=false
第一回合: 28,613 ms, read_file 8 次
2,001 行文件: 5 页, 72,027 bytes, 2,001 lines
中文超长单行: 3 页, 120,022 bytes, 1 line
第二回合: 4,598 ms, runtime session ID 不变, messages 15 -> 17
缺失 history: 仅 AgentError，未创建替代 session
损坏 history: 在模型轮前 AgentError，原损坏文件未被覆盖
```

验收期间实际发现并修复了两个集成问题：Windows 扩展路径前缀造成的同文件误判，以及持久化工具消息带 `success: ` 前缀导致的验收解析错误。第三次完整真实链路执行通过，报告位于 `output/agent-integrity-live/f1523bf852/acceptance-report.json`，该目录受 `.gitignore` 排除。

### 9.2 P0-3 实施结果

已完成的生产改动：

- 新索引使用 `.storydex/.cache/retrieval.fts5.v3.db`，保留 v2 文件用于回滚，不原地迁移或覆盖旧 schema。
- 所有允许检索的 UTF-8 文本都按约 3,200 字符分块，相邻块保留约 400 字符重叠；超长单行会强制切分，文件中部不再丢失。
- `documents` 保存 path、SHA-256 revision、字节数、字符数、行数、mtime_ns 和 chunk_count；每个 chunk 保存精确 char/byte/line span、revision 和原文。
- 查询先按 chunk 计算 BM25，再通过窗口函数按 path 选择最佳 chunk，避免同一长文件占满候选名额。
- 每个命中返回 chunk span 和可由源文件精确复原的 `snippetSpan`；Agent 不再需要先盲读整个文件才能定位证据。
- 全量构建在临时 SQLite 数据库完成覆盖和完整性校验后原子发布；失败时保留上一版完整 v3 和旧 v2。
- 增量更新在单事务内替换变化文件的全部 chunks，并清理删除/重命名后的旧 chunks。
- 主动工具明确返回 `ok/hits`、`ok/no_hits`、`index_building`、`index_stale` 或 `index_error`。失败工具仍以结构化 JSON 持久化，不能退化为不可解析错误文本。
- 被动 `related_passages` 把检索状态、generation、coverage 和错误写入 ContextTrace/notes；索引失败时向模型明确说明“不可解释为证据不存在”。

实际调优过程中发现暖态性能回退：初版在每次 `watch_files()` 都运行 SQLite `integrity_check` 并逐文档复核全部 chunks，300 文件无变化检查耗时 `5,783 ms`。修复后正常无变化直接返回，增量只校验变化文档，完整校验仅用于冷构建和异常恢复。

本机 300 文件、6.62 MB 源文本基准：

```text
文件: 300
chunks: 2,400
v3 database: 10.05 MB，约为源文本 1.52x
冷构建: 9,629 ms
暖态 watch_files: 77.84 ms（修复前 5,783 ms，约快 74 倍）
搜索 + stale 检查: 77.4 ms
```

真实 OpenCode 验收使用隔离的 `OPENCODE / deepseek-v4-flash` 配置副本，连续两次通过。最终报告：`output/agent-retrieval-live/fc3c389c9b/acceptance-report.json`。

```text
status: passed
第一回合: 13,496 ms，StorydexProjectSearch 4 次
索引覆盖: 68 documents / 140 chunks / 583,783 bytes
头部命中: chunk 0
中部命中: chunk 40，startChar=112,014，startByte=322,014
尾部命中: chunk 72
三处命中 revision 完全一致，char/byte snippetSpan 均可精确复原
不存在词: status=ok, resultState=no_hits
破坏 v3 后第二回合: 10,381 ms，success=false, status=index_error, resultState=unavailable
第二回合复用同一 runtime session
```

P0-3 没有修改作品文件格式、前端会话 ID、Provider 配置格式、Agent 推理循环或 P1/P2 架构。首次 v3 构建时间和缓存体积会增加，这是全文覆盖的必要成本；暖态仍会扫描文件元数据，后续由 P1-1 Content Catalog/dirty queue 继续消除。

## 10. P1 计划：消除重复工作并保护证据链

### 10.1 P1-1 Content Catalog 与 dirty queue

目标：TurnContract、Wiki、FTS、资源浏览器共用一个版本化文件清单，不在每个查询中重新 `rglob + stat + read`。

实施要点：

- 文件保存、创建、删除、重命名事件更新 catalog 和 dirty set。
- 外部编辑通过 watcher 进入同一队列；启动时做一次校验扫描。
- 每个文件记录 path、kind、revision、size、mtime_ns、可选 hash 和派生状态。
- Wiki/FTS 消费 dirty set，成功后按 revision 确认；失败不丢 dirty 项。
- 按 workspace 建 single-flight，避免并发请求重复刷新相同 revision。
- 查询链路只读已发布 snapshot；刷新和重建不嵌在 `query()` 中。

验收：300 章节暖态 TurnContract 不再多次调用 `list_chapter_states()`；无变化 Wiki/FTS 查询不做全树遍历；单文件保存只处理该文件及必要派生项。

### 10.2 P1-2 Evidence Ledger

目标：跨工具轮和跨回合记录“Agent 已读过哪个 revision 的哪些范围”，文件未变化时复用证据，变化时精确失效。

建议记录：

```text
session_id
path
revision
read spans
retrieval hit spans
first/last observed turn
source tool
content hash or result hash
```

实施要点：

- `read_file` 和 ProjectSearch 成功后自动写 ledger，不依赖模型自报。
- 相邻/重叠 span 合并，避免账本膨胀。
- 新回合只向模型注入紧凑 evidence 摘要；需要原文时可按 ledger 引用重新取回。
- 文件 revision 不变时，重复读取相同 span 返回缓存或明确 `already_read` 提示。
- 文件 revision 变化时只失效该文件旧 evidence，不清空整个 session。
- 对“检查全部候选文件”类任务建立 coverage gate，未覆盖时不能声称“全部检查完成”。

验收：真实 session 中相同 revision+span 的重复 `read_file` 显著下降；Trace 能回答“结论来自哪个文件版本的哪一段”。

### 10.3 P1-3 检索触发和错误可观察性

目标：普通自然语言主题请求也能形成受控查询，同时不把功能词噪声送入 FTS。

实施要点：

- 增加内容词/实体/query planner，不再只依赖活动实体和引号短语。
- 对检索词、索引 revision、候选数、选中 span、错误状态写结构化 Trace。
- “无查询词”“零命中”“索引不可用”“预算丢弃”必须分开。
- 相关证据正文优先进入预算，candidate paths 留在 metadata。

验收：`请续写星核密钥引发的冲突` 与带引号版本都触发可解释检索；索引失败时 Agent 不得断言项目没有相关内容。

### 10.4 P1-4 规划 LLM 去重或真正接入

当前复杂任务会并行调用独立规划 LLM，但主 Agent 不等待且不消费规划结果。规划多用于事后 UI/Trace，可能形成纯额外 Provider 调用。

二选一，需产品决定：

- 删除独立规划 LLM，由主 Agent 的 Plan/Loop 作为唯一执行计划来源。
- 保留规划 LLM，但在主 Agent 开始前把结构化计划注入 TurnContract，并让执行/Trace 都引用同一个 task id。

禁止维持“付出一次模型调用，但执行不受其约束”的中间状态。

验收：每次规划调用都有可验证的执行消费者；否则调用数为零。

### 10.5 P1-5 真实 Token 预算、LRU 和 JIT 上下文

实施要点：

- 用 Provider tokenizer/估算器统一计算 system、history、tools、TurnContract 和预留输出预算。
- 启用前先让 `CONTEXT_TOKEN_BUDGET_REAL` 在 shadow 模式记录差异。
- LRU 以 `workspace + source revision + context block config` 为 key。
- JIT 只注入任务必需摘要和证据索引，需要原文时再调用工具。
- 预算删除任何块时，模型和 Trace 都必须看到 `omitted/truncated` 状态。

验收：相同输入的预算结果可复现；不会因字符数适配而超过模型窗口；缓存命中不复用旧 revision。

### 10.6 P1-6 结构化压缩检查点

当前压缩会：

- 先把较早工具结果替换为统一的截断文本。
- 最多保留 20,000 token 的真实用户消息。
- assistant/tool 的细节主要依赖一次 LLM 摘要。

改进方向：

- Plan、Loop、未完成动作、用户约束、文件 revision、Evidence Ledger 独立持久化，不只放在自然语言摘要中。
- 压缩前生成机器可校验 checkpoint；摘要只承担叙述性上下文。
- 保留最近完整工具调用链和所有未完成 tool call 配对。
- 对关键用户约束建立不可压缩字段或结构化引用。
- 压缩后运行一致性检查：目标、权限、未完成任务、evidence revision 均存在。

验收：构造超窗口会话并压缩后，用户权限约束、目标文件、未完成计划和证据引用不丢；缺字段时压缩失败并保留原 session。

## 11. P2 计划：运行时与 Provider 架构优化

### 11.1 P2-1 长生命周期 bridge/session worker

目标：按 workspace/session 复用 Rust worker，避免每回合重新加载 Provider、MCP、Hooks、Memory 和工具。

需要解决：

- worker 生命周期、空闲回收和应用退出清理。
- 配置/skill/hook/MCP 变更的 revision 感知和热重载。
- workspace/session 隔离和并发 turn 串行化。
- 崩溃自动拉起，但必须从已持久化 checkpoint 恢复，不能静默丢状态。
- Windows 桌面打包后的子进程句柄与升级行为。

验收：连续 10 回合只初始化一次未变化组件；worker 崩溃后恢复到最后已确认 checkpoint；不同 workspace 不共享可变状态。

### 11.2 P2-2 Provider 增量 conversation/response 协议

目标：Provider 支持时，用 conversation id、response id 或 prefix caching 语义发送增量，而不是每个工具轮重发完整 history 和全部工具 schema。

实施要点：

- Provider capability 明确声明是否支持增量状态。
- 本地 session 保持权威副本，远端 id 只是可失效加速器。
- 远端状态丢失时显式回退到一次完整 replay，并记录原因和成本。
- tool schema 版本化；未变化时复用，变化时刷新。
- 统计 `logical input tokens`、`transmitted tokens` 和 `cached tokens`，避免仅看 Provider 账单字段误判。

验收：多工具轮任务的 transmitted input 随新增消息增长，而不是随完整历史线性重复；增量状态失效不影响语义正确性。

### 11.3 P2-3 统一内容与检索内核

目标：Wiki、FTS、TurnContract、ProjectSearch、read_file 共用 SourceRevision/SourceSpan/Content Catalog，不再各自定义截断、错误和新鲜度规则。

实施要点：

- 查询 API 纯读 published snapshot。
- 索引/投影生成是独立后台任务。
- 所有返回都带 revision、span、coverage 和 freshness。
- 旧的重复检索路径在等价测试通过后删除。

验收：同一 query 在被动上下文和主动工具中的候选集合、source span 与错误语义一致；只有展示预算不同。

## 12. 可观测性与性能门槛

每个 Agent turn 建议记录：

```text
intent_input_chars / intent_input_complete
contract_build_ms / directory_scan_count / stat_count
catalog_revision / dirty_file_count
wiki_refresh_ms / fts_refresh_ms / fts_coverage
bridge_start_ms / component_init_ms
model_rounds / tool_calls / duplicate_tool_calls_same_revision
logical_input_tokens / transmitted_input_tokens / cached_input_tokens
read_bytes / unique_evidence_bytes / evidence_coverage
compaction_count / compaction_checkpoint_valid
session_load_status / session_save_status
```

建议的阶段性门槛：

- P0：所有 session 恢复失败、读取截断、索引不完整都有显式状态；硬损失复现全部转为通过。
- P1：300 章节暖态 TurnContract 的目录扫描归并为一次 snapshot 读取；无变化 Wiki/FTS 查询不全树扫描。
- P1：相同 revision+span 的重复读取率可测并显著下降，目标值在收集一周真实样本后确定。
- P2：连续回合 bridge 初始化次数从每回合 1 次降到每 worker 生命周期 1 次。
- P2：工具轮 transmitted input 不再重复发送可复用前缀；按 Provider 能力分别设定基线。

## 13. 验证矩阵

| 范围 | 必测内容 |
|---|---|
| Rust session | 新建、正常恢复、缺失、损坏、schema 不兼容、原子保存、运行失败后保存 |
| Python binding | workspace 隔离、首次绑定时机、悬空 binding、删除/清空/回滚 |
| Rust read_file | 多页、长单行、UTF-8、revision 冲突、输出预算、路径安全 |
| Intent/TurnContract | 长 prompt 尾部否定、目标路径、章节号、写权限、显式超限错误 |
| FTS | 头中尾命中、跨块短语、增删改名、构建中断、错误与零命中区分 |
| Context | snippet/span 对齐、预算删除可见、缓存 revision 失效 |
| Compaction | 权限/目标/计划/evidence checkpoint 保留、tool call 配对 |
| 性能 | 300 章节 TurnContract、2,000 文件 catalog/FTS、长 session 多工具轮 |

P0 全项实施后的验证结果：

```text
Python 聚焦组合: 106 passed
Python Agent 扩展回归: 156 passed
Rust workspace: 全部通过
  coomi-catalogs: 2
  coomi-engine: 14
  coomi-security: 3
  coomi-services: 63
  coomi-tools: 14
  coomi-ui: 19
  storydex-coomi-bridge: 9
受影响 crate Clippy -D warnings: passed
cargo fmt --all: passed
git diff --check: passed
OpenCode 真实 Agent 验收: passed
P0-3 聚焦检索/状态/Trace: 37 passed
P0-3 检索/Wiki/上下文扩展回归: 85 passed
P0-3 Agent/意图/路由/公开契约回归: 111 passed
Python Ruff（P0-3 生产、测试和验收脚本）: passed
P0-3 OpenCode 真实 Agent 验收: 2 次 passed
```

完整 workspace Clippy 仍会被 `vendor/coomi-rs/ui/src/terminal_ui/mod.rs:999` 的 `clippy::large_enum_variant` 阻断；告警位于未被 P0 修改的 TUI `RuntimeEvent` 枚举。P0 涉及的三个 Rust crate 已单独在 `-D warnings` 下通过，不应为完成 P0 扩大修改 TUI 架构。

后续每个 P1/P2 变更包仍应运行对应聚焦测试、受影响 crate 测试和至少一条真实 Agent 链路。

## 14. 工作区与交付注意事项

P0-1/P0-2/P0-3 当前以未提交工作区改动存在。继续工作前必须执行 `git status` 和 `git diff`，不得回退用户或其他任务的并发改动。

`apps/backend/tests/test_graph_live_acceptance_script.py` 当前有一处不属于 P0-1/P0-2 的并发测试改动：它把固定 Provider/Model 常量替换为测试局部值。该改动应保留，但不能计入本轮变更或被一并回退。

真实验收通过临时目录复制单个 Provider 配置，结束后自动删除临时配置和密钥副本。`output/` 下的验收报告被 Git 忽略；提交前仍应检查暂存差异中没有 API key、Authorization header 值或临时 Provider 配置。

## 15. 明确禁止的修复方式

- 不得在 session 恢复失败时创建新 session 后继续。
- 不得删除损坏 session 或 binding 后假装恢复成功。
- 不得只把 500 行、2,000 行、48,000 字节或 120,000 字符调大。
- 不得用无意义重试掩盖确定性初始化/持久化错误。
- 不得把 FTS 异常继续转换成“零命中”。
- 不得让查询链路隐式重建并写入大量派生文件。
- 不得在没有 revision 的情况下缓存文件内容或工具结果。
- 不得把自然语言 compaction summary 当作 Plan、权限和证据账本的唯一副本。

## 16. 后续对话接手顺序

### 新对话处理 P1

```text
请读取 docs/Agent运行链路信息完整性与性能治理.md 的第 10、12、13、14 节。
P0-1/P0-2/P0-3 已完成；先复核现有 SourceRevision/SourceSpan 和 v3 retrieval 状态协议，禁止另造不兼容格式。
本轮优先设计并实施 P1-1 Content Catalog，不同时改 planner 或长生命周期 bridge。
先复跑 300 章节 TurnContract 和无变化 Wiki/FTS 的扫描基线，
以“暖态不全树扫描、单文件变更只精确失效”为验收标准。
```

### 新对话处理 P2

```text
请读取 docs/Agent运行链路信息完整性与性能治理.md 的第 11 至 14 节，
检查 P0/P1 的 revision、checkpoint、catalog 和 evidence ledger 是否稳定。
先给出长生命周期 bridge worker 的状态机、隔离边界、崩溃恢复和配置热重载设计，
不要在这些前置协议未稳定时直接改为常驻进程。
随后用连续 10 回合初始化次数和多工具轮 transmitted input 作为验收基线。
```

## 17. 最终完成标准

只有同时满足以下条件，才能认为本专项完成：

1. session 历史不会因缺失、损坏、初始化失败或保存中断而静默归零。
2. 任何有界读取都带 revision、span、总量、截断原因和继续方式。
3. 所有可检索项目文本都有全文 chunk 覆盖，零命中与系统错误可区分。
4. 暖态 TurnContract/Wiki/FTS 不重复全树扫描。
5. 同 revision 的证据可跨工具轮/回合复用，变更后精确失效。
6. 压缩后结构化权限、目标、计划和证据引用仍可验证。
7. bridge 和 Provider 的重复成本被量化，并在 P2 后显著下降。
8. 所有结论都由回归测试、Trace 指标和复现基线支持，而不是仅依据实现意图。
