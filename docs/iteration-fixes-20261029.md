# 普通聊天、章节索引与图谱修复（2026-10-29）

已完成代码修改和本地聚焦验证，并以 `af8d487071f6549c3712624b506d1aeb536ae9c3` 推送到 `dev/windows`，通过 [Development CI](https://github.com/TensorHub-ORG/Storydex/actions/runs/40736763517) 后以同 SHA 进入 `main`。[main CI](https://github.com/TensorHub-ORG/Storydex/actions/runs/40738431118) 成功复用 Windows 检查。此前实测报告保留为修复前证据；本轮没有运行 Playwright/E2E、调用真实写作模型或重启现有服务。

## 本轮改动

### 普通聊天流式请求

- 补齐 `upstream stream closed before a terminal event` 的重试分类，复用既有最多 3 次请求尝试。重试只发生在当前模型响应尚未交付给工具执行层时，保留此前工具结果；丢弃失败尝试中的工具调用，并通知前端撤回该次部分回复。
- 正确处理 `response.incomplete`；仅 `max_output_tokens` 导致的截断扩大预算后重试，内容过滤等其他不完整结束保持失败状态，不交付部分工具参数。
- 解析 `response.failed` 的 `response.error`，保留实际错误信息；按服务端错误码区分临时故障和不可重试错误。
- 普通聊天初始推理档位由 `high` 改为 `auto`。自动意味着不发送显式推理档位，由供应商决定；作者显式选择的档位仍保留。
- engine 与 HTTP provider 共用 16384 的默认输出上限，修正原先 engine 的 8192 与 provider 的 16384 不一致。已有供应商配置中显式填写的上限不会被覆盖，因此已保存的 8192 配置仍会发送 8192。

没有调整现有 45 秒响应头/首字节、60 秒流停顿、180 秒读取超时。原始证据包含上游主动报错，不能把它视为客户端超时，也不能认定增加超时或输出预算就能消除故障。

协议依据：[OpenAI Responses streaming events](https://developers.openai.com/api/reference/resources/responses/streaming-events/)。本轮通过 smart-search-cli 搜索并实际抓取官方页面，核对 `response.completed`、`response.incomplete`、`response.failed` 及嵌套错误字段。检索命令：

```powershell
smart-search search 'site:developers.openai.com responses streaming response.incomplete response.failed' --timeout 45 --format json
smart-search fetch 'https://developers.openai.com/api/reference/resources/responses/streaming-events/' --format content
```

**验证边界：应用侧的处理缺口已修正，原供应商的 3000 字长章是否能稳定完成仍需真实复测。**

### 章节列表与文件树

- 新增共用章节规则：支持 `chapters/` 直属目录以及 `.md`、`.txt` 扁平正文；README、index、summary、outline、目录、说明、章纲、大纲和隐藏项不计为章节。
- 支持中文章号和前置阿拉伯数字排序，包括“第一章/第二章/第十章”、`001.md`、`Chapter 20`。不再把标题中间的年份误作章号；无章号条目排序稳定，接在已编号章节之后。
- 章节接口、文件树和 WIKI 章节顺序复用同一比较函数。文件树仍保留说明文档，方便正常编辑，但不提供章节完成操作。
- 完成状态接口接受上述两种章节布局，继续只写进度记录、不改正文；不接受说明文件或章节目录内的单个片段作为章节。
- 文件树返回 `isChapter`，前端直接依据这一标记显示复选框，移除“只有目录才是章节”的前端判断。

### 图谱与展示

- 图谱新增并默认打开“全部”视图，显示跨角色、剧情、设定分类的关系；保留各分类视图和邻居展开。统计文案明确为“当前范围节点”。
- 关系审核和生成的 WIKI Markdown 使用已解析的条目名称；缺失端点显示“缺失条目”，不把 `source:` ID 当成名称。
- `characters/` 下“人物关系”“角色关系”“关系表”“关系总表”、`relations`、`relationships`、`index` 等约定总表名称按文档处理，保持原路径与节点 ID。这是文件命名分类规则，不分析正文来识别人名或文档语义。
- 仅在正文、谓词或引文中提及的人物仍需独立角色卡，才能成为角色节点。没有自动抽取或新建柳素贞等人物；角色视图说明了这个边界。
- 分类投影版本升级，下一次同步自动重建旧图谱；保留证据未变的审核决定及指纹。单纯恢复丢失的图谱缓存不额外增加知识版本。
- 文件变更浮条不再显示没有增删量的 `+0 -0`，仍保留实际变更文件数量。

## 本地验证

分批执行直接相关测试，失败项修正后只重跑相关范围：

| 范围 | 结果 |
| --- | --- |
| provider 解析/请求测试 | 59 项通过，包含 5 项新增本地 HTTP/SSE 测试 |
| engine Agent 与上下文 | 19 项通过 |
| 共用章节规则与 knowledge | 7 项通过，另对投影升级保留审核指纹追加验证通过 |
| agentd 章节、文件树及 WIKI 接口 | 7 项通过 |
| AgentPanel、agentStore、StoryStatePanel、explorerInteractions、componentHandlers | 共 106 项通过，分批运行 |
| 前端 `vue-tsc --noEmit` | 通过 |
| engine/services/agentd/bridge 的 Clippy `--all-targets -- -D warnings` | 通过 |
| Rust 格式、`git diff --check` | 通过 |

HTTP/SSE 测试使用本地监听器发送协议报文，没有供应商网络请求。没有运行 Tauri 外壳、打包、跨平台或全组件套件。

## 给 pi 的小范围复测建议

1. 使用新源码编译并重启 agentd/bridge，刷新前端；确认实际运行的新二进制。仅刷新 Vite 不会更新 Rust 进程。
2. 普通聊天入口执行一次原 3000 字章节请求，记录初始推理档位、实际上游推理字段、输出上限、流事件和尝试次数。若再次断流，应看到有限重试及最终真实结果；核对旧工具操作未重放、部分工具参数未落盘。已有显式 8192 配置与新的默认值应分别记录，不能混为一谈。
3. 同时保留中文命名的扁平 Markdown、TXT 章节和章节目录。核对列表/文件树顺序、说明文件排除、两类章节完成勾选以及重新打开后的持久化。
4. 同步原“青梧照相馆”图谱，核对“人物关系”成为文档、3 张独立角色卡仍为角色、原有 10 条审核记录保留。比较“全部”与“角色”视图，核对跨分类关系可见、审核标题显示名称。

历史另列的计划模式退出定义、角色冲突裁定/故事回档/撤销回档旧接口、原生窗口与多窗口并发、长篇内容一致性仍未在本轮处理。
