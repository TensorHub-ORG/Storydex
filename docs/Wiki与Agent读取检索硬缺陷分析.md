# Storydex Wiki 与 Agent 读取/检索硬缺陷分析

分析日期：2026-08-04

分析范围：当前本地工作区的 Wiki、Agent 上下文、主动/被动检索和 Coomi Rust 文件工具

信息来源：仅基于本地代码、Git 历史和本地临时项目复现，未进行外部检索

本轮交付：完成一组局部止血更新并补充回归测试；不涉及工具调用协议、记忆系统、Content Catalog、Evidence Ledger 或 Wiki contribution 架构重构

阅读说明：第 15 节保留修复前的本地复现基线；第 16 节记录 2026-08-04 本轮实际完成项、验证结果和仍未解决的边界。

## 1. 结论

当前问题不是一个单独的“只读取前 4000 字”缺陷，而是三类硬缺陷叠加：

1. **Wiki 的增量同步仍是伪增量，但已去掉一轮明显的重复全库扫描。** 本轮将实体 registry 归并改为只读取规范/兼容角色卡目录，因此暖态读取从“两轮全量源扫描”降为“一轮角色卡定向扫描 + 一轮全量源扫描”。任意源变化仍会进入完整 `rebuild()`，查询链路也仍会检查新鲜度并可能写盘。
2. **Agent 每轮预检和上下文装配存在大量重复目录遍历。** 在 300 个章节文件的临时项目中，即使关闭被动 FTS，单次暖态 `build_turn_contract()` 仍耗时 4.264 秒，调用 `list_chapter_states()` 13 次，产生 42,898 次 `stat`。
3. **读取和检索仍由多组互不一致的固定窗口组成。** 本轮已把 Agent 最近正文改为尾部 700 字、活动实体识别改为 3000 字头尾窗口，并让 FTS 从真实源文件的命中位置生成摘要。Fact/Relationship fallback 仍取开头 4000 字，FTS 正文索引仍最多保留 120000 字的头尾，超长文件中部事实依然不可召回。

此前确实做过优化，本轮又完成了若干局部止血项，主要改善了重复扫描、章尾上下文、索引判脏、事务和摘要正确性，但没有消除上述渐进复杂度和覆盖率缺陷。继续把 4000 调成 8000 或 12000，只会推迟问题，不会解决根因。

建议优先完成四件事：

1. 将 Wiki 查询改为纯快照读取，刷新/重建从查询链路拆出。
2. 建立工作区共享文件清单和变更队列，Wiki、FTS、上下文装配复用同一版本化清单。
3. 将 FTS 改为全文分块索引并返回真实命中区间，取消“头部生成摘要”和“头尾代替全文”。
4. 将 Agent 当前文件上下文改为光标附近窗口或文件尾窗口，并加入实际读取覆盖率账本。

## 2. 当前调用链

### 2.1 普通 Agent

```text
POST /agent/chat
  -> 意图识别
  -> build_turn_contract()
     -> 多次 list_chapter_states()/目录 stat
     -> StorydexContextAssemblerService.assemble()
        -> 最近章节、角色、世界书、事实、关系、物品
        -> RetrievalService.watch_files()
        -> FTS 查询和 Wiki 快照读取
  -> 上下文块写入 Coomi system prompt
  -> Rust Agent
     -> StorydexProjectSearch -> watch_files -> FTS
     -> StorydexWikiQuery -> query_graph -> read_or_build
     -> read_file -> 行窗口读取
     -> search/grep_files -> 工作区全文正则扫描
```

入口和关键实现：

- `apps/backend/api/routes_agent.py:192`：请求只有 `activeFile`，没有光标、选区或当前可见行范围。
- `apps/backend/services/storydex_context_assembler_service.py:36`：上下文装配入口。
- `apps/backend/services/storydex_context_assembler_service.py:694`：被动 FTS。
- `apps/backend/services/storydex_agent_tools.py:174`：主动项目检索工具。
- `apps/backend/services/storydex_agent_tools.py:254`：主动 Wiki 查询工具。
- `vendor/coomi-rs/tools/src/lib.rs:787`：Rust `read_file`。
- `vendor/coomi-rs/tools/src/lib.rs:890`：Rust `search/grep_files`。

### 2.2 Wiki

```text
GET /story/wiki
  -> read_or_build()
     -> _reconcile_entity_registry()
        -> _collect_character_sources()       # 只读角色卡目录
     -> _collect_sources()                    # 全量扫描 1
     -> 校验 / 可能 sync_local_incremental()
        -> _reconcile_entity_registry()       # 变化时再次定向扫描角色卡
        -> _collect_sources()                 # 变化时第二轮全量扫描
        -> rebuild()                          # 任意变化均完整重建

GET /story/wiki/graph
  -> query_graph()
     -> read_or_build()                       # 再走上述链路
```

关键实现：

- `apps/backend/services/story_wiki_service.py:231`：`read_or_build()`。
- `apps/backend/services/story_wiki_service.py:563`：`sync_local_incremental()`。
- `apps/backend/services/story_wiki_service.py:615`：当前无调用者的旧局部增量实现。
- `apps/backend/services/story_wiki_service.py:1080`：`query_graph()`。
- `apps/backend/services/story_wiki_service.py:3652`：全量 `_collect_sources()`。
- `apps/backend/services/story_wiki_service.py:3662`：角色卡定向 `_collect_character_sources()`。
- `apps/backend/services/story_wiki_service.py:3769`：实体 registry 归并。
- `apps/frontend/src/components/StoryStatePanel.vue:1915`：先读 Wiki。
- `apps/frontend/src/components/StoryStatePanel.vue:1923`：随后再读图谱。

## 3. 严重级别总表

