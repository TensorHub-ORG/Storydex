# Storydex 安卓端 Agent 提示词与剧情片段体系（完整整理）

> 来源代码：`apps/android-frontend/src/story/prompt.ts`、`stores/story.ts`、`stores/session.ts`、`components/EmptyState.vue`、`components/SideDrawer.vue`、`components/Composer.vue`、`views/SettingsView.vue`。
> 适用版本：Storydex Android 0.1.x（基于 Coomi-Android 移植）。
> 整理日期：2026-08-10。

---

## 0. 模式总览

安卓端 Agent 有 **两条模式轴**，用户在空态首屏（EmptyState）上分别切换：

| 轴 | 取值 | 显示名 | 说明 |
|---|---|---|---|
| AgentMode（角色） | `story` | 剧情 | 沉浸式推进剧情，拒绝明确 OOC 与越权操控 |
| | `narrator` | 旁白 | 作为故事内的系统面板，只解说、不续写 |
| | `agent` | Agent | 不受剧情约束的完整 Coomi Agent |
| NarrativeMode（剧情控制强度，仅 story/narrator 显示） | `immersive` | 沉浸 | 以玩家角色为本，严格遵循既有设定 |
| | `narrative` | 叙事 | 以引导者视角维护设定 |
| | `free` | 自由 | 允许玩家以造物主姿态重塑世界 |

- 切换 `agentMode` 时：`setAgentMode(mode)`，并同步 `session.setPermissionMode(key === 'agent' ? 'full' : 'auto')` —— Agent 模式权限为 full，剧情/旁白为 auto。
- 空态建议（`MODE_SUGGESTIONS`）：
  - story：回顾我现在的处境 / 观察眼前的人 / 检查周围环境 / 迈出下一步（**若已有剧情片段，优先用最新片段的动态行动建议**）
  - narrator：详细总结当前主角详细面板 / 总结当前故事背景现状 / 总结当前与主角相关的人物 / 总结当前的完整精炼剧情线
  - agent：我需要创建一个故事设定 / 我需要整理故事文件 / 我需要制作角色设定 / 我需要制作风格预设
- 空态标题（`TITLES`）：story=「剧情可以怎么发展？」；narrator=「想了解故事的哪一面？」；agent=「有什么想为你的故事世界做的？」

---

## 1. 提示词构造总入口

文件：`apps/android-frontend/src/story/prompt.ts`

所有用户输入在发送前都会被包装：

```
session.sendMessage(text)
  → story.promptFor(text)      // stores/story.ts:194
    → buildStoryPrompt({ agentMode, narrativeMode, fragmentMin, fragmentMax, playerText, actionsMarker })
      → 返回最终提示词字符串
  → transport.send({ command: 'send_message', text: agentPrompt })   // session.ts:258
```

- `playerText` = 用户原始输入。
- `actionsMarker` = `'[STORYDEX_ACTIONS]'`（常量 `ACTIONS_MARKER`，stores/story.ts:27）。
- `fragmentMin/fragmentMax` = 每段目标字数（默认 1000–2000，可在设置页 200–8000 调整）。

---

## 2. Agent 模式提示词（完整原文）

当 `agentMode === 'agent'` 时，`buildStoryPrompt` 返回：

```
[Storydex 故事创作 Agent]
你是帮助用户创建和制作角色扮演文字冒险游戏的助手。
你当前工作在该游戏的故事项目目录中。动手前必须先了解这个项目的架构与目录约定：
- chapters/：剧情章节正文（按时间分组的片段文件）；
- .storydex/characters/：角色设定；
- .storydex/worldbook/：世界观设定；
- .storydex/wiki/：百科与资料条目。
必须严格沿用项目既有架构与约定来组织内容；禁止随意创建新的目录结构或改动既有约定。需要了解现状时，先使用读取/搜索工具查看项目目录。

用户指令：${playerText}
```

> 这是安卓端 Agent **唯一**的目录架构说明来源。若 Agent 对目录架构理解失败，问题就出在这里：只有一份简略清单（chapters/ + 三个 .storydex 子目录），没有给出文件命名规则、frontmatter 格式、片段如何分组（`chapters/<group>/<seq>.md`）等生成侧约定。

---

## 3. 剧情 / 旁白模式提示词（完整原文）

`freedom` 变量按 `narrativeMode` 三选一：

