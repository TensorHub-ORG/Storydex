# 图谱稳定性与自然写作指导（2026-10-29）

本轮借鉴两个项目的设计，保留 Storydex 的文件工作区、知识图谱和预设体系。主要实现持久来源身份、按引文维护审核，以及可分别开关的写作指导。没有引入新的写作 Agent 工作流、质量 Judge、正文识别器或强制完成门禁。

## 参考来源与取舍

固定版本阅读源码，未执行第三方项目脚本，也未复制生成器的实现代码。

| 来源 | 核对位置 | 借鉴与边界 |
| --- | --- | --- |
| AI-Novel-Writing-Assistant，`7f06e54b4fa10517497877f7a69e52be7b346ef3` | [角色关系数据模型](https://github.com/ExplosiveCoderflome/AI-Novel-Writing-Assistant/blob/7f06e54b4fa10517497877f7a69e52be7b346ef3/server/src/prisma/schema.prisma)、[关系阶段事务](https://github.com/ExplosiveCoderflome/AI-Novel-Writing-Assistant/blob/7f06e54b4fa10517497877f7a69e52be7b346ef3/server/src/services/novel/dynamics/CharacterDynamicsMutationService.ts)、[图模型](https://github.com/ExplosiveCoderflome/AI-Novel-Writing-Assistant/blob/7f06e54b4fa10517497877f7a69e52be7b346ef3/client/src/pages/novels/components/characterWorkspace/characterRelationshipGraphModel.ts) | 角色 ID、端点外键、结构化关系和事务能减少存储层错接。模型仍可能遗漏角色或抽错关系，不能据此保证图谱事实正确。Storydex 保留关系方向、多谓词及各自证据，不采用同角色 pair 合并覆盖、主角描述补边和关键词猜测紧张程度。 |
| oh-story-claudecode，`70d1cf426f8cbe3d374e2b06e4c2cbdbd986da06` | [story-deslop](https://github.com/zenstory-ai/oh-story-claudecode/blob/70d1cf426f8cbe3d374e2b06e4c2cbdbd986da06/skills/story-deslop/SKILL.md)、[表达](https://github.com/zenstory-ai/oh-story-claudecode/blob/70d1cf426f8cbe3d374e2b06e4c2cbdbd986da06/skills/story-deslop/references/expression.md)、[叙事详略](https://github.com/zenstory-ai/oh-story-claudecode/blob/70d1cf426f8cbe3d374e2b06e4c2cbdbd986da06/skills/story-deslop/references/narrative-focus.md)、[文风优先级](https://github.com/zenstory-ai/oh-story-claudecode/blob/70d1cf426f8cbe3d374e2b06e4c2cbdbd986da06/skills/story-deslop/references/style-resolution.md) | 借鉴表达、详略、因果分别处理和作者要求优先。自行编写三组中文建议，不照搬禁句、标点黑名单、多 Agent 审稿与脚本门禁。规则零命中不能证明文学质量。 |

生成器的 LICENSE 为带项目说明的 AGPL-3.0 文本，oh-story 使用 MIT。本轮是设计参考和独立实现；原始源码留在忽略的 `output/references/`，不随提交分发。

## 图谱如何保持稳定

- 新增 `.storydex/knowledge/source-identities.json`，保存来源 ID、当前路径、历史路径、缺失状态及关系引文的来源绑定。它是项目数据，不能与 `.storydex/wiki/` 缓存一起清除。旧项目首次升级沿用原有节点 ID。
- Storydex 文件树中的文件/目录移动携带身份；Agent `apply_patch` 的 `Move to` 同样携带身份，支持同一次补丁改名并修改内容。补丁数据和身份记录共同成功或回滚。
- 外部编辑器或 shell 改名，在旧来源缺失、新来源内容唯一匹配时自动接续。多个同内容文件、同时改名又改写等无法确定的情况不猜测；旧关系保留待复核。直接在原路径替换文件仍视为该来源的编辑。
- 旧路径被新文件占用时，已绑定的关系引文继续指向原来源。重建 WIKI 缓存不会丢失来源身份与绑定。
- 审核指纹覆盖候选内容、来源 ID、具体引文及匹配状态。补写其他段落、改名不会单独撤销审核；修改谓词、引文、端点或删除引文会触发相应复核。引用仍输出当前路径、全文 SHA 和匹配状态。
- 旧审核在候选和引文仍一致时迁移为新指纹，审核账本、身份记录、投影与最终来源快照一起写入。迁移后无变化同步不会空增版本。
- 保留不同方向和不同谓词的关系。存储稳定性不等于语义真实性：引用外的上下文变化可能改变句子含义，仍需要作者或 Agent 复核。
- UI 在节点/边结构未变时复用布局；刷新保留存续节点的拖拽位置、缩放、平移和选择。切换项目重新建立图谱视图，用户仍可手动重置布局。

图谱持久身份这部分位于 Rust PC 运行时。Python 后端本轮只补齐共享预设格式和编译行为，未迁移其图谱存储。

## 如何使用自然写作指导

预设管理新增“自然写作”入口，创建可编辑的未启用预设；再次点击打开已有版本，不覆盖作者修改。用户检查内容后可启用为主预设，也可直接在原有预设参数中开启“自然写作指导”，保留原预设的文风和模块。

三组建议可分别开启、关闭、编辑，旧预设默认关闭：

1. **自然表达与人物声音**：减少无信息增量的总结、重复情绪注释和机械句式，保留必要的长句、心理描写和叙述者声音，让对话体现人物身份与意图。
2. **场景详略与叙事节奏**：把笔墨给选择、交锋、代价和变化，普通步骤可以概述，不为扩写凭空增加动作或冲突。
3. **因果连贯与信息边界**：行动接得上动机和已知信息，区分作者、读者与视角人物掌握的信息，润色不擅自改写事实和因果。

三组指导都明确限定用于小说正文创作、续写和润色，当前作者要求与本书文风优先。提示词存于预设 `guidanceModules`（磁盘 `guidance_modules`），与原有结构化/导入模块叠加；Rust 与 Python 编译器均支持。普通 Agent 和显式章节生成沿用现有预设注入链路，无额外模型调用。

这是选择写作倾向的功能，未宣称能让任意模型达到某种文学水平，也没有用禁词命中数作质量分数。

## 本地验证

- Rust 知识投影：10 项，含文件/目录移动、外部改名、重复内容不误合并、旧路径复用、缓存重建、引文变化及旧审核迁移。
- Agent 补丁：3 项，含改名并改稿后保留图谱身份。
- Rust 预设：7 项，覆盖结构化/导入预设叠加、开关、运行时禁用和磁盘往返。
- 前端：自然写作、StoryStatePanel、componentHandlers 共 28 项；图谱视图追加调整后重跑其 17 项。类型检查通过。
- Python：自然写作编译、运行时预设、Markdown 同步共 4 项。本机使用 Python 3.13.0，与普通组件 CI 的 3.13 同系列；本机缺少 watchdog，在忽略的 `output/.venv-writing-guidance/` 安装仓库指定的 6.0.0，未改全局环境。
- 静态检查按修改范围执行 services、tools、agentd 的 Clippy，以及格式和差异检查。tools 既有测试的两处 `unwrap()` 改为带说明的 `expect()`，无运行时行为变化。

本轮未运行 Playwright/E2E、未调用真实写作模型、未打包或重启现有服务。自然写作的实际效果，以及真实模型抽取的关系准确率，仍需作者在代表性章节上试用比较。