| ID | 级别 | 缺陷 | 直接后果 | 本轮状态 |
|---|---|---|---|---|
| W-01 | P0 | Wiki 查询/同步重复全量扫描 | 打开、刷新、Agent 查 Wiki 都随总文件数和总字节数增长 | 部分缓解：角色归并改为定向扫描，仍保留一轮全库扫描 |
| W-02 | P0 | 任意变化完整重建，旧局部增量失活 | 单文件保存触发全量派生计算与全量持久化 | 未解决 |
| A-01 | P0 | Agent 每轮重复扫描章节目录 | 模型调用前即出现数秒固定延迟，项目越大越严重 | 未解决 |
| R-01 | P0 | FTS 用 `mtime <= indexed_at` 判断未变化 | 保留/回拨时间戳时索引永久陈旧 | 已修正为比较已存 `mtime + size`；同 mtime 同 size 改写仍可能漏掉 |
| R-02 | P0 | 120000 字头尾索引丢弃中部 | 超长文件中部事实完全不可召回 | 未解决 |
| A-02 | P0 | 最近正文读取开头 700 字而非结尾 | 续写看不到当前场景结尾，连续性直接受损 | 已修复：仅 Agent assembler 启用尾部窗口，其他调用默认不变 |
| A-03 | P1 | 活动实体只从 prompt + 文件头 3000 字识别 | 章尾实体漏掉后，事实、关系、Wiki、FTS 均可能不触发 | 部分修复：改为 3000 字头尾窗口，文件中部和其他头部窗口仍存在 |
| R-03 | P1 | FTS 命中后仅从文件头 4000 字生成摘要 | 路径命中但摘要为空，被动上下文没有证据正文 | 已修复：复用索引的受限头尾源窗口生成真实命中摘要 |
| R-04 | P1 | `watch_files()` 每次全树遍历并逐文件 `stat` | 无变化检索仍为 O(文件数)，首次索引写库成本高 | 部分修复：写库改为单事务；全树遍历仍未解决 |
| T-01 | P1 | Rust `search` 同步全文读取整个工作区 | 无命中最慢，阻塞异步运行时，不能及时取消 | 未解决，本轮明确不改工具协议 |
| T-02 | P1 | Rust `read_file` 先全文读入，再按行截取 | 窗口读取仍付出整文件 I/O/内存成本，超长单行无法翻页 | 未解决，本轮明确不改工具协议 |
| T-03 | P1 | Rust 输出按 48000 字节直接 `String::truncate` | 多字节 UTF-8 边界上存在 panic 风险 | 未解决，本轮明确不改工具协议 |
| A-04 | P1 | 主动检索和继续读取只是提示，不做覆盖率验收 | Agent 可以未读候选文件就断言事实不存在或直接生成 | 未解决 |
| P-01 | P1 | 标记为只读的检索工具会写工作区 | Plan/只读语义与真实文件系统副作用不一致 | 未解决 |
| O-01 | P1 | 工具事件仅保留前 4000 字，审计再缩到 2000 字 | UI/Trace 无法证明模型实际读取了哪些后续内容 | 未解决，本轮明确不改工具调用与记忆协议 |
| A-05 | P2 | 多套检索实现的范围、截断和排序规则不一致 | 同一查询在 Agent、文件搜索和被动上下文中结果不同 | 未解决 |

## 4. Wiki 扫描与重建缺陷

### 4.1 “增量”只避免无变化写盘

`sync_local_incremental()` 会计算 `changed_paths` 和删除路径，但只要集合非空，就把已经收集的全部 `sources` 传给 `rebuild()`。`_build_incremental_payload()` 仍留在文件中，却没有调用者。

这意味着：

- 无变化：全量扫描和校验，最后不写盘。
- 一个文件变化：全量扫描、全量派生、全量索引、全量持久化。
- 一个文件删除或重命名：同样完整重建。

这是为了防止 overview、timeline、topic、index 等派生数据残留旧内容而做的正确性取舍，但当前实现把“保证全局投影一致”错误地等同于“重新读取全部源文件”。正确做法应是增量更新源贡献，再从贡献表重算轻量全局投影。

### 4.2 实体归并的第二轮全库扫描已消除

修复前，`read_or_build()` 先调用 `_reconcile_entity_registry()`；后者为了找角色卡，内部调用 `_collect_sources()`。返回后，`read_or_build()` 又调用一次 `_collect_sources()` 计算校验和与诊断。

本轮新增 `_collect_character_sources()`，只枚举 `.storydex/characters/`、兼容的根级 `characters/`，以及各自的 `cards/` 直接子文件。`states/` 等派生目录不会进入 registry 归并。当前调用次数变为：

- `rebuild()`：一轮角色卡定向扫描 + 一轮全量源扫描。
- `sync_local_incremental()`：一轮角色卡定向扫描 + 一轮全量源扫描。
- `read_or_build()` 暖读：一轮角色卡定向扫描 + 一轮全量源扫描。
- `read_or_build()` 检测到变化后再进入 `sync_local_incremental()`：同一请求仍可能执行两轮全量源扫描。
- `query_graph()`：仍不是纯查询缓存，而是先完整执行 `read_or_build()`。

因此 W-01 只是从“重复全库读取”降为“定向角色读取 + 全库读取”，并没有达到暖读 0 次源扫描的目标。

### 4.3 扫描范围和单文件成本过大

`_collect_sources()` 对工作区根执行 `rglob("*")`，排除项只有 `.git`、`__pycache__`、`.cache`、`traces`、`sessions` 和少数 `.storydex` 前缀。任何放在工作区中的源码、导出文件、日志、第三方内容，只要后缀是 `.md/.txt/.json/.jsonl`，都可能进入 Wiki 扫描。

每个候选源会执行：

1. 文件类型和后缀判断。
2. 全文读取；JSON 还会完整解析并重新格式化。
3. 全文 SHA256。
4. 多次 `stat` 获取 size 和 mtime。
5. 将全文保留在 `sources` 列表中，直到构建结束。

时间复杂度至少为 O(文件数 + 总字节数)，峰值内存也近似 O(总源文本字节数)。

### 4.4 二次复杂度索引已修复

修复前，`apps/backend/services/story_wiki_service.py:2266` 的 `_build_index()` 对每个 source 再遍历全部 entries，并由相关 entry 遍历 nodes，随着章节和条目共同增长接近 O(S×E + S×N)。

本轮先建立 `source_path -> entry_ids` 和 `entry_id -> source_paths`，再按 node 原顺序建立 `source_path -> node_ids`。最终每个 source 只做映射读取，保留原 entry/node 输出顺序和去重语义。这个局部热点已消除，但整个 Wiki 仍会全量读取源正文和完整重建投影。