| mode | 提示词片段（原文） |
|---|---|
| immersive | `沉浸：以玩家角色为本，严格遵循既有设定；拒绝玩家直接控制 NPC、世界事实或预先指定必然结果。` |
| narrative | `叙事：以引导者视角维护设定，可通过合理事件、线索与代价引导剧情走向。` |
| free | `自由：允许玩家以造物主姿态大胆重塑世界，但必须交代变化的因果并保持后续可读。` |

### 3.1 旁白模式（`agentMode === 'narrator'`）

```
[Storydex 剧情旁白模式]
你是故事中的系统面板。先判断输入是否 OOC；只解说当前设定、角色状态、因果、风险和可选行动，不续写小说正文，不替玩家行动。
${freedom}

上下文要求：优先使用引擎附带的故事项目资料；不足时只在当前故事项目内读取文件。

玩家输入：${playerText}
```

### 3.2 剧情模式（`agentMode === 'story'`）

```
[Storydex 剧情模式]
先判断玩家是否仍在剧情内行动，以及是否试图越权掌控 NPC、世界事实或后续必然结果。明确 OOC 或越权时，可以简短拒绝并给出合规替代行动；否则只输出沉浸式小说正文，不解释规则，不暴露 Agent 身份。
${freedom}

${WRITING_RULES}

[本轮篇幅]
目标约 ${fragmentMin}-${fragmentMax} 个中文字符（不计空白与动作建议标记）。这是软目标：完整性、连续性和自然收束优先，禁止填充、重复、报字数或生硬截断。

每轮只生成一个完整剧情片段。剧情正文之后必须严格追加以下结构，并给出四个与本轮情境直接相关、彼此不同的玩家行动；此结构不属于剧情正文：
${actionsMarker}
- 行动一
- 行动二
- 行动三
- 行动四
如果本轮是在拒绝 OOC 或越权请求，不得输出 ${actionsMarker}，该回复不会归档为剧情片段。

玩家行动：${playerText}
```

### 3.3 通用写作规则（`WRITING_RULES`，剧情模式内嵌）

```
[Storydex 移动端写作规则]
- 连续性优先：先核对最近剧情、角色状态、地点、时间、已知事实与未解决冲突，再推进本轮。
- 项目资料优先：引擎会附带最近正文、较早片段摘要、角色、世界观与 WIKI。若信息仍不足，先使用读取/搜索工具检查当前故事项目；禁止访问或修改项目之外的内容。
- 角色一致性：角色只能依据自身知识、动机、能力和处境行动；对话要区分声线，情绪变化必须有可见原因。
- 场景推进：每轮至少推动行动、关系、信息或风险中的一项，保留可供玩家决策的空间，不替玩家决定其角色的思想与最终选择。
- 小说表达：用具体动作、感官、环境反应、对话和必要的内心活动呈现，不写规则说明、创作分析、章节总结或元叙事。
- 因果与节奏：先完成本轮核心事件，再自然收束；删除重复解释和空泛抒情，不为凑字数灌水，也不因长度目标截断正在发生的关键动作。
- 设定冲突：项目文件与模型记忆冲突时，以项目文件为准；无法可靠判断时保持克制，并通过剧情中的可观察信息消解歧义。
```

---

## 4. 剧情片段的生成与解析

### 4.1 行动标记协议（`[STORYDEX_ACTIONS]`）

剧情模式下模型输出必须含两部分，用 `ACTIONS_MARKER = '[STORYDEX_ACTIONS]'` 分隔：

```
<剧情正文>
[STORYDEX_ACTIONS]
- 行动一
- 行动二
- 行动三
- 行动四
```

解析函数 `parseStoryResponse(raw)`（stores/story.ts:85）：

- 用 `raw.lastIndexOf(ACTIONS_MARKER)` 找到**最后一个**标记；
- `content` = 标记之前全部正文（trim）；
- `suggestions` = 标记之后按行切分，去掉 `- * • 1. 1) 1、` 前缀，过滤空行，**只取前 4 条**；
- 校验：`content` 非空 **且** suggestions 恰好 4 条 → 返回 `{ content, suggestions }`；否则返回 `null`（该轮不归档）。

### 4.2 片段何时归档（captureTurn，stores/story.ts:217）