### 4.5 查询链路带写入和重建副作用

`StorydexWikiQuery` 标记为 `READ_ONLY`，但它调用 `query_graph() -> read_or_build()`。当源变化、schema 过旧或诊断阻断时，会写入 Wiki 投影；前置的 `_reconcile_entity_registry()` 还可能改写 `.storydex/memory/current/entities.json`。

结果是：

- 只读 Agent 查询可能触发几十秒扫描和重建。
- Plan 模式允许该工具，但不能保证工作区零写入。
- 查询延迟和同步延迟绑定，无法给查询建立稳定 SLA。

### 4.6 前端重复触发新鲜度检查

`loadWiki()` 获取 `/story/wiki` 后立即 `await loadWikiGraph()` 请求 `/story/wiki/graph`。两个端点都会走 `read_or_build()`。本轮去掉实体归并内部的全库扫描后，无变化时打开一次面板仍通常执行两轮全量源扫描外加两轮角色卡定向扫描；如果第一次请求刚好检测到变化，扫描次数还会更多。

### 4.7 Wiki 本地摘要不是全文语义提取

Wiki 的源收集是全文读取，但本地条目并没有全文理解：

- 章节 summary 只压缩开头 260 字。
- details 只取前 5 个非空行。
- plot overview 主要取首章和末章的开头片段。

因此“Wiki 全文扫描”不等于“章尾事实进入 Wiki”。本地复现中，放在章尾的唯一事实既没有进入章节条目，也无法通过 Wiki 查询命中。

### 4.8 缺少并发单飞

当前没有看到按工作区维度的 Wiki build single-flight 或跨进程锁。两个查询、Agent 同步和保存后同步并发时，可以重复构建同一 revision。单个目标文件使用临时文件加 `os.replace` 能保证该文件替换原子性，但 JSON、Markdown、source index 三个目标并不是一个跨文件事务。

## 5. Agent 上下文读取缺陷

### 5.1 每轮预检重复遍历章节

普通 Agent 在模型开始前先构建 TurnContract。该过程在编排、章节目标规划、generation context、recent segments 等位置反复调用 `list_chapter_states()` 和 `_ordered_segment_paths()`。

特别需要注意：`list_chapter_states()` 不只是读取。它先调用 `_normalize_chapter_directories()`，该函数会加锁、扫描目录，并在需要时重命名章节目录和重写状态。把它重复用于只读上下文查询，成本和副作用都偏大。

本地 300 章节 profile 结果：

| 指标 | 结果 |
|---|---:|
| 被动 FTS | 关闭 |
| 暖态总耗时 | 4.264 秒 |
| Python 调用数 | 1,325,712 |
| `list_chapter_states()` | 13 次 |
| `_ordered_segment_paths()` | 4 次 |
| `Path.stat()` | 42,898 次 |
| `nt.stat` 累计耗时 | 2.931 秒 |

这说明即使完全不查 Wiki、不跑 FTS，Agent 仍有明显的 O(章节数 × 重复调用次数) 前置成本。

### 5.2 最近正文读取方向已局部修复

修复前，`_recent_segments()` 调用 `list_recent_segments(include_content=True, max_chars=700)`；后者使用 `_read_text_preview()`，读取的是每个最近文件的开头，不是结尾。

本轮为 `list_recent_segments()` 增加默认关闭的 `read_from_tail` 参数，只在 `StorydexContextAssemblerService._recent_segments()` 的两条调用路径传入 `True`。因此 Agent 续写上下文现在注入最近文件尾部 700 字，其他调用方仍维持原有头部 preview 语义，避免无关行为变化。

这只是“没有 cursor 时取文件尾”的止血方案。请求仍不携带光标/选区，用户编辑文件中部时仍无法注入真正的局部正文；活动文件也仍被被动 FTS 排除。

### 5.3 请求没有光标局部上下文

`AgentChatRequest` 只携带活动文件路径，没有编辑器光标、选区、可见行或当前段落范围。后端无法知道用户正在文件中部还是末尾，只能使用固定文件头。

应至少传入：

- `cursorLine` 或 `cursorOffset`。
- 当前选区起止位置。
- 编辑器内尚未保存的缓冲区 revision/hash。
- 需要时附带光标前后受限窗口，而不是整份未保存正文。

### 5.4 实体识别漏失会级联

修复前，`_infer_active_entities()` 只处理 prompt 和活动文件前 3000 字。本轮改为读取总计 3000 字的头尾窗口，可以覆盖常见的章尾实体，但不是全文扫描。若实体位于未保留的文件中部而未被识别：

- Fact/Relationship 查询会 fallback 到活动文件前 4000 字。
- Wiki reference 因 active entities 为空而跳过。
- 被动 FTS 若 prompt 无引号短语或章节引用，也会得到空查询。
- Item/角色/世界书相关性排序继续使用文件头部窗口。

此前复现中，实体位于第 100 字时 Fact/Relationship 均有上下文；实体移到第 4500 字后，两者均为空。本轮回归测试确认章尾实体可被 assembler 识别，但 Fact/Relationship 自身的 fallback、Item 和其他相关性排序仍使用各自的头部窗口；实体位于长文件中部时，级联漏召回仍可能发生。

### 5.5 被动查询词过窄

`_related_passage_query_terms()` 只接受：

- 已识别的活动实体。
- prompt 中中文引号/书名号包围的 2 至 24 字短语。
- 明确章节引用。

普通自然语言主题词不进入查询。复现中：

- `请续写星核密钥引发的冲突` -> 空查询。
- `请续写“星核密钥”引发的冲突` -> 查询 `星核密钥`。

收窄查询是为了避免把“继续写”等指令词切成中文 bigram 后污染结果，这个动机合理；缺陷在于没有独立的内容词提取/查询规划层，导致“去噪”直接退化成“多数 prompt 不检索”。

### 5.6 检索块证据优先顺序已修复

上下文总预算默认 10000 字符，而各块最大值之和超过 14000。`related_passages` 排在预设、最近片段、摘要、角色、世界书、事实、关系和物品之后，采用先到先得的剩余预算。

修复前，检索块先写最多 30 个 candidate paths，再写最多 3 段 snippet。路径较长时，1600 字块预算可能先被路径列表吃完，真正的命中正文位于尾部并被截掉。

本轮已把最多 3 段命中 snippet 移到候选路径列表之前，并增加 30 个长路径的回归测试。候选路径目前仍在 prompt 块中，完整方案仍应把它们保留在结构化 metadata，只向模型注入受限提示。

上下文 Trace 能记录某块被截断或因预算丢弃，但这些 `notes` 没有写入 Coomi system prompt，模型不知道相关检索证据已经被预算移除。

### 5.7 Wiki Agent 没有读取覆盖率门禁

Wiki Agent 初始 prompt 提供：

- 完整 source manifest，但只有路径、kind、hash、size、mtime。
- 最多前 48 个源的预览。
- 每个预览最多 420 字，仍是开头压缩。
- 最多前 100 个已有 Wiki 条目。

prompt 要求 Agent 主动读取相关文件，Rust 工具也允许按 offset 继续读取，所以 48×420 不是绝对模型上限。真正缺陷是后端只保留最后 80 个 Agent 事件，并不验证哪些 source hash 或区间被实际读取。Agent 可以只看 sample 就提交“全量分析”结果，后端仍会接受。

## 6. 主动与被动检索缺陷

### 6.1 `watch_files()` 仍是全量元数据扫描

`RetrievalService.watch_files()` 每次都：

1. 读取全部 `doc_meta`。
2. 对 `chapters/` 和 `.storydex/` 执行递归遍历。
3. 对每个候选文件执行 `stat`。
4. 构建 `seen_paths` 才能发现删除。

因此它只在“正文内容读取/写库”层面增量，发现变化仍为 O(文件数)。上下文装配每轮调用一次，Agent 每次主动 `StorydexProjectSearch` 又调用一次。

修复前，连接使用 `isolation_level=None`，删除、插入、更新 metadata 都是独立 autocommit。2000 个小文件的临时项目基线为：

| 操作 | 更新文件数 | 耗时 |
|---|---:|---:|
| 首次 `watch_files()` | 2000 | 15.9817 秒 |
| 无变化再次 `watch_files()` | 0 | 0.4035 秒 |

本轮将 `build_index()` 和 `watch_files()` 的写操作放入显式 `BEGIN IMMEDIATE` 事务，异常时回滚，已用“全量重建中途失败后旧索引仍可查询”的测试验证原子性。`watch_files()` 先完成只读 metadata 差异收集，确认有更新/删除后才获取写锁；无变化时不启动写事务。递归遍历和逐文件 `stat` 没有改变，因此无变化调用仍是 O(文件数)。

### 6.2 基础变化判断已修复

修复前，`doc_meta` 同时存储 `mtime` 和 `indexed_at`，但 `watch_files()` 读取的是 `indexed_at`，判断条件是当前 `mtime <= indexed_at` 就跳过。已存的文件 mtime 和 size 没有用于比较。

本轮已改为比较当前 `(mtime, size)` 与 `doc_meta` 中已存的 `(mtime, size)`；只要任一值不同，即使当前 mtime 早于 `indexed_at`，也会重新入库。回归测试覆盖了“新 mtime 不同但仍早于索引时间”的场景。

仍会漏掉：

- 内容变化后同时保留完全相同的 mtime 和字节数。
- 文件系统时间精度不足且改写前后大小相同。
- watcher 丢事件，而查询前 reconciliation 又碰巧看不到 metadata 差异。

修复前本地复现：先索引“旧有暗号”，把文件改成“全新密钥”后恢复原 mtime；再次 `watch_files()` 返回 0，旧词仍命中，新词完全不可搜。彻底解决仍需要保存事件/文件 watcher 的 dirty 标记，以及对可疑同元数据改写计算 hash。

### 6.3 超长文件中部被永久丢弃

索引通过 `read_text_limited(..., 120000, preserve_tail=True)` 读取。超过限制时只保留头部和尾部，中间插入 marker。索引并不知道被删掉的中部内容。

本地 160006 字文件把唯一词放在中部，建库成功但 `candidatePaths` 为空。这不是摘要缺失，而是召回层完全不存在该事实。

### 6.4 第 4000 字后空摘要已修复

修复前，FTS 排名基于最多 120000 字的头尾内容，摘要却固定从源文件前 4000 字生成。命中位于第 4000 字以后时，FTS 返回正确 path，但 snippet 为空，被动上下文最终只留下 candidate path。

本轮 `_materialize_hits()` 改为复用索引阶段的 `read_text_limited(..., 120000, preserve_tail=True)` 受限源窗口，再调用既有 `_build_snippet()`。摘要读取范围与 FTS 实际可召回范围一致；对大文件底层只读取头尾，不会为了尾部命中顺序扫描整个文件。回归测试确认位于第 4000 字后的“暮色钥印”能够同时返回 path 和非空命中摘要。

该修复只解决“已被 FTS 召回但摘要位置错误”。超过 120000 字后被头尾索引丢弃的中部内容仍不会产生 candidate path，必须由全文 chunk FTS 解决。

### 6.5 查询 token 截断和 OR 语义

中文查询会展开原词、bigram 和 trigram，之后只保留前 24 个 token，并用 OR 连接。活动实体较多时，前几个实体就可能耗尽 token 预算，后续实体被静默丢弃；OR 又会让常见二元词命中大量弱相关文件。

应由查询规划器控制实体/短语权重，并保留“原始短语必须命中”与“分词召回扩展”两种不同子查询，而不是把所有 token 平铺成一个 OR。

### 6.6 Rust 内建 `search/grep_files` 是另一套全盘扫描

Rust 工具不使用 FTS。每次调用都会通过 `WalkBuilder` 遍历目标目录，对每个文件同步执行 `std::fs::read_to_string()`，再逐行跑正则：

- 没有文件大小上限。
- 没有后缀白名单。
- `hidden(false)` 会包含隐藏路径。
- 没有总读取字节预算。
- 无命中时必须读完整个工作区。
- 同步文件 I/O 位于 async tool 中，扫描期间阻塞运行时，取消也不能及时生效。
- 达到 `max_results` 或 48000 字节后没有完整的剩余结果游标。