`turn_end` 事件触发 `story.captureTurn(timeline, sessionId)`（session.ts:200-202），归档条件（全部满足才写入）：

1. `agentMode === 'story'`（**旁白/Agent 模式不归档**）；
2. 存在非空的 assistant 消息；
3. `latest.sourceMessageId !== assistant.id`（避免同一轮重复归档）；
4. `parseStoryResponse` 成功（含 4 条行动建议）；
5. `latest.content !== parsed.content`（内容有变化才归档）。

### 4.3 片段文件命名与分组（captureTurn 内）

- 每 **5 段** 开一个新分组（`fragments.length % 5 === 0`）；
- 分组名 = 时间戳 `yyyyMMddHHmm`（`timestamp()`），若与最新组或已有组冲突则 +60s 重试（`nextGroupTimestamp`）；
- 组内序号 = 组内片段数 + 1，补零 3 位（`001`）；
- 文件名 = `<group>-<seq>.md`；
- 相对路径 = `chapters/<group>/<filename>`（`relativePath`）；
- 片段内容带 frontmatter：

```
---
summary: "<50 字摘要>"
createdAt: "<ISO 时间>"
---

<正文>
```

- `summary` = 正文去掉 markdown 符号与空白后的前 50 字（`shortSummary`）。

### 4.4 片段写入磁盘

`writeStoryFragment` → `POST /api/sessions/{sessionId}/story-fragment`，body `{ path, content }`。有项目路径（`projectPath`）时每次 captureTurn 都会同步写入；编辑/补同步也走同一接口。

---

## 5. 剧情片段的组织与显示逻辑

### 5.1 store 侧排序（stores/story.ts:168-170）

```
fragments  = 数组按生成顺序追加（时间升序，尾部为最新）
latest     = fragments[length - 1]          // 最新一段
latestFive = fragments.slice(-5).reverse()  // 最近 5 段，最新在前
older      = fragments.slice(0, -5).reverse() // 更早的段，最新在前
```

### 5.2 侧边栏抽屉显示（SideDrawer.vue，story/narrator 模式）

布局自上而下：

1. **头部**：标题「剧情片段」+ 当前项目名（`projectPath.split('/').pop()`，空则「默认故事」）+ 设置入口；
2. **操作区**：`继续故事`（continueStory）与 `待机`（standby）两个按钮；
3. **列表**：
   - 空 → 提示「剧情尚未开始。完成第一轮行动后，剧情片段会按顺序收进这里。」
   - 「最近五条」小节：`latestFive`（倒序，最新在上），每项显示 `filename` + `summary`，点击进入编辑弹层；
   - 「更早的剧情片段（N）」折叠项：展开显示 `older`（倒序），同样可点击编辑；
4. **底部**：「返回控制台」按钮。

### 5.3 编辑弹层

- 点击片段 → 底部弹出 sheet，`textarea` 预填该片段 `content`；
- `保存修改` → `story.updateFragment(id, 新内容, sessionId)`：重新算 summary、重写 frontmatter、`POST` 写盘、更新本地数组。

### 5.4 继续故事 / 待机（session.ts:293-309）

- `continueStory()`：清空当前流，把 `story.latest` 的正文作为一条 assistant 消息填回时间线（供用户看到最新一段再接着输入）；
- `standby()`：清空时间线回到剧情主页（空态首屏），不换会话、不中断后台任务。

### 5.5 从项目目录补载（loadFragmentsFromProject，stores/story.ts:286）

适用「从外部导入的已有数据项目」：

- 读取 `window.CoomiAndroid.getStoryProjectPath()`；项目切换时重置本地片段；
- 本地已有片段（`fragments.length > 0`）→ 跳过，不覆盖本地编辑；
- 本地为空 → 递归扫描 `chapters/**/*.md`（`/api/fs/list` 逐层展开，只收 `.md`）：
  - 解析 frontmatter 的 `summary` / `createdAt`，无 frontmatter 或解析失败则 `createdAt=0`、summary 取正文前 50 字；
  - **按 `createdAt` 升序排序**（避免 `chapter2/chapter10` 字典序错乱）；
  - 有竞态保护：等待期间若已有新片段生成则放弃覆盖。
- 补载的片段 `suggestions: []`（外部项目没有行动建议）。

---

## 6. 所有提示词/文案清单（汇总表）