Agent 可能先为 FTS 支付一次全树 `stat`，再因精确查询调用 core search 支付一次全工作区全文读取。

### 6.7 检索实现彼此不一致

当前至少存在三套文本检索：

| 实现 | 主要调用者 | 范围 | 截断/行为 |
|---|---|---|---|
| `RetrievalService` | 被动上下文、`StorydexProjectSearch` | `chapters/` + `.storydex/` | 120000 字头尾、FTS 排名、摘要头 4000 |
| Rust `search/grep_files` | Coomi Agent | 默认整个工作区 | 每次全文扫描、正则、48000 字节输出 |
| `IndexService` | 文件搜索 API | 整个工作区 | 优先 `rg`；fallback BM25 只读 12000 字头尾 |

同一查询的可见文件、排序、摘要和“未命中”语义都不同。`retrieval_service.py` 注释还声称 FTS flag 关闭时会走旧 `IndexService`，但当前被动检索实际直接返回空块，主动 Storydex 检索则仍固定使用 FTS。

## 7. Rust 文件读取与 4000 字误解

### 7.1 模型真实读取上限不是 4000 字

Rust `read_file` 的当前行为：

- 默认从第 1 行开始读 500 行。
- `offset` 支持按行继续读取。
- `limit` 最大 2000 行。
- 工具输出上限为 48000 **字节**。
- 完整工具结果通过 `vendor/coomi-rs/engine/src/agent.rs:339` 写回模型的 tool message。

Python 的 `_CoomiEventTranslator` 在 `apps/backend/services/coomi_agent_service.py:1371` 只把工具完成事件裁成前 4000 字符，供 UI、Trace 和审计展示。自定义 Storydex 工具在 `apps/backend/services/coomi_agent_service.py:667` 将真实 output 原样回传 Rust。故“Agent 所有工具都只读前 4000 字”这个结论不成立。

### 7.2 `read_file` 仍有硬缺陷

1. 先 `tokio::fs::read_to_string()` 读取整个文件，再 `.lines().skip().take()`；读取 20 行也要分配整文件。
2. 600 行短文件默认只返回前 500 行，但没有 `hasMore`、`nextOffset` 或总行数；若未触发字节截断，模型看不到明确的“还有内容”。
3. offset 只按行工作。一个超长单行文件超过 48000 字节后，后半段不能通过增加 line offset 到达。
4. `truncate()` 直接执行 `String::truncate(48000)`。Rust 的索引是字节位置；若 48000 不在 UTF-8 字符边界会 panic。行号前缀改变字节对齐后，长中文行可以触发该风险。

### 7.3 各类固定窗口必须分开理解

| 位置 | 当前限制 | 是否影响模型事实覆盖 |
|---|---:|---|
| Wiki `_collect_sources()` | 全文 | 不受 4000 限制，但代价很高 |
| `read_concision_source_documents()` | 每文件 4000 | 当前无调用者，是遗留死代码 |
| 最近正文块 | Agent assembler 每文件尾部 700；其他调用默认仍为头部 | 是，直接影响续写上文 |
| 活动实体识别 | 活动文件总计 3000 字头尾窗口 | 是，会级联影响多层召回；中部仍不可见 |
| Fact/Relationship fallback | 活动文件开头 4000 | 是，仅在 active entities 为空时触发 |
| 角色/世界书相关性评分 | 文件头 3000/2500 | 是，影响候选选择 |
| 角色/世界书最终块 | 每文件约 520/420 | 是，但属于上下文预算后的摘要层 |
| FTS 原文索引 | 最多 120000 字头尾 | 是，中部完全不可召回 |
| FTS snippet | 与索引一致的最多 120000 字头尾窗口 | 已消除 4000 字空摘要；索引未覆盖的中部仍不可见 |
| Wiki Agent 初始 sample | 48 文件 × 420 | 是初始上下文限制，但可主动继续读 |
| Wiki 本地章节 summary/details | 开头 260 / 前 5 行 | 是，章尾事实不会自动进入条目 |
| Rust `read_file` | 默认 500 行、最多 2000 行、48000 字节 | 是，可分页但缺少完整性协议 |
| `ToolDone.result_preview` | 前 4000 字符 | 不限制模型当轮输入，只限制 UI/Trace 可见性 |
| 后端 audit preview | 前 2000 字符 | 只限制审计可见性 |

## 8. 已有优化及其边界

### 8.1 `b14e506`

该提交引入/强化了三层检索、中文 bigram/trigram、被动相关段落、Wiki reference 和两个主动检索工具；同时把被动查询收窄为活动实体和引号短语，避免指令词污染。

收益：中文检索可用、Agent 有主动查证能力、上下文不再完全依赖固定记忆块。

边界：120000 头尾、4000 摘要、全树 `watch_files()` 和窄查询均保留。

### 8.2 `8376a5e`

该提交将候选路径扩大到最多 30 个，并把被动检索块从 1000 增加到 1600 字。

收益：空摘要时至少能把候选文件路径交给 Agent。

边界：候选路径不等于实际读取；路径列表位于 snippet 之前，反而可能占满 prompt 块。

### 8.3 `68e54ed`

该提交为避免局部合并后 overview/timeline/topic/index 残留旧数据，将本地变更后的流程改成完整重建。

收益：冷重建和“增量”结果更容易保持一致。

边界：从架构上放弃了真实增量，任意变化重新处理全部源。

### 8.4 `91caf7d`

该提交在部分 Wiki 调用中复用已收集 `sources`，并给 `EntityRegistry` 增加实例内缓存。

收益：减少部分同一构建阶段的重复读盘。

边界：`_reconcile_entity_registry()` 仍自行全扫；上下文装配又创建多个 Registry/MemoryStore 实例，缓存不能跨组件复用。

### 8.5 `214f6ae`

该提交继续强化 Wiki 投影诊断、隔离、查询兼容和 Agent 交互。

收益：错误对象处理和图谱正确性更稳定。

边界：没有改变 Wiki 扫描复杂度、FTS 文件发现复杂度和 Agent 读取覆盖率。

综合判断：项目已经优化过，但优化重点是**正确性和召回补偿**，不是**增量架构和可证明的读取覆盖**。

## 9. 推荐目标架构

```text
文件保存 / 外部文件 watcher
          |
          v
Workspace Content Catalog
  path, kind, size, mtime_ns, hash, revision, deleted
          |
          +--------------------+---------------------+
          |                    |                     |
          v                    v                     v
Chunk FTS Index        Wiki Source Contributions   Context Caches
原文块+token+区间       source -> entries/nodes     chapter/entity/memory
          |                    |                     |
          +--------------------+---------------------+
                               |
                               v
                     workspaceRevision=N
                               |
                  +------------+------------+
                  |                         |
                  v                         v
          Agent Query Planner         Pure Wiki Snapshot Query
                  |
          命中 chunk + source span
                  |
          Range Reader + Evidence Ledger
                  |
          预算内上下文 / 主动继续读取
```

核心原则：

1. **查询不发现变化。** 查询只读已发布 revision；文件 watcher/保存事件负责产生 dirty set。
2. **扫描清单只做一次。** Wiki、FTS、上下文装配共享同一 Content Catalog。
3. **索引存原文块和位置。** 排名结果天然带 snippet，不再二次读文件头。
4. **全局投影不等于全量源重读。** 可以从已缓存的 source contributions 重算轻量聚合。
5. **读取必须可证明。** Agent 记录读过的 source hash、行/字节区间和未读候选。
6. **只读必须真的不改项目。** 查询缓存移出项目目录，或将缓存更新显式定义为独立系统权限；Wiki 查询本身绝不重建。

## 10. 分阶段修复方案

### 阶段 0：先修正确性和隐藏副作用

建议作为第一组小 PR，避免等待完整架构后才止血。

本轮只完成了其中不牵涉工具/记忆协议和架构迁移的局部项：第 3 项完成基础 metadata 比较、第 4 项完成事务回滚、第 5 项完成无 cursor 时的尾部窗口；其余仍是待办。

1. 将 `StorydexWikiQuery` 改成纯读取当前 last-good Wiki。不存在或 stale 时返回结构化状态，不在查询中 reconcile/rebuild。
2. 将实体 registry 归并只放到显式同步/写入流程，不放在 `read_or_build()` 入口。
3. 修复 FTS 变化判断：存储并比较 `mtime_ns + size`，保存/文件 watcher 事件直接标记 dirty；必要时对 dirty 文件计算 hash。不要再拿 mtime 与 `indexed_at` 比较。
4. `watch_files()` 的写库操作包在单事务中，使用批量语句；失败回滚到上一 revision。
5. 最近正文改为文件尾窗口；有 cursor 时改为“光标前为主、光标后为辅”的局部窗口。
6. 活动文件在只有头部摘要时不得从 FTS 排除；只有 source span 真正重叠才去重。
7. Rust `read_file` 改为流式逐行读取，返回 `startLine/endLine/totalLines/hasMore/nextOffset/contentHash`。
8. Rust 输出截断必须向下寻找 UTF-8 char boundary，不能直接按任意字节 truncate。
9. 被动检索异常写入 ContextTrace 和 warning，禁止 `except Exception: return "", []` 静默伪装成“无证据”。

### 阶段 1：共享 Content Catalog

新增一个按工作区隔离的版本化清单服务：

```text
files(
  path PRIMARY KEY,
  kind,
  size,
  mtime_ns,
  content_hash,
  revision,
  deleted
)
```

实现要求：

- Storydex 自己保存文件时直接发布精确 dirty paths。
- 外部编辑由文件 watcher 进入去抖队列。
- watcher 丢事件时，后台低频 reconciliation 修复，不在用户查询前同步全扫。
- rename 识别为 old path 删除 + new path 新增，并尽量通过 hash 关联。
- 统一 include/exclude 规则，不让 Wiki、FTS、Agent search 各维护一套。
- 每个工作区只有一个刷新任务，其他请求等待同一 future 或读取旧的 last-good revision。

### 阶段 2：全文分块检索

推荐按段落/行边界分块，而不是对整文件硬截断：

```text
chunks(
  path,
  chunk_id,
  start_line,
  end_line,
  start_byte,
  end_byte,
  raw_text,
  tokens,
  content_hash,
  source_revision,
  PRIMARY KEY(path, chunk_id)
)
```

建议初始参数：

- 每块约 1000 至 2000 个中文字符。
- 相邻块重叠 150 至 250 字，优先在段落边界切分。
- 全文件流式索引，不丢中部；极大文件用后台限速，而不是静默省略。
- 查询返回 `path + score + source span + raw snippet + source hash`。
- 原始短语命中、中文 n-gram 扩展和实体别名分别计分，再融合排序。
- candidate path 列表保留在结构化结果，不放在 snippet 之前占 prompt 预算。

### 阶段 3：真实 Wiki 增量

不要直接恢复现有 `_build_incremental_payload()`。它无法完整解决全局投影一致性。改为保存每个源的确定性贡献：

```text
source_contributions(
  source_path,
  source_hash,
  entry_fragments,
  node_fragments,
  edge_fragments,
  analyzed_revision
)
```

更新流程：

1. 只读取 dirty/new 文件，删除 removed 文件的贡献。
2. 只重算受影响 source contributions。
3. 从贡献表合并 entries/nodes/edges。
4. overview、timeline、category index 可按全部贡献做 O(条目数) 重算，但不再读取全部源正文。
5. 生成 source 反向索引时一次遍历贡献，取消 O(S×E)。
6. JSON、Markdown、index 写到同一 revision 目录，最后原子切换一个 current pointer。
7. 保留 cold rebuild，作为校验和灾难恢复路径，不作为每次变更路径。

### 阶段 4：Agent 证据覆盖协议

为需要事实核对、续写连续性、Wiki 深度生成的任务建立 Evidence Ledger：

```json
{
  "workspaceRevision": 42,
  "queries": ["星核密钥"],
  "candidateSpans": 12,
  "readSpans": [
    {"path": "chapters/001.md", "startLine": 88, "endLine": 112, "hash": "..."}
  ],
  "unreadCandidates": 3,
  "coveragePolicy": "fact_claim",
  "coverageSatisfied": true
}
```

规则建议：