| 位置 | 内容 | 用途 |
|---|---|---|
| prompt.ts `buildStoryPrompt` agent 分支 | 「Storydex 故事创作 Agent」全文（§2） | Agent 模式系统提示 |
| prompt.ts `freedom` 三值 | 沉浸/叙事/自由（§3 表） | 剧情控制强度 |
| prompt.ts narrator 分支 | 「Storydex 剧情旁白模式」全文（§3.1） | 旁白模式系统提示 |
| prompt.ts story 分支 | 「Storydex 剧情模式」全文（§3.2） | 剧情模式系统提示 |
| prompt.ts `WRITING_RULES` | 7 条移动端写作规则（§3.3） | 剧情模式通用约束 |
| prompt.ts story 分支 `[本轮篇幅]` | 目标字数 + 禁止灌水/截断 | 长度控制 |
| prompt.ts story 分支 actions 结构 | `[STORYDEX_ACTIONS]` + 4 条行动 | 行动建议协议 |
| EmptyState.vue `MODES` | 剧情/旁白/Agent 三段描述（§0 表） | 模式切换 UI |
| EmptyState.vue `FREEDOM` | 沉浸/叙事/自由 | 强度切换 UI |
| EmptyState.vue `MODE_SUGGESTIONS` | 三组各 4 条建议（§0） | 空态建议卡 |
| EmptyState.vue `TITLES`/`SUBTITLES`/`hint` | 各模式标题/副标题/说明 | 空态文案 |
| Composer.vue `agentLabels`/`narrativeLabels` | 剧情/旁白/Agent、沉浸/叙事/自由 | 输入框标签 |
| session.ts `GUIDE_TITLES` | newbie=「Storydex 新手使用指南」/ extension=「自定义拓展进化指南」 | `send_guide` 内置引导标题 |
| SettingsView.vue | 剧情控制、剧情片段字数（min/max）、reasoningEffort 六档 | 设置文案 |
| SettingsView.vue `STORY_MODES` | 剧情模式=「沉浸推进剧情并识别 OOC」/ 剧情旁白=「只解说故事状态，不续写正文」/ 独立 Agent=「完整且不受剧情约束的 Coomi Agent」 | 设置页模式选择 |
| SettingsView.vue `NARRATIVE_MODES` | 沉浸=「以角色为本，严格遵循设定」/ 叙事=「以引导者视角，合理引导走向」/ 自由=「以造物主姿态，大胆重塑世界」 | 设置页强度选择 |
| SettingsView.vue `REASONING_EFFORTS` | auto=自动（按模型能力自动选择）/ low=低（响应更快，适合简单推进）/ medium=中（速度与分析深度平衡）/ high=高（默认，适合连续剧情与设定核对）/ xhigh=超高（复杂伏笔、多人关系与长上下文）/ max=最大（使用模型可提供的最高推理档位） | 设置页推理档位 |
| SideDrawer.vue | 「继续故事」「待机」「最近五条」「更早的剧情片段」等 | 抽屉 UI 文案 |

---

## 7. 目录架构与 Agent 理解问题的关系（诊断）

安卓端目录约束（参见项目记忆 storydex-story-project-scope）：

- 故事项目根 = `filesDir/stories/<子目录>`；
- 目录约定：`chapters/`（剧情章节正文，按时间分组的片段文件）、`.storydex/characters/`、`.storydex/worldbook/`、`.storydex/wiki/`；
- 本 app 生成片段写 `chapters/<yyyymmddHHmm>/<seq>.md`，frontmatter 含 `summary`/`createdAt`。

**Agent 对目录架构理解失败的可能缺口**（当前 `agent` 模式提示词只有 §2 那一段简略清单）：

1. 未说明 `chapters/` 内部的**分组目录命名规则**（`<时间戳>/`）与文件命名（`<时间戳>-<seq>.md`）；
2. 未说明片段文件的 **frontmatter 格式**（`summary`/`createdAt`）与正文后 `[STORYDEX_ACTIONS]` 协议；
3. 未说明「每 5 段开新分组」的约定；
4. 未给出「先 `read_file`/`search` 探查目录再动手」的强约束（只有一句「需要了解现状时」）。

> 若要让 Agent 真正理解并正确沿用目录架构，建议在 `prompt.ts` 的 agent 分支中补充上述生成侧约定（或引导其读取 `docs/` 内的一份架构说明文档）。