- “事实不存在”类结论必须覆盖全部高置信候选，不能仅凭 top-3 空摘要。
- 续写必须覆盖活动文件尾部或光标前窗口。
- Wiki `update` 必须覆盖全部 changed sources；`generate/review` 应按章节分片并报告每片 revision。
- 模型可以选择不读低分候选，但必须把未读数量和理由保留在 Trace。
- 上下文预算优先顺序建议为：当前局部正文 > 直接命中证据 >硬设定 > 摘要 > 候选路径提示。

### 阶段 5：统一检索入口

将 Rust `search`、`StorydexProjectSearch` 和文件搜索 API 统一到一个后端检索协议：

- `mode=exact|regex|ranked`。
- 同一 Content Catalog 和排除规则。
- 同一 source span、分页、超时、取消和 `hasMore` 语义。
- Rust core search 可以保留为底层 fallback，但必须流式、受字节预算约束，并明确结果不完整。

## 11. 验收指标

### 11.1 必须满足的复杂度不变量

- 暖态 `GET /story/wiki`：0 次源目录扫描，0 个项目文件写入。
- 暖态 `query_graph`：只读已发布 Wiki revision，复杂度与源文件总字节数无关。
- 单文件保存：Wiki 和 FTS 只读取/解析该文件及必要的轻量全局贡献，不读取其他正文。
- 无变化 Agent turn：不再重复 13 次章节目录扫描。
- 无变化主动检索：不递归遍历工作区；只查询已发布索引。
- 任意长度文本：唯一词位于文件头、中、尾均可召回。
- snippet 必须包含实际命中词，并携带可继续读取的准确区间。
- Plan/只读模式：除明确声明的外部缓存外，工作区文件写入数为 0。

### 11.2 建议的本地基准门槛

绝对耗时需要按 CI 和目标硬件校准，但可以先使用以下门槛防回退：

| 场景 | 建议门槛 |
|---|---:|
| 10000 文件、无变化 Wiki 快照读取 | p95 < 50 ms，0 次 source scan |
| 10000 文件、无变化 FTS 查询 | p95 < 100 ms，不运行 watcher reconciliation |
| 单个 20 KB 文件变化后的增量索引 | p95 < 200 ms |
| 单源 Wiki contribution 更新 | p95 < 300 ms，不读取其他正文 |
| 300 章节 Agent 暖态上下文装配（不含模型） | p95 < 300 ms |
| 1 MB 文件中部唯一词检索 | 100% 命中，snippet 含词 |
| 两个并发 Wiki 刷新请求 | 只执行一次 build |

## 12. 回归测试矩阵

| 场景 | 当前覆盖 | 新断言 |
|---|---|---|
| Wiki 无变化读取 | 结果正确性有覆盖 | `_collect_sources` 调用 0 次、无写盘 |
| Wiki 单文件变化 | 冷/增量 checksum 一致有覆盖 | 只读取 dirty 文件，结果等于 cold rebuild |
| 删除/重命名 | 部分正确性覆盖 | 贡献被精确移除，未读取无关正文 |
| 并发读取/刷新 | 无 | single-flight、revision 一致、无半套投影 |
| FTS mtime 不同但早于 `indexed_at` | 已覆盖，新词命中且旧词消失 | 同 mtime、同 size 改写由 dirty event/hash 兜底 |
| 索引全量构建中途失败 | 已覆盖事务回滚 | 旧 revision 仍可查询，无半套索引 |
| 系统时钟回拨 | metadata 差异路径已覆盖 | dirty event 丢失时仍触发重建 |
| 超长文件中部词 | 无 | 中部命中并返回准确 span |
| 第 4000 字后命中 | 已覆盖 | snippet 非空且包含命中词 |
| 普通未加引号主题 prompt | 无 | Query Planner 产生内容查询词 |
| 活动实体位于章尾 | assembler 头尾识别已覆盖 | Fact、Relationship、Wiki 全链路均可召回 |
| 续写长章节 | Agent 文件尾窗口已覆盖 | 有 cursor 后使用光标局部窗口 |
| 30 个长 candidate paths | 已覆盖 | snippet 先于路径元数据且不被预算截掉 |
| Wiki 角色 registry 归并 | 已覆盖不调用 `_collect_sources()` | 暖读最终达到 0 次全量源扫描 |
| Wiki source 反向索引 | 已覆盖 entry/node 顺序与去重 | 大规模复杂度基准防回退 |
| 600 行 `read_file` | 无 | 返回 `hasMore=true` 和 `nextOffset=501` |
| 超长单行中文文件 | 无 | 可按 byte/char range 翻页，无 UTF-8 panic |
| Rust search 无命中 | 无 | 有读取字节/文件上限，可取消，不阻塞 runtime |
| Plan 模式调用两个检索工具 | 只测工具被注册 | 工作区文件 hash 完全不变 |
| 工具结果超过 4000 字 | 只测 preview 字段 | 模型收到完整受限结果；Trace 有 hash/span/truncated 元数据 |
| Wiki Agent 全量生成 | 无覆盖率门禁 | 每个 source shard 有已读 revision 或明确未读状态 |

## 13. 不建议的修复

- 只把 4000 改成更大的常数。
- 在每次查询前计算全项目 hash 来保证新鲜度。
- 只依赖 mtime，不处理保存事件、保留时间戳和 watcher 丢事件。
- 直接重新启用旧 `_build_incremental_payload()`，但不建立 source contribution 和冷构建等价测试。
- 继续让查询接口隐式修复、归并和重建数据。
- 用静默 fallback 把索引异常伪装成“没有相关内容”。
- 将完整 30 条路径放在正文 snippet 之前消耗上下文预算。
- 只在 prompt 中要求“请完整阅读”，却不记录实际 source span 覆盖率。

## 14. 推荐实施顺序

1. **PR 1：纯查询与止血修复。** Wiki query/read 与 rebuild 解耦；修 mtime 判断、事务、最近正文尾窗口、Rust UTF-8 截断和 `hasMore`。
2. **PR 2：Content Catalog。** 接入保存事件、文件 watcher、dirty queue、single-flight 和 revision。
3. **PR 3：Chunk FTS。** 全文分块、真实 snippet/source span、统一 exact/ranked 查询协议。
4. **PR 4：Wiki contributions。** 单源增量、删除/重命名、原子 revision 发布、冷构建等价测试。
5. **PR 5：Evidence Ledger。** 光标上下文、读取覆盖门禁、Wiki Agent 分片覆盖和 UI/Trace 可观测性。
6. **PR 6：删除旧路径。** 移除失活 `_build_incremental_payload()`、无调用的 4000 字 concision reader 和重复检索实现。

本轮只完成了 PR 1 中不涉及大重构的部分止血项，不代表 PR 1 已完成：Wiki query/read 仍未与 rebuild 解耦，Rust 读取协议也未修改。完成完整 PR 1 可以消除最危险的错误和只读副作用；完成 PR 2 至 PR 4 后，Wiki 与检索的渐进复杂度才算真正解决；完成 PR 5 后，才能对“Agent 已读取项目证据”作可验证声明。

## 15. 本地复现记录

以下数据是本轮修改前的本地基线，只代表当前开发机，不应直接视为跨机器 SLA。保留这些结果用于衡量后续架构优化，不再把它们描述为修改后现状：

### Wiki 压力样本

以当前源码仓库作为 Wiki 工作区执行一次 `_collect_sources()`：

```text
20.965s
2927 个来源
43,005,769 字符
```

80 章节、12 角色临时项目：

```text
initial_rebuild:       0.197s, scans=2
warm_read_or_build:    0.087s, scans=2
unchanged_sync:        0.088s, scans=2
one_file_changed_sync: 0.212s, scans=2, 完整 rebuild
graph_query:           0.088s, scans=2
```

### Agent/检索样本

```text
300 章节暖态 TurnContract，passive FTS=false:
4.264s, list_chapter_states=13, ordered_segment_paths=4, stat=42,898

2000 小文件 RetrievalService.watch_files:
首次 15.9817s，更新 2000
暖态 0.4035s，更新 0

第 5200 字后唯一词:
candidatePaths 命中，snippet 长度 0

160,006 字文件中部唯一词:
candidatePaths 为空

内容变化但恢复原 mtime:
watch updated=0，旧词仍命中，新词不命中

3609 字活动文件章尾事实:
Recent content 仅 700 字，章尾事实缺失
```

### 已运行相关测试

```text
Set-Location apps/backend
python -m pytest tests/test_story_wiki_service.py tests/test_story_wiki_helpers_comprehensive.py tests/test_memory_query_and_diagnostics.py -q

42 passed in 3.63s
```

这些修复前基线测试说明原有正确性可用，但当时没有覆盖本报告列出的扫描次数、复杂度、章尾/中部召回、时间戳回拨、并发刷新和读取覆盖率；本轮新增覆盖见第 16.3 节。

## 16. 本轮小更新结果（2026-08-04）

### 16.1 已完成

| 范围 | 实现 | 直接效果 |
|---|---|---|
| Wiki 实体 registry | 新增 `_collect_character_sources()`，只扫描 `.storydex/characters/`、兼容根级 `characters/` 和各自 `cards/` 的直接文件 | registry 归并不再为了找角色卡读取章节、世界书和其他项目源 |
| Wiki source index | 预建 source/entry/node 反向映射 | 去掉 `_build_index()` 的 source × entries/nodes 二次遍历，保留输出顺序 |
| 最近正文 | `list_recent_segments()` 新增默认关闭的 `read_from_tail`；仅 assembler 启用 | 续写上下文读取最近正文尾部 700 字，其他调用行为不变 |
| 活动实体 | 活动文件改用 3000 字头尾窗口 | 常见章尾角色可触发 Fact/Relationship/Wiki/FTS 的后续召回链 |
| 被动检索预算 | snippet 先输出，candidate paths 后输出 | 30 个长路径不再优先挤掉真正证据正文 |
| FTS 判脏 | 比较已存 `(mtime, size)` 与当前值，不再比较 `mtime <= indexed_at` | mtime 回拨或早于索引时间但 metadata 已变化时能够重建 |
| FTS 写入 | `build_index()`、`watch_files()` 使用显式单事务，异常回滚；无变化 watch 不获取写锁 | 消除逐条 autocommit；失败不会留下被清空或半更新的索引 |
| FTS 摘要 | 复用 FTS 的 120000 字受限头尾源窗口生成 snippet | 第 4000 字后已召回命中能够返回非空真实摘要，尾部命中不会顺序扫描整个超大文件 |

### 16.2 明确未解决

- Wiki 暖读仍执行一轮全量 `_collect_sources()`；任意变化仍完整 `rebuild()`。
- `StorydexWikiQuery -> query_graph() -> read_or_build()` 仍可能归并、重建和写盘，只读语义尚未修复。
- Agent TurnContract 仍重复调用章节遍历，300 章节的 42,898 次 `stat` 根因未处理。
- `watch_files()` 仍递归扫描全部候选文件；相同 mtime 且相同 size 的内容改写仍可能漏掉。
- FTS 仍只索引超长文件的 120000 字头尾，中部事实依然没有 candidate path。
- 请求仍没有 cursor/选区/未保存 buffer revision；3000 字头尾窗口无法覆盖长文件中部。
- Fact/Relationship fallback、Item 相关性等其他读取窗口没有调整，避免把本轮扩大成记忆系统重构。
- 普通未加引号主题 prompt 仍可能不产生被动查询词；检索异常仍会被静默降为空块。
- Rust `read_file`、`search/grep_files`、工具结果协议、Trace 预览和 Evidence Ledger 均未修改。

### 16.3 新增验证

聚焦测试：

```text
python -m pytest tests/test_storydex_runtime_fixes.py tests/test_story_wiki_helpers_comprehensive.py -q

21 passed in 2.06s
```

新增断言覆盖：Wiki registry 不调用全量 source scan、source 反向索引顺序、4000 字后摘要、mtime 回拨判脏、全量索引失败回滚、Agent 尾部正文、章尾实体识别，以及长 candidate path 列表的预算顺序。

组合回归：

```text
python -m pytest tests/test_story_wiki_service.py tests/test_story_wiki_helpers_comprehensive.py tests/test_memory_query_and_diagnostics.py tests/test_context_policy.py tests/test_storydex_runtime_fixes.py -q

67 passed in 5.16s
```
