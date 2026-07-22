from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from uuid import uuid4

from core.bounded_text_io import read_text_limited as read_bounded_text_limited
from core.bounded_text_io import read_text_preview as read_bounded_text_preview
from core.bounded_text_io import read_text_tail as read_bounded_text_tail
from core.config import get_settings
from core.exceptions import StorydexError
from services.preset_schema import (
    PresetDocument,
    find_sidecar_path,
    load_preset_sidecar as _load_preset_sidecar_impl,
    summarize_preset_sidecar,
    write_preset_sidecar as _write_preset_sidecar_impl,
)
from services.storydex_manifest import ensure_manifest as ensure_storydex_manifest

_TEXT_SEGMENT_SUFFIXES = {".md", ".txt"}
_INVALID_FILE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_CHAPTER_NUMBER_RE = re.compile(r"^(?:第\s*)?([0-9一二三四五六七八九十百千两零〇]+)\s*章")
_NUMERIC_SEGMENT_RE = re.compile(r"^(?P<number>\d{3,4})$")
_SEG_STYLE_RE = re.compile(r"^seg-(?P<number>\d{4})$", re.IGNORECASE)
_PREFIX_NUMBER_RE = re.compile(r"^(?P<prefix>.*?)(?P<number>\d{2,4})$")
_SCRIPT_CONTEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml"}
_RUNTIME_PRESET_TEXT_SUFFIXES = {".md", ".txt"}
_RUNTIME_PRESET_JSON_SUFFIX = ".safe.json"
_PRESET_COMPILE_LOGGER = logging.getLogger("storydex.preset_compile")
# 外部导入预设编译后动辄上万字，运行时给它专用的大预算（Storydex 自有
# 预设仍走 720/2400 的紧凑预算）。
_ST_RUNTIME_PRESET_MAX_CHARS_PER_FILE = 24_000
_ST_RUNTIME_PRESET_TOTAL_CHARS = 26_000
_CHARACTER_CONSTRAINT_JSON_READ_LIMIT = 80_000
_TEMPLATE_CONTEXT_READ_CHARS = 12_000
_PRESET_LIBRARY_DIRS = {"library", "imported", "blocked"}
_TERM_RE = re.compile(r"[A-Za-z0-9_]{3,}|[\u4e00-\u9fff]{2,}")
_REWRITE_CHAPTER_RE = re.compile(r"(?:重写|rewrite)[^\d一二三四五六七八九十百千万两零〇]{0,12}(?:第)?([0-9一二三四五六七八九十百千万两零〇]+)\s*章", re.IGNORECASE)
_UNKNOWN_CHARACTER_FIELD_VALUE = "未知"

_SYSTEM_RULE_TEMPLATE = """# Storydex 项目规则

1. 正文创作必须统一使用标准中文双引号“”包裹对话、引语与其他需要加引号的正文内容，严禁使用「」、『』、英文半角双引号或其他替代引号样式。
2. 剧情模式只在 `chapters/` 下写入正文片段。
3. 变量思考优先使用可读 Markdown 记录，不要求输出固定 JSON 条目；机器可合并的变量操作只是可选层。
4. 变量思考应覆盖人物、时间、天气、地点、装备、情绪、关系、事件推进等变化。
5. 若信息证据不足，必须先读取更多上下文，禁止臆造硬设定。

格式范本：
暮色压在河堤上，风里带着未散的雨腥。江照把伞骨往上抬了抬，低声道：“脚印还没被冲掉，人应该刚离开不久。”
顾南星顺着她的视线望过去，手指在袖口里缓缓收紧。“别急着追，先看他为什么故意留下这串脚印。”
"""

_SYSTEM_SKILL_TEMPLATE = """# 剧情变量思考规范

适用范围：Storydex 剧情模式

输出要求：
1. 剧情片段正文必须保持纯叙事，不要把变量说明或系统说明写入正文。
2. 变量思考优先写成自然语言/Markdown，便于人工调试、维护和二次整理。
3. 不要求固定 JSON path/value 条目；只有在状态变化明确且适合机器合并时，才额外提供结构化变量操作。
4. 变量思考至少覆盖以下维度：
   - 时间推进
   - 天气与环境变化
   - 角色身体状态与心理状态
   - 角色关系与立场变化
   - 装备、资源、位置、任务目标变化
   - 事件因果与新悬念
5. 若本段没有某一类变化，可在 Markdown 中明确“无新增变化”，不要为了凑格式制造空 JSON。

可选机器变量路径示例（非强制输出格式）：
- time.current
- environment.weather
- environment.location
- cast.<角色名>.status
- cast.<角色名>.inventory
- cast.<角色名>.emotion
- cast.<角色名>.relation
- plot.pending_threads
- plot.resolved_threads
- plot.current_goal
"""

_PROJECT_MEMORY_TEMPLATE = """# 项目级缓存记忆

- 这里记录短期整理后的偏好、命名习惯、临时工作约束与高频修正。
"""

_FAILURES_INDEX_TEMPLATE = """# 错题集

- 这里记录已确认的纠错总结与关键改进规则。
"""

_MEMORY_README_TEMPLATE = """# Storydex 长期记忆与变量

本目录只保存经过确认、需要跨会话长期使用的故事记忆与变量，例如人物状态、关系、物品、地点、时间线、事实、伏笔、章节摘要和创作约束。

## 严格边界

- 禁止保存聊天记录、历史会话、Agent执行过程、工具日志、任务方案和临时草稿。
- 历史会话必须保存在 `.storydex/.agent/sessions/` 等 Agent 专用目录。
- `.storydex/temp/` 是普通临时工作台，除非用户明确要求或当前任务确实需要，否则不得读取、检索或注入上下文。
- 正文和用户明确确认是最高优先级证据；正式角色档案次之；派生摘要和推断不得覆盖正式事实。

## 自适应目录

AI可以根据项目实际需要创建记忆模块，不要求所有小说使用相同目录。创建模块前必须先检查现有模块，能够扩展已有模块时禁止重复创建。

每个模块必须包含 README.md 或在 catalog.json 中登记：用途、数据类型、权威来源、读取场景、写入条件、消费者、schemaVersion、生命周期、canonical/derived/index 分类。

- canonical：正式状态，可以影响后续创作，必须有证据和变更记录。
- derived：摘要或概览，可重新生成，不得反向覆盖 canonical。
- index：检索索引，可随时重建，不是事实来源。

## Agent执行规范

1. 先读取本 README 和 catalog.json，只加载与当前任务相关的模块，禁止全量倾倒记忆。
2. 使用稳定的角色、物品、地点和事件ID；显示名称变化不得创建重复实体。
3. 不直接随意覆盖正式状态。先生成包含 baseRevision、来源章节、证据和操作列表的变更集。
4. 写入前校验 schema、引用、冲突和当前 revision；通过后使用原子写入并追加 change-ledger.jsonl。
5. 高置信度且有正文证据的普通变化可以自动应用；删除、冲突、低置信度、角色合并和重大关系变化必须请求确认。
6. 变量思考使用可读 Markdown，属于辅助说明，不是正式状态，缺失不构成错误。
7. 长期未读取、重复、无消费者或失效的模块只能建议合并或归档，不得自动删除。
8. 旧项目迁移必须先备份，兼容 UTF-8 BOM、旧快照、分片变量和旧字段；展示冲突后再写入，不删除旧数据。

## 固定控制文件

- `catalog.json`：自适应模块登记表。
- `change-ledger.jsonl`：长期记忆变更账本，不保存会话内容。
- `checkpoints/`：必要时保存可恢复状态，不要求每个片段复制完整快照。
"""

_TEMP_README_TEMPLATE = """# 临时工作台

这是一个普通、灵活的临时文件夹，可用于角色草案、世界观设计、剧本、候选方案和其他中间文件。

- 本目录没有索引、诊断、生命周期、自动清理或自动迁移等系统功能。
- 内容不是正式记忆，不能直接作为后续剧情事实。
- Agent只需知道本目录可能存在临时创作文件；除非用户明确要求或当前任务需要，否则不要读取、关注或注入上下文。
- Agent内部运行中间数据应放在 `.storydex/.agent/temp/`，不要与本目录混用。
"""

_CURRENT_STATE_README_TEMPLATE = """# 当前变量状态

- `全部变量.json` 保存当前实时变量总快照。
- 其他同级 JSON 文件保存按主题切分的状态切片，便于快速检索。
"""

_CHAPTER_MEMORY_README_TEMPLATE = """# 剧情片段变量更新

- 本目录与 `chapters/` 的章节层级保持一致。
- `<片段名>.variables.md` 保存可读变量思考。
- `<片段名>.variables.json` 是可选机器快照，仅在存在明确结构化变量操作时生成。
"""

_LEGACY_AGENT_SKILLS_V1: Dict[str, str] = {
    "设计角色.md": """# 设计角色

用途：创建或补全角色档案。

要求：
- 新角色必须落到 `.storydex/characters/`。
- 缺失信息统一写“未知”。
- 不把未经剧情证据支持的信息写成硬事实。
""",
    "角色更新.md": """# 角色更新

用途：根据剧情进展更新角色信息。

要求：
- 只更新有证据支持的角色变化。
- 新出现角色必须创建角色文件。
- 近期活跃角色优先进入上下文。
""",
    "设计世界书条目.md": """# 设计世界书条目

用途：创建地点、势力、物品、功法、规则等世界书条目。

要求：
- 条目落到 `.storydex/worldbook/`。
- 区分确定设定、推测设定和待确认内容。
- 保留来源或触发剧情片段。
""",
    "设计剧本.md": """# 设计剧本

用途：规划剧情、大纲、章节路线和关键场景。

要求：
- 剧本与大纲落到 `.storydex/scripts/`。
- 不直接覆盖正文。
- 明确冲突、目标、转折和未解决线索。
""",
    "变量思考.md": """# 变量思考

用途：分析剧情后的变量变化。

要求：
- 优先输出自然语言/Markdown 变量思考，不强制固定 JSON 条目。
- 覆盖时间、地点、天气、角色状态、关系、物品和事件推进。
- 只有变化明确且适合机器合并时，才提供可选结构化变量操作。
- 变量思考记录写入 `.storydex/memory/chapters/`；机器快照只作为可选状态层。
""",
    "WIKI整理.md": """# WIKI 整理

用途：生成、更新、审阅或修复项目 WIKI。

要求：
- WIKI 输出落到 `.storydex/wiki/`。
- 只把有来源的事实写入知识结构。
- 标记冲突、过时和需要人工确认的条目。
""",
    "项目目录整理.md": """# 项目目录整理

用途：整理小说项目文件和目录。

要求：
- 不破坏基础骨架。
- 不删除用户已有文件。
- 新增目录必须职责清晰，必要时创建极简 README。
""",
    "故事生成后更新.md": """# 故事生成后更新

用途：剧情生成后执行角色、变量、物品、事实、关系和 WIKI 的增量整理。

要求：
- 先分析增量，再执行直接写入。
- 自动更新变量需要较多耗时，建议每次仅生成单条剧情片段。
- WIKI 更新应在变量更新后执行或询问用户。
""",
}


def _build_default_skill_document(
    *,
    title: str,
    purpose: str,
    triggers: List[str],
    inputs: List[str],
    asset_targets: List[str],
    steps: List[str],
    output_template: str,
    checks: List[str],
    boundaries: Optional[List[str]] = None,
) -> str:
    lines = [
        f"# {title}",
        "",
        "> 模板版本：2｜适用范围：任意 Storydex 小说项目｜性质：可直接执行的通用技能模板",
        "",
        "## 1. 技能用途",
        "",
        purpose.strip(),
        "",
        "## 2. 适用触发",
        "",
        *[f"- {item}" for item in triggers],
        "",
        "## 3. 通用前置规则",
        "",
        "- 先确认当前工作区是 Storydex 小说项目，并以当前项目根目录作为唯一工作范围。",
        "- 先读取与任务直接相关的正文、角色、世界书、剧本、记忆、WIKI 和有效预设；禁止无目的全量读取。",
        "- 证据优先级：用户本轮明确要求 > 正文与用户确认内容 > 正式角色/世界书/剧本 > WIKI 与结构化记忆 > 派生摘要与推测。",
        "- 项目没有提供的信息必须写“未知”“待确认”或“候选”，不得补成既定事实。",
        "- 写入前检查目标文件是否存在；保留用户已有结构、字段和自定义内容，不得静默覆盖。",
        "",
        "## 4. 输入检查清单",
        "",
        *[f"- {item}" for item in inputs],
        "",
        "## 5. 允许的资产落点",
        "",
        *[f"- `{item}`" for item in asset_targets],
        "",
        "除非用户明确要求，本技能不得把中间分析写入正文目录；草案可先放在回答中或 `.storydex/temp/`。",
        "",
        "## 6. 执行步骤",
        "",
        *[f"{index}. {item}" for index, item in enumerate(steps, start=1)],
        "",
        "## 7. 输出模板",
        "",
        "```markdown",
        output_template.strip(),
        "```",
        "",
        "## 8. 完成前自检",
        "",
        *[f"- [ ] {item}" for item in checks],
        "",
        "## 9. 安全边界",
        "",
        "- 不删除用户文件，不改写无关内容，不越过当前项目目录。",
        "- 不把聊天记录、工具日志、临时推理或未经确认的候选设定写入长期记忆。",
        "- 遇到事实冲突、重大关系变化、删除、角色合并或不可逆修改时，先列出影响并请求确认。",
        "- 用户只要求分析或草案时，不执行落盘；用户要求写入时，先给出目标路径和变更摘要。",
    ]
    for item in boundaries or []:
        lines.append(f"- {item}")
    return "\n".join(lines).strip() + "\n"


_DEFAULT_AGENT_SKILLS: Dict[str, str] = {
    "设计角色.md": _build_default_skill_document(
        title="设计角色",
        purpose="创建一个与当前项目世界规则、剧情需求和现有角色网络相兼容的新角色，或把角色草案补全为可持续使用的正式档案。",
        triggers=["用户要求创建新角色、补充角色卡或设计人物弧线。", "剧情需要新的对手、盟友、导师、线索人物或功能角色。"],
        inputs=["角色定位、剧情功能、首次出场阶段和用户明确约束。", "现有角色档案、世界规则、组织/势力、当前剧情状态和角色命名习惯。"],
        asset_targets=[".storydex/characters/", ".storydex/temp/（未确认草案）"],
        steps=[
            "检查现有角色，判断是否可以扩展已有角色，避免同功能角色重复。",
            "提取世界观、时代、力量体系、组织与剧情阶段约束。",
            "设计身份定位、叙事功能、外在目标、内在需求、恐惧、秘密、能力边界和行为模式。",
            "建立与现有角色、势力、地点或事件的关系钩子，并标记事实与候选设定。",
            "按项目角色模板生成档案；用户确认后选择不冲突的文件名写入。",
        ],
        output_template="""# [角色名]\n\n## 定位\n- 身份：\n- 叙事功能：\n- 当前剧情作用：\n\n## 基本信息\n- 年龄：未知\n- 外貌：未知\n- 身份/职业：未知\n- 常驻地点：未知\n\n## 性格与行为模式\n- 核心性格：\n- 说话方式：\n- 压力下的反应：\n\n## 关系网络\n- [关系对象]：关系、利益、冲突、信任与信息差\n\n## 动机、秘密与边界\n- 外在目标：\n- 内在需求：\n- 核心恐惧：\n- 秘密/未知信息：\n- 能力与行为边界：\n\n## 角色弧线与出场建议\n- 初始状态：\n- 变化触发：\n- 可能终点：\n\n## 事实状态\n- 已确认：\n- 本次建议：\n- 待确认：""",
        checks=["角色没有违反现有时代、世界规则或力量成本。", "角色功能不与已有角色无意义重叠。", "所有缺失信息均明确标为未知或待确认。", "关系和秘密不会让角色提前知道不该知道的信息。"],
    ),
    "角色更新.md": _build_default_skill_document(
        title="角色更新",
        purpose="根据新正文或用户确认内容，增量更新角色状态、关系、知识边界与人物弧线，同时保留角色档案中的稳定设定。",
        triggers=["新章节改变了角色状态、位置、关系、物品、能力或认知。", "用户要求同步角色档案、检查人物连续性或补录新角色。"],
        inputs=["本次变更对应的章节/片段和直接证据。", "目标角色现有档案、相关角色档案、当前变量与知识图谱。"],
        asset_targets=[".storydex/characters/", ".storydex/memory/（有证据的长期状态）"],
        steps=[
            "读取变更前角色档案与本次剧情证据，区分稳定属性和阶段状态。",
            "列出新增、修改、保持不变、冲突和待确认项。",
            "检查角色知识边界、关系变化和物品/位置连续性。",
            "以最小差异更新原档案；新角色使用项目角色模板新建文件。",
            "记录证据来源和需要复核的冲突，不删除仍可能有效的历史信息。",
        ],
        output_template="""# [角色名]增量更新\n\n## 证据来源\n- 文件/章节：\n- 关键证据：\n\n## 变更摘要\n- 新增：\n- 修改：\n- 保持：\n- 冲突/待确认：\n\n## 状态更新\n- 时间与位置：\n- 身体/情绪：\n- 关系：\n- 物品/能力：\n- 已知信息：\n- 未知或误解：\n\n## 建议写入\n- 目标文件：\n- 最小修改范围：""",
        checks=["每项变化都有正文或用户确认依据。", "没有把短期情绪误写为稳定性格。", "没有让派生摘要覆盖正式角色事实。", "原文件中的用户自定义字段与历史信息得到保留。"],
    ),
    "设计世界书条目.md": _build_default_skill_document(
        title="设计世界书条目",
        purpose="创建或补全地点、势力、物品、制度、历史、能力体系、种族、技术与规则等可复用世界设定。",
        triggers=["用户要求设计世界观、地点、组织、物品、规则或力量体系。", "正文出现需要沉淀为长期设定的新概念。"],
        inputs=["条目主题、题材、时代、规模、氛围和用户约束。", "现有世界书、正文证据、角色档案、势力关系和相关 WIKI。"],
        asset_targets=[".storydex/worldbook/", ".storydex/temp/（候选方案）"],
        steps=[
            "检索现有条目，决定新建、扩展还是建立关联，避免重复概念。",
            "识别该条目在剧情中的功能，以及它与人物选择和冲突的关系。",
            "定义核心规则、适用范围、成本、限制、反制、例外和失败后果。",
            "补充历史、日常影响、关联实体、已知证据和待确认问题。",
            "用户确认后按项目命名习惯写入独立条目，并更新必要关联。",
        ],
        output_template="""# [条目名称]\n\n## 分类与定位\n- 类型：地点/势力/物品/制度/能力/历史/其他\n- 剧情功能：\n\n## 核心定义\n\n## 规则、成本与限制\n- 生效条件：\n- 成本：\n- 限制：\n- 反制：\n- 失败后果：\n\n## 历史与现状\n\n## 对普通生活和剧情的影响\n\n## 关联对象\n- 角色：\n- 势力：\n- 地点/事件：\n\n## 证据与状态\n- 已确认：\n- 建议设定：\n- 待确认：""",
        checks=["规则具有边界、成本和反制，不是无限能力。", "条目与已有设定没有未说明的冲突。", "设定能够服务人物行动或剧情冲突。", "确认内容、建议内容和推测内容已清晰区分。"],
    ),
    "设计剧本.md": _build_default_skill_document(
        title="设计剧本",
        purpose="规划卷纲、章节路线、关键场景、冲突升级、人物弧线和伏笔回收，为后续正文生成提供可审阅路线。",
        triggers=["用户要求设计大纲、卷纲、章节计划、场景或剧情分支。", "当前剧情缺少明确目标、冲突或后续推进路线。"],
        inputs=["当前剧情起点、目标篇幅、章节数量、节奏和必须发生的事件。", "现有正文、未解决线索、角色状态、世界规则、预设与既有剧本。"],
        asset_targets=[".storydex/scripts/", ".storydex/temp/（未确认草案）"],
        steps=[
            "总结当前故事状态、角色目标、未解决冲突和不可违反的连续性。",
            "确定阶段目标、阻力、升级链、关键选择、人物变化和阶段终点。",
            "拆分为可执行章节/场景，确保每段都有目标、冲突、变化与钩子。",
            "建立伏笔埋设、强化和回收表，检查知识边界与因果链。",
            "输出风险与替代方案；用户确认后写入剧本目录，不直接覆盖正文。",
        ],
        output_template="""# [卷名/剧情阶段]剧本\n\n## 起点状态\n\n## 阶段目标与核心冲突\n\n## 人物弧线\n- [角色]：起点 → 触发 → 选择 → 阶段结果\n\n## 章节/场景计划\n### [章节或场景]\n- 目标：\n- 冲突：\n- 关键行动：\n- 信息揭示：\n- 状态变化：\n- 结尾钩子：\n\n## 伏笔表\n- [伏笔]：埋设 / 强化 / 回收\n\n## 连续性风险与备选路线\n""",
        checks=["每章/场景都产生有效变化而非重复信息。", "冲突升级有因果链，不依赖无依据巧合。", "角色只使用其当前能够知道的信息。", "剧本不会未经授权直接改写正文。"],
    ),
    "变量思考.md": _build_default_skill_document(
        title="变量思考",
        purpose="分析剧情片段造成的时间、地点、角色、关系、物品、事件和世界状态变化，优先生成可读的增量说明。",
        triggers=["新剧情生成或用户修改正文后需要同步状态。", "用户要求总结变量变化、检查状态连续性或生成章节快照。"],
        inputs=["本次剧情片段及其前一状态。", "当前变量、角色档案、关系图、物品与时间线。"],
        asset_targets=[".storydex/memory/chapters/", ".storydex/memory/current-state/（仅明确结构化变更）"],
        steps=[
            "定位本次片段前后的状态边界，提取明确发生的事实变化。",
            "按时间地点、角色、关系、物品、事件、线索和环境分类。",
            "区分持续状态、瞬时状态、未知、冲突和待确认内容。",
            "先生成 Markdown 变量思考；仅对确定且适合合并的变化给出结构化操作。",
            "写入前校验 baseRevision、证据、引用与冲突，重大变化请求确认。",
        ],
        output_template="""# [章节/片段]变量思考\n\n## 来源\n- 文件：\n- 时间范围：\n\n## 明确变化\n### 时间、地点与环境\n### 角色状态与知识\n### 关系\n### 物品与资源\n### 事件、任务与伏笔\n\n## 保持不变但需要关注\n\n## 冲突与待确认\n\n## 可选结构化操作\n- 仅在变化确定时列出 path / op / value / evidence / baseRevision\n""",
        checks=["变化全部来自本次片段或用户确认。", "没有把变量思考当成高优先级正式事实。", "结构化操作只包含可安全合并的明确变化。", "删除、冲突和重大关系变化已进入人工确认。"],
    ),
    "WIKI整理.md": _build_default_skill_document(
        title="WIKI 整理",
        purpose="生成、更新、审阅或修复项目 WIKI 与知识图谱，使角色、事件、关系、地点和设定可检索且具有证据来源。",
        triggers=["用户要求整理 WIKI、知识图谱、实体关系或伏笔。", "正文或项目资产发生变化，需要增量同步知识结构。"],
        inputs=["待处理的章节、角色、世界书、剧本和现有 WIKI。", "实体命名、稳定 ID、证据来源和当前关系图。"],
        asset_targets=[".storydex/wiki/", ".storydex/memory/current/relationship_graph.json（通过受控更新）"],
        steps=[
            "读取现有 WIKI 索引并识别本次相关实体，优先更新已有稳定 ID。",
            "从权威来源提取实体、事实、关系、事件和伏笔证据。",
            "区分新增、更新、冲突、过时、别名和待确认条目。",
            "以最小增量更新条目与关系，保留证据路径和可追溯说明。",
            "检查孤立实体、重复实体、无证据关系和知识边界泄露。",
        ],
        output_template="""# WIKI 增量整理报告\n\n## 处理范围\n\n## 实体变更\n- 新增：\n- 更新：\n- 别名/合并候选：\n\n## 关系变更\n- 主体 → 关系 → 客体\n- 证据：\n- 置信度：\n\n## 事件与伏笔\n\n## 冲突、过时与待确认\n\n## 建议写入路径\n""",
        checks=["每个正式事实和关系都有来源。", "显示名称变化没有制造重复实体。", "推测、候选和冲突没有被写成确定事实。", "WIKI 没有反向覆盖正文或正式角色设定。"],
    ),
    "项目目录整理.md": _build_default_skill_document(
        title="项目目录整理",
        purpose="在不破坏 Storydex 基础骨架和用户文件的前提下，整理小说项目目录、命名、归类和说明文档。",
        triggers=["用户要求整理文件、迁移目录、统一命名或修复项目结构。", "项目存在散落文件、重复目录、职责不清或旧结构。"],
        inputs=["当前完整目录树、项目 Manifest、用户指定的整理目标。", "文件引用、打开标签、Git 状态和可能依赖旧路径的配置。"],
        asset_targets=[".storydex/", "chapters/", "项目内用户明确允许整理的其他目录"],
        steps=[
            "只读扫描目录，识别基础骨架、用户文件、临时文件、重复项和旧结构。",
            "提出保留、移动、新建、合并、归档和不处理清单，说明每项影响。",
            "检查引用关系、命名风格、章节顺序和 Git 修改状态。",
            "重大移动、合并或潜在删除先请求确认；执行时使用可回退的小批次操作。",
            "整理后复查目录树、引用、项目打开与 Agent 资产路径。",
        ],
        output_template="""# 项目目录整理方案\n\n## 当前问题\n\n## 保留不动\n\n## 建议新建\n- 路径 / 用途\n\n## 建议移动或重命名\n- 原路径 → 新路径 / 理由 / 影响\n\n## 重复与归档候选\n\n## 需要确认的高风险操作\n\n## 执行后验证清单\n""",
        checks=["基础骨架与用户自定义目录均得到保护。", "不存在未经确认的删除或覆盖。", "移动后引用、配置和命名仍然有效。", "整理方案适用于当前项目，不强迫所有小说使用同一额外目录。"],
        boundaries=["`.storydex/temp/` 与 `.storydex/.agent/temp/` 职责不同，不得混用。"],
    ),
    "故事生成后更新.md": _build_default_skill_document(
        title="故事生成后更新",
        purpose="在正文新增或修改后，按依赖顺序增量同步角色、变量、物品、事实、关系、WIKI 与必要索引。",
        triggers=["Agent 完成故事生成、续写或用户保存重要剧情修改。", "用户要求把最新剧情同步到项目资产。"],
        inputs=["本次新增/修改的正文范围和生成前状态。", "相关角色、变量、WIKI、知识图谱、世界书与未解决冲突。"],
        asset_targets=[".storydex/characters/", ".storydex/memory/", ".storydex/wiki/", ".storydex/worldbook/（仅新增明确设定）"],
        steps=[
            "读取本次正文增量，列出明确新增事实、状态变化和候选推断。",
            "先执行变量思考，确定时间、地点、角色、关系、物品和事件变化。",
            "在同一次正文增量中直接同步有证据且可安全合并的角色、变量、物品、事实和关系；重大或冲突变化进入待确认。",
            "在变量和角色更新后增量整理 WIKI/知识图谱，避免使用旧状态。",
            "输出完整变更摘要、证据、写入路径和未处理事项，并检查正文未被二次改写。",
        ],
        output_template="""# 故事生成后更新报告\n\n## 本次正文范围\n\n## 变量变化\n\n## 角色更新\n\n## 物品、地点与事件\n\n## WIKI / 知识图谱更新\n\n## 已写入文件\n- 路径 / 修改摘要 / 证据\n\n## 待确认或未处理\n\n## 连续性风险\n""",
        checks=["更新范围只覆盖本次正文实际产生的变化。", "角色、变量和 WIKI 的更新顺序正确。", "不存在无证据的长期记忆写入。", "正文没有被更新流程再次覆盖或重写。"],
        boundaries=[
            "自动更新耗时较高时应分批处理，避免一次吞入过多章节。",
            "用户显式要求延期时不应用记忆增量；需复核的删除、冲突和重大关系变化始终保留待确认。",
        ],
    ),
}

_DEFAULT_AGENT_SKILL_REGISTRY: Dict[str, Any] = {
    "version": 2,
    "registryType": "storydex_agent_skill_registry",
    "policy": {
        "coomiRole": "general_agent_runtime",
        "storydexRole": "fiction_orchestration",
        "skillSource": ".storydex/.agent/skills/",
        "fileBacked": True,
        "templateFormat": "storydex_universal_skill_v2",
        "universalForAnyNovelProject": True,
    },
    "skills": [
        {
            "id": "character_design",
            "name": "设计角色",
            "file": "设计角色.md",
            "intent": "character_work",
            "assetTargets": [".storydex/characters/"],
            "description": "创建与当前项目设定、剧情功能和关系网络兼容的新角色。",
        },
        {
            "id": "character_update",
            "name": "角色更新",
            "file": "角色更新.md",
            "intent": "character_work",
            "assetTargets": [".storydex/characters/"],
            "description": "依据正文证据增量更新角色状态、关系和知识边界。",
        },
        {
            "id": "worldbook_design",
            "name": "设计世界书条目",
            "file": "设计世界书条目.md",
            "intent": "worldbook_work",
            "assetTargets": [".storydex/worldbook/"],
            "description": "设计具有规则、成本、限制和剧情功能的世界设定条目。",
        },
        {
            "id": "script_design",
            "name": "设计剧本",
            "file": "设计剧本.md",
            "intent": "script_work",
            "assetTargets": [".storydex/scripts/"],
            "description": "规划卷纲、章节路线、关键场景、人物弧线和伏笔。",
        },
        {
            "id": "variable_thinking",
            "name": "变量思考",
            "file": "变量思考.md",
            "intent": "story_generation",
            "assetTargets": [".storydex/memory/chapters/"],
            "outputPolicy": "markdown_first_optional_machine_ops",
            "description": "分析剧情后的明确变量变化，并生成可读增量记录。",
        },
        {
            "id": "wiki_organization",
            "name": "WIKI整理",
            "file": "WIKI整理.md",
            "intent": "wiki_work",
            "assetTargets": [".storydex/wiki/"],
            "description": "以证据为基础整理 WIKI、实体、关系与知识图谱。",
        },
        {
            "id": "project_organization",
            "name": "项目目录整理",
            "file": "项目目录整理.md",
            "intent": "project_organization",
            "assetTargets": [".storydex/", "chapters/"],
            "description": "在保护用户文件的前提下整理项目目录、命名与结构。",
        },
        {
            "id": "post_story_increment_update",
            "name": "故事生成后更新",
            "file": "故事生成后更新.md",
            "intent": "story_generation",
            "assetTargets": [
                ".storydex/characters/",
                ".storydex/memory/",
                ".storydex/wiki/",
            ],
            "description": "正文变更后按顺序同步角色、变量、记忆和 WIKI。",
        },
    ],
}

_LEGACY_DEFAULT_AGENT_SKILLS: Dict[str, Set[str]] = {
    file_name: {content.strip()} for file_name, content in _LEGACY_AGENT_SKILLS_V1.items()
}
_LEGACY_DEFAULT_AGENT_SKILLS["变量思考.md"].add(
    """# 变量思考

用途：分析剧情后的变量变化。

要求：
- 覆盖时间、地点、天气、角色状态、关系、物品和事件推进。
- 当前总状态写入 `.storydex/memory/current-state/全部变量.json`。
- 片段快照写入 `.storydex/memory/chapters/`。
""".strip()
)

# Match only known generated-template text so existing projects receive policy
# fixes without treating genuinely customized skills as upgrade candidates.
_LEGACY_DEFAULT_AGENT_SKILL_MARKERS: Dict[str, Tuple[Tuple[str, ...], ...]] = {
    "故事生成后更新.md": (
        (
            "模板版本：2｜适用范围：任意 Storydex 小说项目",
            "3. 同步角色档案和长期记忆，重大变化进入待确认。",
        ),
    ),
}

_DEFAULT_CHARACTER_TEMPLATE_ID = "default_character_template"
_DEFAULT_CHARACTER_TEMPLATE_JSON = "default-character-template.json"
_DEFAULT_CHARACTER_TEMPLATE_MARKDOWN = "default-character-template.md"
_DEFAULT_CHAPTER_TEMPLATE_JSON = "default-chapter-directory-template.json"
_CHARACTER_TEMPLATE_TITLE_KEYS = {
    "定位": "positioning",
    "基本信息": "basic_info",
    "性格与行为模式": "personality_behavior",
    "性格底色": "personality_behavior",
    "关系网络": "relationship_network",
    "与主角关系": "relationship_network",
    "与主角的关系": "relationship_network",
    "与陈思齐的关系": "relationship_network",
    "对主角的态度": "relationship_network",
    "对陈思齐的态度": "relationship_network",
    "人物关系": "relationship_network",
    "人际关系": "relationship_network",
    "关系定位": "relationship_network",
    "动机、秘密与边界": "motivation_secrets_boundaries",
    "写作注意": "writing_notes",
    "核心欲望": "motivation_secrets_boundaries",
    "核心恐惧": "motivation_secrets_boundaries",
    "隐藏身份": "motivation_secrets_boundaries",
    "关键信息": "motivation_secrets_boundaries",
    "叙事功能": "narrative_function",
    "功能定位": "narrative_function",
    "角色功能": "narrative_function",
    "出场与状态": "appearance_and_state",
    "出场建议": "appearance_and_state",
    "后续关系": "relationship_network",
    "补充设定": "additional_settings",
}
_CHARACTER_TEMPLATE_OPTIONAL_TITLES = {"出场建议", "出场与状态", "写作注意", "补充设定"}


class StoryProjectServiceError(StorydexError):
    default_code = "story_project_service_error"
    default_status_code = 400


@dataclass
class ChapterState:
    relative_path: str
    name: str
    display_name: str
    chapter_number: int
    completed: bool
    updated_at: str


class StoryProjectService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._lock = Lock()

    def ensure_project_structure(self, workspace_root: Path) -> None:
        root = Path(workspace_root).resolve()
        # WP-0.3: 目录骨架委托 .storydex Manifest（services/storydex_manifest.py）。
        ensure_storydex_manifest(root)

        storydex_root = self.storydex_root(root)
        # 旧 v1 目录暂时保留，不通过 Manifest 管理；WP-5.1 做迁移并最终摘除。
        # 保留这些 mkdir 是为了避免破坏老工程的运行时（兼容 reader 仍依赖）。
        starter_files = {
            self.project_settings_path(root): json.dumps(self.default_project_settings(), ensure_ascii=False, indent=2) + "\n",
            self.chapter_progress_path(root): json.dumps(self.default_chapter_progress(), ensure_ascii=False, indent=2) + "\n",
            self.default_character_template_json_path(root): json.dumps(
                self.default_character_template(),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            self.default_character_template_markdown_path(root): self.character_template_to_markdown(
                self.default_character_template()
            ),
            self.default_chapter_template_json_path(root): json.dumps(
                self.default_chapter_directory_template(),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            storydex_root / "presets" / "active.json": json.dumps(
                {
                    "version": 1,
                    "runtimePolicy": {
                        "mainPresetLimit": 1,
                        "rawJsonRuntime": False,
                        "presetSchemaVersion": 1,
                        "description": "Only files under presets/active or compiled presets may affect generation.",
                    },
                    "directories": {
                        "active": ".storydex/presets/active",
                        "library": ".storydex/presets/library",
                        "compiled": ".storydex/presets/compiled",
                        "blocked": ".storydex/presets/blocked",
                    },
                    "activeMainPreset": "",
                    "activePatches": [],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            storydex_root / "memory" / "README.md": _MEMORY_README_TEMPLATE.strip() + "\n",
            storydex_root / "memory" / "catalog.json": json.dumps(
                {"schemaVersion": 1, "revision": 0, "modules": []}, ensure_ascii=False, indent=2
            ) + "\n",
            storydex_root / "memory" / "change-ledger.jsonl": "",
            storydex_root / "temp" / "README.md": _TEMP_README_TEMPLATE.strip() + "\n",
            storydex_root / "memory" / "chapters" / "README.md": _CHAPTER_MEMORY_README_TEMPLATE.strip() + "\n",
            storydex_root / "memory" / "current-state" / "README.md": _CURRENT_STATE_README_TEMPLATE.strip() + "\n",
            storydex_root / "memory" / "current-state" / "全部变量.json": json.dumps(
                {
                    "schemaVersion": 2,
                    "revision": 0,
                    "updatedAt": "",
                    "latestSnapshotPath": "",
                    "fullState": {},
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            storydex_root / "memory" / "current" / "entities.json": json.dumps(
                {"version": 1, "entities": []},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            storydex_root / "memory" / "current" / "facts.json": json.dumps(
                {"version": 1, "facts": []},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            storydex_root / "memory" / "current" / "relationship_graph.json": json.dumps(
                {"version": 1, "edges": []},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            storydex_root / "memory" / "current" / "items.json": json.dumps(
                {"version": 1, "items": []},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        }
        for path, content in starter_files.items():
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
        (storydex_root / "memory" / "checkpoints").mkdir(parents=True, exist_ok=True)
        self._ensure_default_agent_skills(root)
        self._refresh_project_preset_skill(root)
        self.migrate_legacy_snapshots(root)

    def default_project_settings(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "storySegmentFormat": "md",
            "defaultDialogueQuote": "cn_double",
            "segmentNamingMode": "auto",
            "maxSegmentsPerChapter": 3,
            "storyFragmentCount": 1,
            "storyFragmentWordCount": 2000,
            "autoUpdateVariables": False,
            "autoUpdateWiki": False,
            "autoUpdateVariablesNote": "自动更新变量需要较多耗时，建议每次仅生成单条剧情片段。",
            "agentCommitPromptEnabled": True,
            "autoNameChapterTitle": False,
            "contextConcisionMinCalls": 1,
            "contextConcisionMaxCalls": 2,
            "contextConcisionMaxInputTokens": 32000,
            "consistencyMode": "warn",
            "consistencyJudgeEnabled": False,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }

    def default_chapter_progress(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "chapters": {},
        }

    def _ensure_default_agent_skills(self, workspace_root: Path) -> None:
        skills_root = self.agent_root(workspace_root) / "skills"
        skills_root.mkdir(parents=True, exist_ok=True)
        for file_name, content in self._read_builtin_skill_templates().items():
            path = skills_root / file_name
            if file_name == "story_preset_constraints.md":
                # This file is generated from the packaged template plus the
                # currently active project preset immediately after copying.
                continue
            if path.exists():
                legacy_contents = _LEGACY_DEFAULT_AGENT_SKILLS.get(file_name, set())
                try:
                    existing = path.read_text(encoding="utf-8").strip()
                except OSError:
                    existing = ""
                legacy_marker_groups = _LEGACY_DEFAULT_AGENT_SKILL_MARKERS.get(file_name, ())
                matches_legacy_template = existing in legacy_contents or any(
                    all(marker in existing for marker in markers)
                    for markers in legacy_marker_groups
                )
                if not matches_legacy_template:
                    continue
            path.write_text(content.strip() + "\n", encoding="utf-8")
        registry_path = skills_root / "registry.json"
        if not registry_path.exists():
            registry_path.write_text(
                json.dumps(_DEFAULT_AGENT_SKILL_REGISTRY, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    def _resolve_builtin_skills_root(self) -> Path | None:
        configured_raw = os.environ.get("STORYDEX_BUILTIN_SKILLS_ROOT", "").strip()
        configured = Path(configured_raw).expanduser() if configured_raw else None
        if configured is not None and configured.exists() and configured.is_dir():
            return configured.resolve()

        current = Path(__file__).resolve()
        for parent in current.parents:
            candidate = parent / "docs" / "skills"
            if candidate.exists() and candidate.is_dir():
                return candidate.resolve()
        return None

    def _read_builtin_skill_templates(self) -> Dict[str, str]:
        root = self._resolve_builtin_skills_root()
        if root is None:
            raise StoryProjectServiceError(
                "Storydex built-in skill templates are missing. Expected docs/skills or STORYDEX_BUILTIN_SKILLS_ROOT."
            )

        templates: Dict[str, str] = {}
        for path in sorted(root.glob("*.md"), key=lambda item: item.name.lower()):
            if not path.is_file() or path.name.lower() == "readme.md":
                continue
            templates[path.name] = path.read_text(encoding="utf-8").strip() + "\n"
        if not templates:
            raise StoryProjectServiceError(f"No built-in skill templates found in {root.as_posix()}")
        return templates

    def read_agent_skill_registry(self, workspace_root: Path) -> Dict[str, Any]:
        registry_path = self.agent_root(workspace_root) / "skills" / "registry.json"
        if not registry_path.exists():
            return dict(_DEFAULT_AGENT_SKILL_REGISTRY)
        try:
            payload = json.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return dict(_DEFAULT_AGENT_SKILL_REGISTRY)
        return payload if isinstance(payload, dict) else dict(_DEFAULT_AGENT_SKILL_REGISTRY)

    def default_character_template(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "id": _DEFAULT_CHARACTER_TEMPLATE_ID,
            "name": "默认角色模板",
            "description": "项目级角色 Markdown 骨架。JSON 是权威存储，Markdown 是用户编辑投影。模板只定义基础语义落点，不限制特殊条目；无法归类的信息写入“补充设定”。",
            "sections": [
                {
                    "key": "positioning",
                    "title": "定位",
                    "required": True,
                    "kind": "paragraph",
                    "hint": "角色在故事中的身份定位、叙事功能、与主线/支线的关系。",
                },
                {
                    "key": "basic_info",
                    "title": "基本信息",
                    "required": True,
                    "kind": "field_list",
                    "fields": ["年龄", "身份", "外貌", "住处", "常驻地点"],
                    "hint": "只写稳定静态信息。不要把关系网、秘密、剧情功能、出场安排写进这里。",
                },
                {
                    "key": "personality_behavior",
                    "title": "性格与行为模式",
                    "required": True,
                    "kind": "list",
                    "hint": "写稳定性格、说话方式、行为习惯、判断方式。避免只写临时情绪。",
                },
                {
                    "key": "relationship_network",
                    "title": "关系网络",
                    "required": True,
                    "kind": "list",
                    "hint": "写与主角、其他角色、势力、组织、地点之间的关系。主角关系放第一位，其他重要关系按对象分条。",
                },
                {
                    "key": "motivation_secrets_boundaries",
                    "title": "动机、秘密与边界",
                    "required": True,
                    "kind": "list",
                    "hint": "写核心欲望、恐惧、隐瞒信息、已知/未知信息，以及不能提前知道或不能提前揭露的内容。",
                },
                {
                    "key": "narrative_function",
                    "title": "叙事功能",
                    "required": True,
                    "kind": "list",
                    "hint": "写该角色承担的信息、情感、冲突、线索、转场、结构功能或复用价值。",
                },
                {
                    "key": "appearance_and_state",
                    "title": "出场与状态",
                    "required": False,
                    "kind": "list",
                    "hint": "写出场建议、触发条件、当前状态、后续是否复用、退场边界。",
                },
                {
                    "key": "writing_notes",
                    "title": "写作注意",
                    "required": False,
                    "kind": "list",
                    "hint": "记录口吻、禁忌、不要写偏的点，以及与既有设定的边界。",
                },
                {
                    "key": "additional_settings",
                    "title": "补充设定",
                    "required": False,
                    "kind": "open_sections",
                    "hint": "写无法归入以上栏目但重要的特殊条目。可以用三级标题自定义小节。",
                },
            ],
        }

    def default_chapter_directory_template(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "id": "default_chapter_directory",
            "name": "默认章节目录",
            "description": "目录式章节结构：chapters/第X章 标题/001.md，适合持续长篇创作和分段续写。",
            "chapterMode": "directory",
            "chapterNamePattern": "第X章 标题",
            "segmentNaming": "001.md",
            "initialChapters": [
                {
                    "number": 1,
                    "title": "未命名",
                    "directory": "第1章 未命名",
                    "firstSegment": "001.md",
                }
            ],
            "rules": [
                "全新故事默认使用本模板创建章节目录。",
                "正文片段只写入 chapters/ 下的章节目录或章节文件。",
                "变量思考记录写入 .storydex/memory/chapters/，优先 Markdown，可选机器快照。",
            ],
        }

    def character_template_to_markdown(self, template: Dict[str, Any]) -> str:
        title = str(template.get("name") or "默认角色模板").strip() or "默认角色模板"
        lines = [
            f"# {title}",
            "",
            "> 用于约束新角色 Markdown 角色卡的基础栏目顺序。注释是填写指引，不写入角色正文；无法归类的重要内容写入“补充设定”。",
            "",
        ]
        sections = template.get("sections")
        if not isinstance(sections, list):
            sections = []
        for section in sections:
            if not isinstance(section, dict):
                continue
            section_title = str(section.get("title") or "").strip()
            if not section_title:
                continue
            lines.append(f"## {section_title}")
            hint = str(section.get("hint") or "").strip()
            fields = section.get("fields")
            field_text = ""
            if isinstance(fields, list):
                field_names = [str(item).strip() for item in fields if str(item).strip()]
                if field_names:
                    field_text = "建议包含：" + "、".join(field_names) + "。"
            comment = " ".join(part for part in [hint, field_text] if part).strip()
            if comment:
                lines.append(f"<!-- {comment} -->")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def normalize_character_template(self, template: Any) -> Dict[str, Any]:
        if not isinstance(template, dict):
            template = {}
        default = self.default_character_template()
        raw_sections = template.get("sections")
        if not isinstance(raw_sections, list):
            raw_sections = default["sections"]
        sections: List[Dict[str, Any]] = []
        for index, section in enumerate(raw_sections, start=1):
            if not isinstance(section, dict):
                continue
            title = str(section.get("title") or "").strip()
            if not title:
                continue
            key = str(section.get("key") or "").strip() or self._character_template_key_from_title(title, index)
            normalized_section: Dict[str, Any] = {
                "key": key,
                "title": title,
                "required": bool(section.get("required", title not in _CHARACTER_TEMPLATE_OPTIONAL_TITLES)),
                "kind": str(section.get("kind") or "paragraph").strip() or "paragraph",
            }
            hint = str(section.get("hint") or "").strip()
            if hint:
                normalized_section["hint"] = hint
            fields = section.get("fields")
            if isinstance(fields, list):
                normalized_fields = [str(item).strip() for item in fields if str(item).strip()]
                if normalized_fields:
                    normalized_section["fields"] = normalized_fields
            sections.append(normalized_section)
        if not sections:
            sections = default["sections"]
        return {
            "version": 1,
            "id": str(template.get("id") or _DEFAULT_CHARACTER_TEMPLATE_ID).strip() or _DEFAULT_CHARACTER_TEMPLATE_ID,
            "name": str(template.get("name") or default["name"]).strip() or default["name"],
            "description": str(template.get("description") or default["description"]).strip() or default["description"],
            "sections": sections,
        }

    def character_template_from_markdown(self, markdown: str) -> Dict[str, Any]:
        text = str(markdown or "").replace("\r\n", "\n").replace("\r", "\n")
        name = "默认角色模板"
        sections: List[Dict[str, Any]] = []
        current: Optional[Dict[str, Any]] = None
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("# ") and not stripped.startswith("## "):
                candidate = stripped[2:].strip()
                if candidate:
                    name = candidate
                continue
            heading_match = re.match(r"^##\s+(.+?)\s*$", stripped)
            if heading_match:
                title = heading_match.group(1).strip()
                if title:
                    current = {
                        "key": self._character_template_key_from_title(title, len(sections) + 1),
                        "title": title,
                        "required": title not in _CHARACTER_TEMPLATE_OPTIONAL_TITLES,
                        "kind": "paragraph",
                    }
                    sections.append(current)
                continue
            if current is None:
                continue
            comment_match = re.match(r"^<!--\s*(.*?)\s*-->$", stripped)
            if comment_match:
                hint = comment_match.group(1).strip()
                if hint:
                    current["hint"] = hint
        if not sections:
            raise StoryProjectServiceError(
                "Character template Markdown must contain at least one section heading.",
                code="character_template_invalid",
                details={"expectedHeading": "## 栏目名称"},
            )
        return self.normalize_character_template(
            {
                "version": 1,
                "id": _DEFAULT_CHARACTER_TEMPLATE_ID,
                "name": name,
                "description": "项目级角色 Markdown 骨架。JSON 是权威存储，Markdown 是用户编辑投影。",
                "sections": sections,
            }
        )

    def read_character_template(self, workspace_root: Path) -> Dict[str, Any]:
        self.ensure_project_structure(workspace_root)
        root = Path(workspace_root).resolve()
        template = self.normalize_character_template(self._read_json(self.default_character_template_json_path(root)))
        markdown = self.character_template_to_markdown(template)
        return {
            "template": template,
            "markdown": markdown,
            "templateJsonPath": self.default_character_template_json_path(root).relative_to(root).as_posix(),
            "templateMarkdownPath": self.default_character_template_markdown_path(root).relative_to(root).as_posix(),
        }

    def write_character_template_from_markdown(self, workspace_root: Path, markdown: str) -> Dict[str, Any]:
        self.ensure_project_structure(workspace_root)
        root = Path(workspace_root).resolve()
        template = self.character_template_from_markdown(markdown)
        rendered_markdown = self.character_template_to_markdown(template)
        self.default_character_template_json_path(root).write_text(
            json.dumps(template, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.default_character_template_markdown_path(root).write_text(rendered_markdown, encoding="utf-8")
        return {
            "template": template,
            "markdown": rendered_markdown,
            "templateJsonPath": self.default_character_template_json_path(root).relative_to(root).as_posix(),
            "templateMarkdownPath": self.default_character_template_markdown_path(root).relative_to(root).as_posix(),
        }

    def storydex_root(self, workspace_root: Path) -> Path:
        return Path(workspace_root).resolve() / self.settings.storydex_dir_name

    def agent_root(self, workspace_root: Path) -> Path:
        return self.storydex_root(workspace_root) / ".agent"

    def project_settings_path(self, workspace_root: Path) -> Path:
        return self.storydex_root(workspace_root) / "config" / "project-settings.json"

    def character_templates_root(self, workspace_root: Path) -> Path:
        return self.storydex_root(workspace_root) / "templates" / "characters"

    def default_character_template_json_path(self, workspace_root: Path) -> Path:
        return self.character_templates_root(workspace_root) / _DEFAULT_CHARACTER_TEMPLATE_JSON

    def default_character_template_markdown_path(self, workspace_root: Path) -> Path:
        return self.character_templates_root(workspace_root) / _DEFAULT_CHARACTER_TEMPLATE_MARKDOWN

    def chapter_templates_root(self, workspace_root: Path) -> Path:
        return self.storydex_root(workspace_root) / "templates" / "chapters"

    def default_chapter_template_json_path(self, workspace_root: Path) -> Path:
        return self.chapter_templates_root(workspace_root) / _DEFAULT_CHAPTER_TEMPLATE_JSON

    def list_chapter_templates(self, workspace_root: Path) -> List[Dict[str, Any]]:
        root = Path(workspace_root).resolve()
        self.ensure_project_structure(root)
        template_root = self.chapter_templates_root(root)
        templates: List[Dict[str, Any]] = []
        if not template_root.exists():
            return templates
        for path in sorted(template_root.glob("*.json"), key=lambda item: item.name.lower()):
            payload: Dict[str, Any] = {}
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    payload = loaded
            except (OSError, json.JSONDecodeError):
                payload = {}
            templates.append(
                {
                    "id": str(payload.get("id") or path.stem),
                    "name": str(payload.get("name") or path.stem),
                    "relativePath": path.relative_to(root).as_posix(),
                    "description": str(payload.get("description") or ""),
                    "chapterMode": str(payload.get("chapterMode") or "directory"),
                    "chapterNamePattern": str(payload.get("chapterNamePattern") or ""),
                    "segmentNaming": str(payload.get("segmentNaming") or "001.md"),
                    "initialChapters": payload.get("initialChapters") if isinstance(payload.get("initialChapters"), list) else [],
                }
            )
        return templates

    def find_chapter_template(self, workspace_root: Path, template_id: str) -> Dict[str, Any] | None:
        normalized_id = str(template_id or "").strip()
        if not normalized_id:
            return None
        for template in self.list_chapter_templates(workspace_root):
            if str(template.get("id") or "") == normalized_id:
                return template
        return None

    def initial_segment_path_from_chapter_template(self, template: Dict[str, Any]) -> str:
        chapter_mode = str(template.get("chapterMode") or "directory").strip() or "directory"
        segment_name = self._safe_template_path_part(str(template.get("segmentNaming") or "001.md"), fallback="001.md")
        initial_chapters = template.get("initialChapters") if isinstance(template.get("initialChapters"), list) else []
        initial = initial_chapters[0] if initial_chapters and isinstance(initial_chapters[0], dict) else {}
        first_segment = self._safe_template_path_part(
            str(initial.get("firstSegment") or segment_name),
            fallback=segment_name,
        )
        title = str(initial.get("title") or "未命名").strip() or "未命名"
        number = self._safe_int(initial.get("number"), fallback=1, minimum=1, maximum=9999)
        if chapter_mode in {"flat", "flat_file", "file"}:
            file_name = self._safe_template_path_part(
                str(initial.get("file") or initial.get("filename") or f"第{number}章 {title}.md"),
                fallback=f"第{number}章 {title}.md",
            )
            return f"chapters/{file_name}"
        chapter_dir = self._safe_template_path_part(
            str(initial.get("directory") or self._chapter_name_from_template(template, number=number, title=title)),
            fallback=f"第{number}章 {title}",
        )
        return f"chapters/{chapter_dir}/{first_segment}"

    def chapter_progress_path(self, workspace_root: Path) -> Path:
        return self.storydex_root(workspace_root) / "memory" / "chapter-progress.json"

    def current_state_master_path(self, workspace_root: Path) -> Path:
        return self.storydex_root(workspace_root) / "memory" / "current-state" / "全部变量.json"

    def memory_catalog_path(self, workspace_root: Path) -> Path:
        return self.storydex_root(workspace_root) / "memory" / "catalog.json"

    def memory_change_ledger_path(self, workspace_root: Path) -> Path:
        return self.storydex_root(workspace_root) / "memory" / "change-ledger.jsonl"

    def latest_snapshot_index_path(self, workspace_root: Path) -> Path:
        return self.storydex_root(workspace_root) / "memory" / "current-state" / "最新快照索引.json"

    def concision_root(self, workspace_root: Path) -> Path:
        return self.agent_root(workspace_root) / "sessions" / "concision"

    def legacy_concision_root(self, workspace_root: Path) -> Path:
        return self.storydex_root(workspace_root) / "memory" / "concision"

    def agent_temp_root(self, workspace_root: Path) -> Path:
        return self.agent_root(workspace_root) / "temp"

    def read_project_settings(self, workspace_root: Path) -> Dict[str, Any]:
        self.ensure_project_structure(workspace_root)
        payload = self._read_json(self.project_settings_path(workspace_root))
        if not isinstance(payload, dict):
            payload = {}
        merged = self.default_project_settings()
        merged.update(
            {
                "storySegmentFormat": self._normalize_story_segment_format(payload.get("storySegmentFormat")),
                "defaultDialogueQuote": str(payload.get("defaultDialogueQuote") or "cn_double").strip() or "cn_double",
                "segmentNamingMode": str(payload.get("segmentNamingMode") or "auto").strip() or "auto",
                "maxSegmentsPerChapter": self._normalize_max_segments_per_chapter(payload.get("maxSegmentsPerChapter")),
                "storyFragmentCount": self._normalize_story_fragment_count(payload.get("storyFragmentCount")),
                "storyFragmentWordCount": self._normalize_story_fragment_word_count(payload.get("storyFragmentWordCount")),
                "autoUpdateVariables": self._normalize_bool(payload.get("autoUpdateVariables"), default=False),
                "autoUpdateWiki": self._normalize_bool(payload.get("autoUpdateWiki"), default=False),
                "autoUpdateVariablesNote": str(
                    payload.get("autoUpdateVariablesNote")
                    or "自动更新变量需要较多耗时，建议每次仅生成单条剧情片段。"
                ),
                "agentCommitPromptEnabled": self._normalize_bool(
                    payload.get("agentCommitPromptEnabled", payload.get("agent_commit_prompt_enabled")),
                    default=True,
                ),
                "autoNameChapterTitle": self._normalize_bool(payload.get("autoNameChapterTitle"), default=False),
                "contextConcisionMinCalls": self._normalize_llm_call_count(payload.get("contextConcisionMinCalls"), fallback=1),
                "contextConcisionMaxCalls": self._normalize_llm_call_count(payload.get("contextConcisionMaxCalls"), fallback=2),
                "contextConcisionMaxInputTokens": self._normalize_context_input_tokens(
                    payload.get("contextConcisionMaxInputTokens"),
                    fallback=32000,
                ),
                "consistencyMode": self._normalize_consistency_mode(payload.get("consistencyMode")),
                "consistencyJudgeEnabled": self._normalize_bool(payload.get("consistencyJudgeEnabled"), default=False),
                "updatedAt": str(payload.get("updatedAt") or datetime.now(timezone.utc).isoformat()),
            }
        )
        merged["contextConcisionMaxCalls"] = max(merged["contextConcisionMinCalls"], merged["contextConcisionMaxCalls"])
        return merged

    def write_project_settings(self, workspace_root: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
        current = self.read_project_settings(workspace_root)
        current["storySegmentFormat"] = self._normalize_story_segment_format(payload.get("storySegmentFormat"))
        current["defaultDialogueQuote"] = str(payload.get("defaultDialogueQuote") or current["defaultDialogueQuote"]).strip() or "cn_double"
        current["segmentNamingMode"] = str(payload.get("segmentNamingMode") or current["segmentNamingMode"]).strip() or "auto"
        current["maxSegmentsPerChapter"] = self._normalize_max_segments_per_chapter(
            payload.get("maxSegmentsPerChapter", current.get("maxSegmentsPerChapter"))
        )
        current["storyFragmentCount"] = self._normalize_story_fragment_count(
            payload.get("storyFragmentCount", current.get("storyFragmentCount"))
        )
        current["storyFragmentWordCount"] = self._normalize_story_fragment_word_count(
            payload.get("storyFragmentWordCount", current.get("storyFragmentWordCount"))
        )
        current["autoUpdateVariables"] = self._normalize_bool(
            payload.get("autoUpdateVariables", current.get("autoUpdateVariables")),
            default=False,
        )
        current["autoUpdateWiki"] = self._normalize_bool(
            payload.get("autoUpdateWiki", current.get("autoUpdateWiki")),
            default=False,
        )
        current["autoUpdateVariablesNote"] = str(
            payload.get("autoUpdateVariablesNote")
            or current.get("autoUpdateVariablesNote")
            or "自动更新变量需要较多耗时，建议每次仅生成单条剧情片段。"
        )
        current["agentCommitPromptEnabled"] = self._normalize_bool(
            payload.get(
                "agentCommitPromptEnabled",
                payload.get("agent_commit_prompt_enabled", current.get("agentCommitPromptEnabled")),
            ),
            default=True,
        )
        current["autoNameChapterTitle"] = self._normalize_bool(
            payload.get("autoNameChapterTitle", current.get("autoNameChapterTitle")),
            default=False,
        )
        current["contextConcisionMinCalls"] = self._normalize_llm_call_count(
            payload.get("contextConcisionMinCalls", current.get("contextConcisionMinCalls")),
            fallback=int(current.get("contextConcisionMinCalls") or 1),
        )
        current["contextConcisionMaxCalls"] = self._normalize_llm_call_count(
            payload.get("contextConcisionMaxCalls", current.get("contextConcisionMaxCalls")),
            fallback=int(current.get("contextConcisionMaxCalls") or 2),
        )
        current["contextConcisionMaxCalls"] = max(current["contextConcisionMinCalls"], current["contextConcisionMaxCalls"])
        current["contextConcisionMaxInputTokens"] = self._normalize_context_input_tokens(
            payload.get("contextConcisionMaxInputTokens", current.get("contextConcisionMaxInputTokens")),
            fallback=int(current.get("contextConcisionMaxInputTokens") or 32000),
        )
        current["consistencyMode"] = self._normalize_consistency_mode(
            payload.get("consistencyMode", current.get("consistencyMode"))
        )
        current["consistencyJudgeEnabled"] = self._normalize_bool(
            payload.get("consistencyJudgeEnabled", current.get("consistencyJudgeEnabled")),
            default=False,
        )
        current["updatedAt"] = datetime.now(timezone.utc).isoformat()
        self.project_settings_path(workspace_root).write_text(
            json.dumps(current, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return current

    def read_chapter_progress(self, workspace_root: Path) -> Dict[str, Any]:
        self.ensure_project_structure(workspace_root)
        payload = self._read_json(self.chapter_progress_path(workspace_root))
        if not isinstance(payload, dict):
            payload = {}
        normalized = self.default_chapter_progress()
        chapters: Dict[str, Any] = {}
        raw_chapters = payload.get("chapters")
        if isinstance(raw_chapters, dict):
            for key, value in raw_chapters.items():
                normalized_key = self._normalize_relative_path(str(key or ""))
                if not normalized_key or not isinstance(value, dict):
                    continue
                chapters[normalized_key] = {
                    "completed": bool(value.get("completed", False)),
                    "updatedAt": str(value.get("updatedAt") or ""),
                    "displayName": str(value.get("displayName") or Path(normalized_key).name),
                }
        normalized["chapters"] = chapters
        normalized["updatedAt"] = str(payload.get("updatedAt") or normalized["updatedAt"])
        return normalized

    def set_chapter_completed(self, workspace_root: Path, chapter_relative_path: str, completed: bool) -> Dict[str, Any]:
        progress = self.read_chapter_progress(workspace_root)
        normalized = self._normalize_relative_path(chapter_relative_path)
        if not normalized:
            raise StoryProjectServiceError("Chapter path is required.")
        chapter_dir = Path(workspace_root).resolve() / normalized
        if not chapter_dir.exists() or not chapter_dir.is_dir():
            raise StoryProjectServiceError(
                "Chapter directory does not exist.",
                details={"chapterPath": normalized},
            )
        now_iso = datetime.now(timezone.utc).isoformat()
        progress["chapters"][normalized] = {
            "completed": bool(completed),
            "updatedAt": now_iso,
            "displayName": self._build_chapter_display_name(chapter_dir.name),
        }
        progress["updatedAt"] = now_iso
        self.chapter_progress_path(workspace_root).write_text(
            json.dumps(progress, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return progress["chapters"][normalized]

    def replace_chapter_completion(self, workspace_root: Path, chapter_completion: Dict[str, Any]) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        progress = self.default_chapter_progress()
        normalized_completion = chapter_completion if isinstance(chapter_completion, dict) else {}
        now_iso = datetime.now(timezone.utc).isoformat()
        chapters: Dict[str, Dict[str, Any]] = {}

        for key, value in normalized_completion.items():
            relative_path = self._normalize_relative_path(str(key or ""))
            if not relative_path:
                continue
            chapter_dir = root / relative_path
            display_name = self._build_chapter_display_name(chapter_dir.name if chapter_dir.name else relative_path)
            chapters[relative_path] = {
                "completed": bool(value),
                "updatedAt": now_iso,
                "displayName": display_name,
            }

        progress["chapters"] = chapters
        progress["updatedAt"] = now_iso
        self.chapter_progress_path(root).write_text(
            json.dumps(progress, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return progress

    def list_chapter_states(self, workspace_root: Path) -> List[ChapterState]:
        root = Path(workspace_root).resolve()
        self._normalize_chapter_directories(root)
        chapters_root = root / "chapters"
        progress = self.read_chapter_progress(root)
        chapter_entries = progress.get("chapters") if isinstance(progress.get("chapters"), dict) else {}
        states: List[ChapterState] = []
        if not chapters_root.exists():
            return states

        chapter_paths = [*self._sorted_chapter_dirs(chapters_root), *self._sorted_flat_chapter_files(chapters_root)]
        chapter_paths.sort(
            key=lambda path: (
                self._extract_chapter_number(path.stem if path.is_file() else path.name) or 999999,
                path.stat().st_mtime,
                path.name.lower(),
            )
        )

        for index, chapter_path in enumerate(chapter_paths, start=1):
            relative = chapter_path.relative_to(root).as_posix()
            progress_entry = chapter_entries.get(relative) if isinstance(chapter_entries, dict) else {}
            completed = bool(progress_entry.get("completed", False)) if isinstance(progress_entry, dict) else False
            updated_at = (
                str(progress_entry.get("updatedAt") or "")
                if isinstance(progress_entry, dict)
                else datetime.fromtimestamp(chapter_path.stat().st_mtime, timezone.utc).isoformat()
            )
            raw_name = chapter_path.stem if chapter_path.is_file() else chapter_path.name
            chapter_number = self._extract_chapter_number(raw_name) or index
            states.append(
                ChapterState(
                    relative_path=relative,
                    name=chapter_path.name,
                    display_name=self._build_chapter_display_name(raw_name, fallback_number=chapter_number),
                    chapter_number=chapter_number,
                    completed=completed,
                    updated_at=updated_at,
                )
            )
        states.sort(key=lambda item: (item.chapter_number, item.relative_path))
        return states

    def resolve_existing_chapter_by_number(
        self,
        workspace_root: Path,
        chapter_number: int,
    ) -> Optional[str]:
        """T-fix: 根据章号定位已有章节的 in-place 覆写目标路径。

        flat 单文件章节 → 直接返回 chapters/第N章 标题.md
        目录式章节 → 返回 chapters/第N章 标题/seg-0001.md（最早 seg；不存在则用 default）
        找不到匹配章节 → None。
        """
        if not isinstance(chapter_number, int) or chapter_number <= 0:
            return None
        root = Path(workspace_root).resolve()
        states = self.list_chapter_states(root)
        match = next((item for item in states if item.chapter_number == chapter_number), None)
        if match is None:
            return None
        chapter_path = root / match.relative_path
        if chapter_path.is_file():
            return match.relative_path
        if chapter_path.is_dir():
            settings = self.read_project_settings(root)
            extension = "." + settings["storySegmentFormat"]
            existing_segs = sorted(
                [p for p in chapter_path.iterdir() if p.is_file() and p.suffix.lower() == extension],
                key=lambda p: p.name.lower(),
            )
            if existing_segs:
                return existing_segs[0].relative_to(root).as_posix()
            return f"{match.relative_path}/{self._default_segment_name(extension)}"
        return None

    def compute_continue_segment_path(
        self,
        workspace_root: Path,
        chapter_number: int,
        *,
        prompt: str = "",
    ) -> str:
        """T-fix: 定位"继续/续写第N章"的下一段写入路径。

        语义是"在已有第N章后面追加新片段"：
        - 目录式章节 → 返回该章节目录的下一段路径（无视 completed 标记，因为用户显式要求继续）。
        - flat 单文件章节 → 返回该章节文件本身（由下游决定追加/覆写）。
        - 第N章不存在 → 返回 ""，调用方回退到默认 compute_next_segment_path。
        """
        if not isinstance(chapter_number, int) or chapter_number <= 0:
            return ""
        root = Path(workspace_root).resolve()
        self.ensure_project_structure(root)
        settings = self.read_project_settings(root)
        extension = "." + settings["storySegmentFormat"]
        match = next(
            (item for item in self.list_chapter_states(root) if item.chapter_number == chapter_number),
            None,
        )
        if match is None:
            return ""
        chapter_path = root / match.relative_path
        if chapter_path.is_dir():
            return self._next_segment_path_in_chapter(
                chapter_dir=chapter_path,
                workspace_root=root,
                extension=extension,
            )
        if chapter_path.is_file():
            return match.relative_path
        return ""

    def build_tree_meta(self, workspace_root: Path) -> Dict[str, Dict[str, Any]]:
        root = Path(workspace_root).resolve()
        chapter_states = self.list_chapter_states(root)
        diagnostics = self.collect_story_diagnostics(root)
        meta: Dict[str, Dict[str, Any]] = {}

        for chapter in chapter_states:
            meta[chapter.relative_path] = {
                "story": {
                    "kind": "chapter",
                    "chapterNumber": chapter.chapter_number,
                    "displayName": chapter.display_name,
                    "completed": chapter.completed,
                    "updatedAt": chapter.updated_at,
                },
                "diagnostics": diagnostics.get(chapter.relative_path, []),
            }

        for relative_path, items in diagnostics.items():
            meta.setdefault(relative_path, {})
            meta[relative_path]["diagnostics"] = items
            if relative_path.startswith("chapters/") and root.joinpath(relative_path).is_file():
                meta.setdefault(relative_path, {}).setdefault(
                    "story",
                    {
                        "kind": "segment",
                        "snapshotPath": self.snapshot_relative_path(root, relative_path),
                    },
                )

        for chapter in chapter_states:
            chapter_path = root / chapter.relative_path
            if not chapter_path.exists():
                continue
            if self._is_story_text_file(chapter_path):
                chapter_segments = [chapter_path]
            elif chapter_path.is_dir():
                chapter_segments = self._sorted_segment_files(chapter_path)
            else:
                chapter_segments = []
            for file_path in chapter_segments:
                relative = file_path.relative_to(root).as_posix()
                meta.setdefault(relative, {})
                meta[relative].setdefault(
                    "story",
                    {
                        "kind": "segment",
                        "snapshotPath": self.snapshot_relative_path(root, relative),
                        "variableThoughtPath": self.variable_thought_relative_path(root, relative),
                        "segmentExtension": file_path.suffix.lower(),
                    },
                )
                meta[relative].setdefault("diagnostics", diagnostics.get(relative, []))
        return meta

    def collect_story_diagnostics(self, workspace_root: Path) -> Dict[str, List[Dict[str, Any]]]:
        root = Path(workspace_root).resolve()
        diagnostics: Dict[str, List[Dict[str, Any]]] = {}
        chapter_states = self.list_chapter_states(root)
        for chapter in chapter_states:
            chapter_path = root / chapter.relative_path
            if chapter_path.is_file():
                chapter_segments = [chapter_path] if self._is_story_text_file(chapter_path) else []
            elif chapter_path.is_dir():
                chapter_segments = [
                    file_path
                    for file_path in chapter_path.iterdir()
                    if self._is_story_text_file(file_path)
                ]
            else:
                chapter_segments = []
            stale_count = 0
            for file_path in chapter_segments:
                relative = file_path.relative_to(root).as_posix()
                snapshot_relative = self.snapshot_relative_path(root, relative)
                thought_relative = self.variable_thought_relative_path(root, relative)
                snapshot_path = root / snapshot_relative
                thought_path = root / thought_relative
                # Fragment memory is optional; only an existing record can be stale.
                memory_paths = [path for path in (thought_path, snapshot_path) if path.exists()]
                if memory_paths:
                    snapshot_mtime = max(path.stat().st_mtime for path in memory_paths)
                    segment_mtime = file_path.stat().st_mtime
                    if segment_mtime <= snapshot_mtime:
                        continue
                    stale_count += 1
                    diagnostics.setdefault(relative, []).append(
                        {
                            "code": "story_snapshot_stale",
                            "source": "story.memory",
                            "severity": "warning",
                            "relativePath": relative,
                            "line": 0,
                            "column": 0,
                            "message": "该剧情片段在变量思考记录后被修改过，建议重新整理变量思考。",
                        }
                    )
                    continue
            if stale_count > 0:
                diagnostics.setdefault(chapter.relative_path, []).append(
                    {
                        "code": "story_snapshot_stale_in_chapter",
                        "source": "story.memory",
                        "severity": "warning",
                        "relativePath": chapter.relative_path,
                        "line": 0,
                        "column": 0,
                        "message": f"本章节有 {stale_count} 个剧情片段的变量思考记录可能已过期。",
                    }
                )
        return diagnostics

    def compute_next_segment_path(self, workspace_root: Path, *, active_file: str = "", prompt: str = "") -> str:
        root = Path(workspace_root).resolve()
        self.ensure_project_structure(root)
        settings = self.read_project_settings(root)
        extension = "." + settings["storySegmentFormat"]
        max_segments = self._normalize_max_segments_per_chapter(settings.get("maxSegmentsPerChapter"))
        chapter_states = self.list_chapter_states(root)

        active_chapter_relative = self._resolve_active_chapter_relative(active_file)
        active_chapter_state = next((item for item in chapter_states if item.relative_path == active_chapter_relative), None)
        if active_chapter_relative and active_chapter_state is None:
            active_chapter_number = self._extract_chapter_number(Path(active_chapter_relative).name)
            if active_chapter_number > 0:
                active_chapter_state = next(
                    (item for item in chapter_states if item.chapter_number == active_chapter_number),
                    None,
                )
        if active_chapter_relative and active_chapter_state is None:
            active_chapter_path = root / active_chapter_relative
            if active_chapter_path.exists() and active_chapter_path.is_dir():
                return self._next_segment_path_in_chapter(
                    chapter_dir=active_chapter_path,
                    workspace_root=root,
                    extension=extension,
                )
            normalized_active = self._normalize_relative_path(active_file)
            active_suffix = Path(normalized_active).suffix.lower()
            if normalized_active.startswith(active_chapter_relative + "/") and active_suffix in _TEXT_SEGMENT_SUFFIXES:
                return (Path(active_chapter_relative) / self._default_segment_name(extension)).as_posix()

        candidate_chapters: List[ChapterState] = []
        if active_chapter_state is not None and not active_chapter_state.completed:
            candidate_chapters.append(active_chapter_state)

        for item in reversed(chapter_states):
            if item.completed:
                continue
            if any(existing.relative_path == item.relative_path for existing in candidate_chapters):
                continue
            candidate_chapters.append(item)

        for chapter_state in candidate_chapters:
            chapter_dir = root / chapter_state.relative_path
            if chapter_dir.is_file():
                continue
            if not chapter_dir.exists() or not chapter_dir.is_dir():
                continue
            if self._chapter_has_capacity(chapter_dir, max_segments=max_segments):
                return self._next_segment_path_in_chapter(chapter_dir=chapter_dir, workspace_root=root, extension=extension)

        next_number = (chapter_states[-1].chapter_number + 1) if chapter_states else 1
        chapter_title = "未命名"
        if self._normalize_bool(settings.get("autoNameChapterTitle"), default=False):
            chapter_title = self._suggest_new_chapter_title(
                root,
                chapter_number=next_number,
                prompt=prompt,
                active_file=active_file,
            )
        if self._uses_flat_chapter_files(root):
            chapter_name = (
                f"{self._build_new_chapter_name(next_number, title=chapter_title, number_style=self._infer_flat_chapter_number_style(root))}"
                f"{extension}"
            )
            return f"chapters/{chapter_name}"
        chapter_relative = f"chapters/{self._build_new_chapter_name(next_number, title=chapter_title)}"
        return f"{chapter_relative}/{self._default_segment_name(extension)}"

    def snapshot_relative_path(self, workspace_root: Path, segment_relative_path: str) -> str:
        normalized = self._normalize_relative_path(segment_relative_path)
        if not normalized:
            return ""
        path = Path(normalized)
        if len(path.parts) == 2 and path.parent.as_posix() == "chapters":
            chapter_name = path.stem
        else:
            chapter_name = path.parent.name if len(path.parts) >= 2 else "第一章 未命名"
        return (
            self.storydex_root(workspace_root)
            / "memory"
            / "chapters"
            / chapter_name
            / f"{path.stem}.variables.json"
        ).relative_to(Path(workspace_root).resolve()).as_posix()

    def variable_thought_relative_path(self, workspace_root: Path, segment_relative_path: str) -> str:
        normalized = self._normalize_relative_path(segment_relative_path)
        if not normalized:
            return ""
        path = Path(normalized)
        if len(path.parts) == 2 and path.parent.as_posix() == "chapters":
            chapter_name = path.stem
        else:
            chapter_name = path.parent.name if len(path.parts) >= 2 else "第一章 未命名"
        return (
            self.storydex_root(workspace_root)
            / "memory"
            / "chapters"
            / chapter_name
            / f"{path.stem}.variables.md"
        ).relative_to(Path(workspace_root).resolve()).as_posix()

    def read_current_state(self, workspace_root: Path) -> Dict[str, Any]:
        self.ensure_project_structure(workspace_root)
        payload = self._read_json(self.current_state_master_path(workspace_root))
        return payload if isinstance(payload, dict) else {}

    def find_latest_snapshot(self, workspace_root: Path) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        self.ensure_project_structure(root)
        latest_index = self._read_json(self.latest_snapshot_index_path(root))
        latest_relative_path = str(latest_index.get("latestSnapshotPath") or "").strip() if isinstance(latest_index, dict) else ""
        if latest_relative_path:
            payload = self._read_json(root / latest_relative_path)
            if isinstance(payload, dict) and payload:
                return {
                    "relativePath": latest_relative_path,
                    "snapshot": payload,
                }

        snapshot_root = self.storydex_root(root) / "memory" / "chapters"
        if not snapshot_root.exists():
            return {}
        snapshot_files = sorted(
            [path for path in snapshot_root.rglob("*.variables.json") if path.is_file()],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for snapshot_path in snapshot_files:
            payload = self._read_json(snapshot_path)
            if isinstance(payload, dict) and payload:
                return {
                    "relativePath": snapshot_path.relative_to(root).as_posix(),
                    "snapshot": payload,
                }
        return {}

    def read_latest_concision(self, workspace_root: Path) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        self.ensure_project_structure(root)
        files: List[Path] = []
        for concision_root in (self.concision_root(root), self.legacy_concision_root(root)):
            if concision_root.exists():
                files.extend(path for path in concision_root.glob("*.md") if path.is_file())
        if not files:
            return {}
        files = sorted(
            files,
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not files:
            return {}
        latest = files[0]
        try:
            content = latest.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return {}
        return {
            "relativePath": latest.relative_to(root).as_posix(),
            "content": content,
            "updatedAt": datetime.fromtimestamp(latest.stat().st_mtime, timezone.utc).isoformat(),
        }

    def build_story_workspace_inventory(self, workspace_root: Path, *, max_items: int = 320) -> str:
        root = Path(workspace_root).resolve()
        self.ensure_project_structure(root)
        candidates: List[str] = []
        include_roots = [
            root / "chapters",
            self.storydex_root(root) / "characters",
            self.storydex_root(root) / "worldbook",
            self.storydex_root(root) / "scripts",
            self.storydex_root(root) / "presets",
            self.storydex_root(root) / "memory",
            self.storydex_root(root) / "config",
        ]
        for base in include_roots:
            if not base.exists():
                continue
            for path in sorted(base.rglob("*")):
                if not path.is_file():
                    continue
                try:
                    relative = path.relative_to(root).as_posix()
                except ValueError:
                    continue
                candidates.append(relative)
                if len(candidates) >= max(40, int(max_items or 320)):
                    break
            if len(candidates) >= max(40, int(max_items or 320)):
                break
        if not candidates:
            return "- (empty workspace)"
        return "\n".join(f"- {item}" for item in candidates)

    def read_concision_source_documents(
        self,
        workspace_root: Path,
        relative_paths: Iterable[str],
        *,
        max_chars_per_file: int = 4000,
    ) -> List[Dict[str, str]]:
        root = Path(workspace_root).resolve()
        docs: List[Dict[str, str]] = []
        seen: set[str] = set()
        for item in relative_paths:
            normalized = self._normalize_relative_path(str(item or ""))
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            target = root / normalized
            if not target.exists() or not target.is_file():
                continue
            try:
                content = read_bounded_text_preview(target, max_chars=max_chars_per_file)
            except (OSError, UnicodeDecodeError):
                continue
            docs.append(
                {
                    "relativePath": normalized,
                    "content": content,
                }
            )
        return docs

    def write_concision_note(self, workspace_root: Path, content: str) -> Dict[str, str]:
        root = Path(workspace_root).resolve()
        self.ensure_project_structure(root)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        concision_root = self.concision_root(root)
        readme = concision_root / "README.md"
        if not readme.exists():
            concision_root.mkdir(parents=True, exist_ok=True)
            readme.write_text("# 上下文压缩\n\n存放 Agent 会话的上下文压缩记录。\n", encoding="utf-8")
        path = concision_root / f"{timestamp}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content or "").strip() + "\n", encoding="utf-8")
        return {"relativePath": path.relative_to(root).as_posix(), "content": str(content or "").strip()}

    def write_agent_temp_note(self, workspace_root: Path, *, name: str, content: str) -> str:
        root = Path(workspace_root).resolve()
        self.ensure_project_structure(root)
        safe_name = _INVALID_FILE_CHARS.sub("_", str(name or "temp")).strip("._") or "temp"
        path = self.agent_temp_root(root) / f"{safe_name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content or "").strip() + "\n", encoding="utf-8")
        return path.relative_to(root).as_posix()

    def cleanup_agent_temp_notes(self, workspace_root: Path, relative_paths: Iterable[str]) -> None:
        root = Path(workspace_root).resolve()
        temp_root = self.agent_temp_root(root)
        for item in relative_paths:
            normalized = self._normalize_relative_path(str(item or ""))
            if not normalized:
                continue
            target = root / normalized
            try:
                target.relative_to(temp_root)
            except ValueError:
                continue
            if target.exists() and target.is_file():
                try:
                    target.unlink()
                except OSError:
                    continue

    def sync_current_state_from_latest_snapshot(self, workspace_root: Path) -> List[str]:
        latest = self.find_latest_snapshot(workspace_root)
        if not latest:
            return []
        return self.sync_current_state_from_snapshot_payload(
            workspace_root,
            str(latest.get("relativePath") or ""),
            latest.get("snapshot") if isinstance(latest.get("snapshot"), dict) else {},
        )

    def migrate_legacy_snapshots(self, workspace_root: Path) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        chapters_root = root / "chapters"
        if not chapters_root.exists():
            return {"migratedCount": 0, "existingCount": 0}

        migrated_count = 0
        existing_count = 0
        backup_count = 0
        conflicts: List[str] = []
        for legacy_snapshot in chapters_root.rglob("*.variables.json"):
            if not legacy_snapshot.is_file():
                continue
            try:
                legacy_relative = legacy_snapshot.relative_to(root).as_posix()
            except ValueError:
                continue
            if not legacy_relative.startswith("chapters/"):
                continue
            segment_relative = self._segment_relative_from_legacy_snapshot(root, legacy_relative)
            if not segment_relative:
                continue
            target_relative = self.snapshot_relative_path(root, segment_relative)
            target_path = root / target_relative
            if target_path.exists():
                existing_count += 1
                existing_payload = self._read_json(target_path)
                legacy_payload = self._read_json(legacy_snapshot)
                if existing_payload and legacy_payload and existing_payload != legacy_payload:
                    conflicts.append(legacy_relative)
                continue
            payload = self._read_json(legacy_snapshot)
            if not isinstance(payload, dict):
                continue
            normalized_payload = dict(payload)
            backup_path = self.storydex_root(root) / "memory" / "migration" / "v1-backup" / legacy_relative
            if not backup_path.exists():
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(legacy_snapshot, backup_path)
                backup_count += 1
            normalized_payload.setdefault("segment_path", segment_relative)
            normalized_payload.setdefault("fragment_format", Path(segment_relative).suffix.lower().lstrip(".") or "md")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(json.dumps(normalized_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            migrated_count += 1
        return {
            "migratedCount": migrated_count,
            "existingCount": existing_count,
            "backupCount": backup_count,
            "conflictCount": len(conflicts),
            "conflicts": conflicts,
        }

    def sync_current_state_from_snapshot_file(self, workspace_root: Path, snapshot_relative_path: str) -> List[str]:
        root = Path(workspace_root).resolve()
        normalized = self._normalize_relative_path(snapshot_relative_path)
        if not normalized:
            return []
        snapshot_path = root / normalized
        payload = self._read_json(snapshot_path)
        if not isinstance(payload, dict):
            return []
        return self.sync_current_state_from_snapshot_payload(root, normalized, payload)

    def sync_current_state_from_snapshot_payload(
        self,
        workspace_root: Path,
        snapshot_relative_path: str,
        payload: Dict[str, Any],
    ) -> List[str]:
        root = Path(workspace_root).resolve()
        self.ensure_project_structure(root)
        current_state_root = self.storydex_root(root) / "memory" / "current-state"
        current_state_root.mkdir(parents=True, exist_ok=True)
        full_state = payload.get("full_state") if isinstance(payload.get("full_state"), dict) else {}
        updated_at = str(payload.get("created_at") or datetime.now(timezone.utc).isoformat())
        previous_master = self._read_json(self.current_state_master_path(root))
        current_revision = int(previous_master.get("revision") or 0) if isinstance(previous_master, dict) else 0
        base_revision = int(payload.get("base_revision")) if payload.get("base_revision") is not None else current_revision
        snapshot_revision = int(payload.get("revision") or 0)
        is_idempotent_replay = (
            isinstance(previous_master, dict)
            and str(previous_master.get("latestSnapshotPath") or "") == snapshot_relative_path
            and snapshot_revision == current_revision
        )
        if is_idempotent_replay:
            return sorted(
                path.relative_to(root).as_posix()
                for path in current_state_root.rglob("*")
                if path.is_file()
            )
        if current_revision and base_revision != current_revision:
            raise ValueError(f"Memory revision conflict: expected {current_revision}, received {base_revision}.")
        revision = int(payload.get("revision") or (current_revision + 1))
        written_paths: List[str] = []

        master_payload = {
            "schemaVersion": 2,
            "revision": revision,
            "updatedAt": updated_at,
            "latestSnapshotPath": snapshot_relative_path,
            "fullState": full_state,
        }
        master_path = current_state_root / "全部变量.json"
        master_path.write_text(json.dumps(master_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written_paths.append(master_path.relative_to(root).as_posix())

        latest_path = current_state_root / "最新快照索引.json"
        latest_path.write_text(
            json.dumps(
                {
                    "updatedAt": updated_at,
                    "latestSnapshotPath": snapshot_relative_path,
                    "chapterId": str(payload.get("chapter_id") or ""),
                    "segmentId": str(payload.get("segment_id") or ""),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        written_paths.append(latest_path.relative_to(root).as_posix())

        for key, value in sorted(full_state.items(), key=lambda item: str(item[0])):
            safe_name = self._safe_leaf_name(str(key or "状态"))
            if not safe_name:
                continue
            slice_path = current_state_root / f"{safe_name}.json"
            slice_payload = {
                "updatedAt": updated_at,
                "latestSnapshotPath": snapshot_relative_path,
                "key": key,
                "value": value,
            }
            slice_path.write_text(json.dumps(slice_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            written_paths.append(slice_path.relative_to(root).as_posix())

        written_paths.extend(
            self._sync_character_files_from_snapshot_payload(
                root,
                snapshot_relative_path=snapshot_relative_path,
                payload=payload,
                full_state=full_state,
                updated_at=updated_at,
            )
        )
        self._register_memory_module(root, source_path=str(payload.get("segment_path") or ""))
        self._append_memory_change_ledger(
            root,
            {
                "schemaVersion": 1,
                "changeSetId": str(payload.get("change_set_id") or ""),
                "moduleId": "current-state",
                "baseRevision": base_revision,
                "revision": revision,
                "sourcePath": str(payload.get("segment_path") or ""),
                "snapshotPath": snapshot_relative_path,
                "operationCount": len(payload.get("operations") if isinstance(payload.get("operations"), list) else []),
                "summary": str(payload.get("snapshot_comment") or "更新当前故事变量"),
                "createdAt": updated_at,
            },
        )
        return written_paths

    def _register_memory_module(self, workspace_root: Path, *, source_path: str) -> None:
        catalog_path = self.memory_catalog_path(workspace_root)
        catalog = self._read_json(catalog_path)
        if not isinstance(catalog, dict):
            catalog = {"schemaVersion": 1, "revision": 0, "modules": []}
        modules = catalog.get("modules") if isinstance(catalog.get("modules"), list) else []
        module = next((item for item in modules if isinstance(item, dict) and item.get("id") == "current-state"), None)
        now_iso = datetime.now(timezone.utc).isoformat()
        entry = {
            "id": "current-state",
            "path": "current-state",
            "title": "当前故事变量",
            "purpose": "保存经过校验的当前人物、物品、关系、地点、时间线与剧情状态。",
            "kind": "canonical",
            "schemaVersion": 2,
            "authoritativeSources": [source_path] if source_path else [],
            "readTriggers": ["相关续写、一致性检查或用户查询"],
            "writeTriggers": ["正文或用户确认产生明确状态变化"],
            "consumers": ["coomi-generation", "story-consistency-check"],
            "lifecycle": "active",
            "updatedAt": now_iso,
        }
        if module is None:
            entry["createdAt"] = now_iso
            modules.append(entry)
        else:
            module.update(entry)
        catalog["modules"] = modules
        catalog["revision"] = int(catalog.get("revision") or 0) + 1
        catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _append_memory_change_ledger(self, workspace_root: Path, payload: Dict[str, Any]) -> None:
        ledger = self.memory_change_ledger_path(workspace_root)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")

    def apply_story_generation_increment(self, workspace_root: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Apply a structured post-generation increment to Storydex project files."""
        root = Path(workspace_root).resolve()
        self.ensure_project_structure(root)
        if not isinstance(payload, dict):
            payload = {}

        settings = self.read_project_settings(root)
        active_file = self._normalize_relative_path(str(payload.get("activeFile") or payload.get("active_file") or ""))
        prompt = str(payload.get("prompt") or "").strip()
        fragments = self._normalize_story_increment_fragments(payload)
        if not fragments:
            fragments = [{}]

        top_level_increment = self._normalize_story_increment_payload(payload)
        has_explicit_apply_variables = any(
            key in payload and payload.get(key) not in (None, "")
            for key in ("applyVariables", "apply_variables")
        )
        has_generated_text = bool(self._story_increment_fragment_text(payload)) or any(
            self._story_increment_fragment_text(fragment) for fragment in fragments
        )
        has_generated_memory_payload = self._story_increment_has_variable_payload(top_level_increment) or any(
            self._story_increment_has_variable_payload(self._normalize_story_increment_payload(fragment))
            for fragment in fragments
        )
        # A generated fragment carries the model's governed memory delta. Apply that
        # delta immediately unless the caller explicitly chose to defer variables;
        # the project-wide switch remains the default for non-generation updates.
        auto_apply_generated_memory = (
            not has_explicit_apply_variables and has_generated_text and has_generated_memory_payload
        )
        apply_variables = self._normalize_bool(
            payload.get("applyVariables", payload.get("apply_variables")),
            default=bool(settings.get("autoUpdateVariables", False)) or auto_apply_generated_memory,
        )
        apply_wiki = self._normalize_bool(
            payload.get("applyWiki", payload.get("apply_wiki")),
            default=bool(settings.get("autoUpdateWiki", False)) and apply_variables,
        )
        chapter_summary = str(payload.get("chapterSummary") or payload.get("chapter_summary") or "").strip()
        written_paths: List[str] = []
        fragment_results: List[Dict[str, Any]] = []
        applied_facts: List[Dict[str, Any]] = []
        applied_relationships: List[Dict[str, Any]] = []
        applied_items: List[Dict[str, Any]] = []
        all_character_updates: List[Dict[str, Any]] = []
        knowledge_review_items: List[Dict[str, Any]] = []
        applied_knowledge_command_count = 0
        applied_non_command_snapshot_updates = False
        chapter_path_mapping: Dict[str, str] = {}
        has_variable_payload = self._story_increment_has_variable_payload(top_level_increment)
        now_iso = datetime.now(timezone.utc).isoformat()

        for index, fragment in enumerate(fragments):
            is_last_fragment = index == len(fragments) - 1
            segment_relative_path = self._resolve_story_increment_segment_path(
                root,
                fragment,
                active_file=active_file,
                prompt=prompt,
                settings=settings,
            )
            # Existing chapter aliases may have been normalized while resolving the
            # path. Keep the increment anchored to the final directory name before
            # writing any derived memory paths.
            chapter_path_mapping.update(self._normalize_chapter_directories(root))
            segment_relative_path = self._rewrite_segment_path_for_chapter_mapping(
                segment_relative_path,
                chapter_path_mapping,
            )
            segment_text = self._story_increment_fragment_text(fragment)
            if segment_text:
                segment_path = root / segment_relative_path
                segment_path.parent.mkdir(parents=True, exist_ok=True)
                segment_path.write_text(segment_text.rstrip() + "\n", encoding="utf-8")

            # A newly-created non-canonical directory is only visible to the
            # normalizer after the segment has been written. Apply the rename mapping
            # now, before computing snapshot/thought paths or reading prior state.
            chapter_path_mapping.update(self._normalize_chapter_directories(root))
            segment_relative_path = self._rewrite_segment_path_for_chapter_mapping(
                segment_relative_path,
                chapter_path_mapping,
            )
            if segment_text:
                written_paths.append(segment_relative_path)

            fragment_increment = self._normalize_story_increment_payload(fragment)
            stage2_output = self._merge_story_increment_payloads(
                fragment_increment,
                top_level_increment if is_last_fragment else {},
            )
            has_variable_payload = has_variable_payload or self._story_increment_has_variable_payload(stage2_output)
            snapshot_relative_path = self.snapshot_relative_path(root, segment_relative_path)
            thought_relative_path = self.variable_thought_relative_path(root, segment_relative_path)
            variable_thought_written = False
            all_character_updates.extend(stage2_output.get("character_updates", []))
            item_updates = [
                {**item, "source_segment": item.get("source_segment") or segment_relative_path}
                for item in stage2_output.get("item_updates", [])
                if isinstance(item, dict)
            ]
            normalized_fragment_operations = self._normalize_snapshot_operations(
                stage2_output.get("variable_updates", [])
            )
            accepted_fragment_operations, review_required_fragment_operations = (
                self._partition_snapshot_operations(normalized_fragment_operations)
            )
            has_non_command_snapshot_updates = any(
                isinstance(stage2_output.get(key), list) and bool(stage2_output.get(key))
                for key in ("memory_updates", "character_updates", "event_updates")
            )
            review_only_fragment = (
                bool(review_required_fragment_operations)
                and not accepted_fragment_operations
                and not has_non_command_snapshot_updates
            )

            snapshot_written = False
            if apply_variables:
                variable_thoughts = stage2_output.get("variable_thoughts")
                if isinstance(variable_thoughts, list) and variable_thoughts and not review_only_fragment:
                    thought_path = root / thought_relative_path
                    thought_path.parent.mkdir(parents=True, exist_ok=True)
                    thought_path.write_text(
                        self._render_variable_thought_markdown(
                            segment_relative_path=segment_relative_path,
                            thoughts=variable_thoughts,
                            stage2_output=stage2_output,
                            updated_at=now_iso,
                        ),
                        encoding="utf-8",
                    )
                    written_paths.append(thought_path.relative_to(root).as_posix())
                    variable_thought_written = True

                if self._story_increment_has_structured_variable_operations(stage2_output):
                    previous_snapshot = self._read_previous_snapshot(root, segment_relative_path)
                    if not previous_snapshot:
                        previous_snapshot = self.find_latest_snapshot(root).get("snapshot") or {}
                    snapshot_payload = self._build_snapshot_payload(
                        previous_snapshot=previous_snapshot if isinstance(previous_snapshot, dict) else {},
                        stage2_output=stage2_output,
                        segment_relative_path=segment_relative_path,
                        operations=accepted_fragment_operations,
                    )
                    knowledge_review_items.extend(review_required_fragment_operations)
                    applied_knowledge_command_count += len(snapshot_payload.get("operations", []))
                    if snapshot_payload.get("operations") or has_non_command_snapshot_updates:
                        snapshot_path = root / snapshot_relative_path
                        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
                        snapshot_path.write_text(json.dumps(snapshot_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                        written_paths.append(snapshot_path.relative_to(root).as_posix())
                        written_paths.extend(
                            self.sync_current_state_from_snapshot_payload(
                                root,
                                snapshot_relative_path,
                                snapshot_payload,
                            )
                        )
                        snapshot_written = True
                        applied_non_command_snapshot_updates = (
                            applied_non_command_snapshot_updates or has_non_command_snapshot_updates
                        )
                elif stage2_output.get("character_updates"):
                    current_state = self.read_current_state(root)
                    full_state = current_state.get("fullState") if isinstance(current_state.get("fullState"), dict) else {}
                    written_paths.extend(
                        self._sync_character_files_from_snapshot_payload(
                            root,
                            snapshot_relative_path=snapshot_relative_path,
                            payload={
                                "segment_path": segment_relative_path,
                                "created_at": now_iso,
                                "character_updates": stage2_output.get("character_updates", []),
                            },
                            full_state=full_state,
                            updated_at=now_iso,
                        )
                    )
            elif stage2_output.get("character_updates"):
                current_state = self.read_current_state(root)
                full_state = current_state.get("fullState") if isinstance(current_state.get("fullState"), dict) else {}
                written_paths.extend(
                    self._sync_character_files_from_snapshot_payload(
                        root,
                        snapshot_relative_path=snapshot_relative_path,
                        payload={
                            "segment_path": segment_relative_path,
                            "created_at": now_iso,
                            "character_updates": stage2_output.get("character_updates", []),
                        },
                        full_state=full_state,
                        updated_at=now_iso,
                    )
                )

            if apply_variables:
                applied_facts.extend(stage2_output.get("fact_updates", []))
                applied_relationships.extend(stage2_output.get("relationship_updates", []))
                applied_items.extend(item_updates)

            fragment_results.append(
                {
                    "segmentPath": segment_relative_path,
                    "snapshotPath": snapshot_relative_path,
                    "variableThoughtPath": thought_relative_path,
                    "snapshotWritten": snapshot_written,
                    "variableThoughtWritten": variable_thought_written,
                    "variableThoughtCount": len(stage2_output.get("variable_thoughts", [])),
                    "characterUpdateCount": len(stage2_output.get("character_updates", [])),
                    "variableUpdateCount": len(stage2_output.get("variable_updates", [])),
                    "itemUpdateCount": len(item_updates),
                    "factUpdateCount": len(stage2_output.get("fact_updates", [])),
                    "relationshipUpdateCount": len(stage2_output.get("relationship_updates", [])),
                }
            )

        if all_character_updates:
            written_paths.extend(self._upsert_entities_from_character_updates(root, all_character_updates, updated_at=now_iso))
        if apply_variables and applied_facts:
            written_paths.extend(self._apply_fact_updates(root, applied_facts, updated_at=now_iso))
        if apply_variables and applied_relationships:
            written_paths.extend(self._apply_relationship_updates(root, applied_relationships, updated_at=now_iso))
        if apply_variables and applied_items:
            written_paths.extend(self._apply_item_updates(root, applied_items, updated_at=now_iso))

        chapter_summary_path = ""
        if chapter_summary:
            # 滚动章节摘要：Agent 生成正文时顺带产出，零额外 LLM 往返；
            # 按章节 key 覆盖写入，作为中程剧情脉络的读取层数据源。
            from services.memory_extraction_service import MemoryExtractionService

            last_segment = fragment_results[-1]["segmentPath"] if fragment_results else ""
            chapter_summary_path = (
                MemoryExtractionService().write_rolling_summary(
                    chapter_summary,
                    workspace_root=root,
                    chapter_id=self._chapter_key_for_segment(last_segment),
                )
                or ""
            )
            if chapter_summary_path:
                written_paths.append(chapter_summary_path)

        review_only_knowledge_batch = bool(knowledge_review_items) and not any(
            (
                applied_knowledge_command_count,
                applied_non_command_snapshot_updates,
                applied_facts,
                applied_relationships,
                applied_items,
                all_character_updates,
            )
        )
        wiki_applied = apply_wiki and not review_only_knowledge_batch
        wiki_payload: Dict[str, Any] = {}
        if wiki_applied:
            from services.story_wiki_service import get_story_wiki_service

            wiki_service = get_story_wiki_service()
            wiki_payload = wiki_service.sync_local_incremental(root)
            for wiki_path in (
                wiki_service.wiki_json_path(root),
                wiki_service.wiki_markdown_path(root),
                wiki_service.wiki_index_path(root),
            ):
                if wiki_path.exists():
                    written_paths.append(wiki_path.relative_to(root).as_posix())

        unique_written_paths = list(dict.fromkeys(path for path in written_paths if path))
        required_decisions: List[Dict[str, Any]] = []
        if not apply_variables and has_variable_payload:
            required_decisions.append(
                {
                    "type": "update_variables",
                    "message": "变量更新默认需要询问用户；用户同意后用 applyVariables=true 重新应用增量。",
                }
            )
        if (
            apply_variables
            and not apply_wiki
            and not review_only_knowledge_batch
            and bool(settings.get("autoUpdateWiki", False)) is False
        ):
            required_decisions.append(
                {
                    "type": "update_wiki",
                    "message": "WIKI 更新默认在变量更新后询问用户；用户同意后用 applyWiki=true 同步 WIKI。",
                }
            )

        result = {
            "ok": True,
            "applied": {
                "variables": apply_variables,
                "wiki": wiki_applied,
                "facts": apply_variables and bool(applied_facts),
                "relationships": apply_variables and bool(applied_relationships),
                "items": apply_variables and bool(applied_items),
            },
            "settings": {
                "autoUpdateVariables": bool(settings.get("autoUpdateVariables", False)),
                "autoUpdateWiki": bool(settings.get("autoUpdateWiki", False)),
                "autoUpdateVariablesNote": str(settings.get("autoUpdateVariablesNote") or ""),
            },
            "fragments": fragment_results,
            "chapterSummaryPath": chapter_summary_path,
            "writtenPaths": unique_written_paths,
            "writtenPathCount": len(unique_written_paths),
            "requiredDecisions": required_decisions,
            "wiki": {
                "entryCount": len(wiki_payload.get("entries", [])) if isinstance(wiki_payload, dict) else 0,
                "graphNodeCount": len(wiki_payload.get("graph", {}).get("nodes", [])) if isinstance(wiki_payload.get("graph"), dict) else 0,
                "graphEdgeCount": len(wiki_payload.get("graph", {}).get("edges", [])) if isinstance(wiki_payload.get("graph"), dict) else 0,
            },
            "items": {
                "appliedCount": len(applied_items) if apply_variables else 0,
                "memoryPath": ".storydex/memory/current/items.json",
            },
        }
        if knowledge_review_items:
            result["knowledgeReview"] = {
                "status": "review_required",
                "code": "knowledge_review_required",
                "items": knowledge_review_items,
                "appliedCount": applied_knowledge_command_count,
            }
        return result

    @staticmethod
    def _chapter_key_for_segment(segment_relative_path: str) -> str:
        """从片段路径推断滚动摘要的章节 key：chapters/第1章/001.md → 第1章。"""
        normalized = str(segment_relative_path or "").replace("\\", "/").strip("/")
        parts = [part for part in normalized.split("/") if part]
        if len(parts) >= 3 and parts[0] == "chapters":
            return parts[1]
        if parts:
            return Path(parts[-1]).stem
        return ""

    def build_generation_context(
        self,
        workspace_root: Path,
        *,
        active_file: str = "",
        prompt: str = "",
    ) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        self.ensure_project_structure(root)
        settings = self.read_project_settings(root)
        chapter_states = self.list_chapter_states(root)
        current_state_path = self.storydex_root(root) / "memory" / "current-state" / "全部变量.json"
        latest_index_path = self.storydex_root(root) / "memory" / "current-state" / "最新快照索引.json"
        current_state = self._read_json(current_state_path)
        latest_index = self._read_json(latest_index_path)
        normalized_active_file = self._normalize_relative_path(active_file)
        focus_chapter = self._resolve_focus_chapter_state(
            root,
            active_file=normalized_active_file,
            chapter_states=chapter_states,
        )
        focus_chapter_relative = focus_chapter.relative_path if focus_chapter is not None else ""
        recent_segments = self.list_recent_segments(
            root,
            chapter_relative_path=focus_chapter_relative,
            limit=3,
            include_content=False,
        )
        if not recent_segments:
            recent_segments = self.list_recent_segments(root, limit=3, include_content=False)
        relevant_scripts = self.list_relevant_scripts(
            root,
            prompt=prompt,
            active_file=normalized_active_file,
            limit=3,
            include_content=False,
        )

        return {
            "storyRules": {
                "dialogueQuote": "中文双引号“”",
                "chapterRoot": "chapters/",
                "chapterNamePattern": "第X章 标题",
                "segmentNamePattern": "001.md 或 001.txt",
                "maxSegmentsPerChapter": settings.get("maxSegmentsPerChapter", 3),
                "storyFileMustBePlainNarrative": True,
                "variableThoughtPathPattern": ".storydex/memory/chapters/<章节>/<片段>.variables.md",
                "snapshotPathPattern": ".storydex/memory/chapters/<章节>/<片段>.variables.json",
                "machineSnapshotIsOptional": True,
            },
            "projectSettings": settings,
            "chapterStates": [
                {
                    "relativePath": item.relative_path,
                    "displayName": item.display_name,
                    "completed": item.completed,
                    "updatedAt": item.updated_at,
                }
                for item in chapter_states
            ],
            "projectRulesText": "",
            "projectSkillText": "",
            "projectNamingSkillText": "",
            "latestSnapshotIndex": latest_index,
            "currentStatePreview": self._truncate_text(
                json.dumps(current_state.get("fullState", {}), ensure_ascii=False, indent=2)
                if isinstance(current_state.get("fullState"), dict)
                else json.dumps(current_state, ensure_ascii=False, indent=2),
                max_chars=3200,
            ),
            "focusChapter": self._serialize_chapter_state(focus_chapter),
            "nextSegmentPath": self.compute_next_segment_path(root, active_file=normalized_active_file, prompt=prompt),
            "recentSegmentPaths": [str(item.get("relativePath") or "") for item in recent_segments],
            "storyScriptPaths": [str(item.get("relativePath") or "") for item in relevant_scripts],
        }

    def list_recent_segments(
        self,
        workspace_root: Path,
        *,
        chapter_relative_path: str = "",
        limit: int = 3,
        include_content: bool = False,
        max_chars: int = 900,
        exclude_chapter_numbers: Optional[List[int]] = None,
    ) -> List[Dict[str, Any]]:
        root = Path(workspace_root).resolve()
        self.ensure_project_structure(root)
        ordered = self._ordered_segment_paths(root)
        normalized_chapter = self._normalize_relative_path(chapter_relative_path)
        if normalized_chapter:
            ordered = [relative for relative in ordered if Path(relative).parent.as_posix() == normalized_chapter]
        # T-fix: 重写场景下排除目标章节自身，避免把要重写的内容喂回模型当参考。
        if exclude_chapter_numbers:
            excluded_files: Set[str] = set()
            excluded_dirs: Set[str] = set()
            try:
                states = self.list_chapter_states(root)
                excluded_numbers = {int(n) for n in exclude_chapter_numbers}
                for state in states:
                    if state.chapter_number in excluded_numbers:
                        rel = state.relative_path
                        chapter_path = root / rel
                        if chapter_path.is_file():
                            excluded_files.add(rel)
                        elif chapter_path.is_dir():
                            excluded_dirs.add(rel)
            except Exception:
                pass
            if excluded_files or excluded_dirs:
                ordered = [
                    relative
                    for relative in ordered
                    if relative not in excluded_files
                    and not any(relative.startswith(d + "/") for d in excluded_dirs)
                ]
        if not ordered:
            return []

        max_count = max(1, min(int(limit or 1), 8))
        selected = ordered[-max_count:]
        results: List[Dict[str, Any]] = []
        for relative in selected:
            segment_path = root / relative
            relative_path_obj = Path(relative)
            chapter_path = relative if len(relative_path_obj.parts) == 2 and relative_path_obj.parent.as_posix() == "chapters" else relative_path_obj.parent.as_posix()
            preview = self._read_text_preview(segment_path, max_chars=max_chars)
            item = {
                "relativePath": relative,
                "chapterPath": chapter_path,
                "segmentId": relative_path_obj.stem,
                "snapshotPath": self.snapshot_relative_path(root, relative),
                "updatedAt": datetime.fromtimestamp(segment_path.stat().st_mtime, timezone.utc).isoformat(),
            }
            if include_content:
                item["content"] = preview
            else:
                item["snippet"] = self._truncate_text(" ".join(preview.split()), max_chars=min(320, max_chars))
            results.append(item)
        return results

    def list_relevant_scripts(
        self,
        workspace_root: Path,
        *,
        prompt: str = "",
        active_file: str = "",
        limit: int = 3,
        include_content: bool = False,
        max_chars: int = 1200,
    ) -> List[Dict[str, Any]]:
        root = Path(workspace_root).resolve()
        self.ensure_project_structure(root)
        script_root = self.storydex_root(root) / "scripts"
        if not script_root.exists():
            return []

        normalized_active_file = self._normalize_relative_path(active_file)
        focus_chapter = self._resolve_focus_chapter_state(root, active_file=normalized_active_file)
        focus_name = focus_chapter.display_name if focus_chapter is not None else ""
        terms = self._extract_story_terms(
            prompt=prompt,
            active_file=normalized_active_file,
            focus_name=focus_name,
        )

        script_paths = sorted(
            (
                script_path
                for script_path in script_root.rglob("*")
                if script_path.is_file() and script_path.suffix.lower() in _SCRIPT_CONTEXT_SUFFIXES
            ),
            key=lambda path: path.relative_to(root).as_posix(),
        )
        if not script_paths:
            return []

        max_workers = min(8, len(script_paths))
        if max_workers <= 1:
            candidates = [
                candidate
                for candidate in (
                    self._build_relevant_script_candidate(
                        root=root,
                        script_path=script_path,
                        terms=terms,
                        active_file=normalized_active_file,
                        include_content=include_content,
                        max_chars=max_chars,
                    )
                    for script_path in script_paths
                )
                if candidate is not None
            ]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                candidates = [
                    candidate
                    for candidate in pool.map(
                        lambda script_path: self._build_relevant_script_candidate(
                            root=root,
                            script_path=script_path,
                            terms=terms,
                            active_file=normalized_active_file,
                            include_content=include_content,
                            max_chars=max_chars,
                        ),
                        script_paths,
                    )
                    if candidate is not None
                ]

        if not candidates:
            return []

        candidates.sort(
            key=lambda item: (
                -float(item.get("score") or 0.0),
                -float(item.get("_mtime") or 0.0),
                str(item.get("relativePath") or ""),
            )
        )
        max_count = max(1, min(int(limit or 1), 8))
        selected = candidates[:max_count]
        for item in selected:
            item.pop("_mtime", None)
        return selected

    def _build_relevant_script_candidate(
        self,
        *,
        root: Path,
        script_path: Path,
        terms: List[str],
        active_file: str,
        include_content: bool,
        max_chars: int,
    ) -> Optional[Dict[str, Any]]:
        try:
            content = read_bounded_text_preview(script_path, max_chars=max(max_chars, 2400))
            stat = script_path.stat()
        except (OSError, UnicodeDecodeError):
            return None

        relative_path = script_path.relative_to(root).as_posix()
        score = self._score_story_script(
            relative_path=relative_path,
            content=content,
            terms=terms,
            active_file=active_file,
        )
        if Path(relative_path).name.lower().startswith("readme"):
            score -= 1.0

        item: Dict[str, Any] = {
            "relativePath": relative_path,
            "title": script_path.stem,
            "updatedAt": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "score": round(score, 2),
            "_mtime": stat.st_mtime,
        }
        if include_content:
            item["content"] = self._truncate_text(content, max_chars=max_chars)
        else:
            item["contentPreview"] = self._truncate_text(" ".join(content.split()), max_chars=min(360, max_chars))
        return item


    @staticmethod
    def _serialize_chapter_state(chapter: Optional[ChapterState]) -> Dict[str, Any]:
        if chapter is None:
            return {}
        return {
            "relativePath": chapter.relative_path,
            "displayName": chapter.display_name,
            "completed": chapter.completed,
            "updatedAt": chapter.updated_at,
        }

    def _resolve_focus_chapter_state(
        self,
        workspace_root: Path,
        *,
        active_file: str,
        chapter_states: Optional[List[ChapterState]] = None,
    ) -> Optional[ChapterState]:
        root = Path(workspace_root).resolve()
        chapters = chapter_states if chapter_states is not None else self.list_chapter_states(root)
        normalized_active = self._normalize_relative_path(active_file)
        if normalized_active.startswith("chapters/"):
            active_chapter_relative = Path(normalized_active).parent.as_posix()
            active_chapter = next((item for item in chapters if item.relative_path == active_chapter_relative), None)
            if active_chapter is not None:
                return active_chapter

        incomplete = [item for item in chapters if not item.completed]
        if incomplete:
            return incomplete[-1]
        if chapters:
            return chapters[-1]
        return None

    def _resolve_snapshot_anchor(
        self,
        workspace_root: Path,
        *,
        active_file: str,
        focus_chapter_relative: str,
    ) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        normalized_active = self._normalize_relative_path(active_file)
        if normalized_active.startswith("chapters/"):
            snapshot_relative = self.snapshot_relative_path(root, normalized_active)
            snapshot_payload = self._read_json(root / snapshot_relative)
            if isinstance(snapshot_payload, dict) and snapshot_payload:
                return {"relativePath": snapshot_relative, "snapshot": snapshot_payload}

            previous_snapshot = self._read_previous_snapshot(root, normalized_active)
            if previous_snapshot:
                return {
                    "relativePath": str(previous_snapshot.get("relativePath") or ""),
                    "snapshot": previous_snapshot,
                }

        recent_segments = self.list_recent_segments(root, chapter_relative_path=focus_chapter_relative, limit=1)
        if recent_segments:
            snapshot_relative = str(recent_segments[0].get("snapshotPath") or "")
            if snapshot_relative:
                snapshot_payload = self._read_json(root / snapshot_relative)
                if isinstance(snapshot_payload, dict) and snapshot_payload:
                    return {"relativePath": snapshot_relative, "snapshot": snapshot_payload}

        return self.find_latest_snapshot(root)

    @staticmethod
    def _read_text_preview(path: Path, *, max_chars: int = 1200) -> str:
        try:
            return read_bounded_text_preview(path, max_chars=max_chars)
        except (OSError, UnicodeDecodeError):
            return ""

    @staticmethod
    def _read_text_tail(path: Path, *, max_chars: int = 1200) -> str:
        try:
            return read_bounded_text_tail(path, max_chars=max_chars)
        except (OSError, UnicodeDecodeError):
            return ""

    @staticmethod
    def _count_text_chars(path: Path, *, chunk_size: int = 64 * 1024) -> int:
        total = 0
        with path.open("r", encoding="utf-8") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                total += len(chunk)
        return total

    @staticmethod
    def _extract_story_terms(*, prompt: str, active_file: str, focus_name: str) -> List[str]:
        candidates: List[str] = []
        for value in (prompt, active_file, Path(active_file).stem, focus_name):
            candidates.extend(_TERM_RE.findall(str(value or "")))

        seen = set()
        ordered: List[str] = []
        for item in candidates:
            normalized = str(item or "").strip().lower()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
        return ordered[:16]

    @staticmethod
    def _score_story_script(
        *,
        relative_path: str,
        content: str,
        terms: List[str],
        active_file: str,
    ) -> float:
        lowered_path = str(relative_path or "").lower()
        lowered_content = str(content or "").lower()
        score = 0.0
        for term in terms:
            if term in lowered_path:
                score += 3.0
            if term in lowered_content:
                score += 1.0
        if active_file and relative_path == active_file:
            score += 20.0
        if lowered_path.endswith(".md"):
            score += 0.25
        return score

    @staticmethod
    def _prompt_section_min_chars(section: str) -> int:
        title = section.splitlines()[0].strip() if section.splitlines() else ""
        if title.startswith(("[Project Relationship Context]", "[Project Fact Context]", "[Foreshadow Ledger]")):
            return 700
        if title.startswith(("[Story Focus]", "[Narrative Agenda]")):
            return 240
        return 220

    @staticmethod
    def _fit_prompt_sections(sections: List[str], *, max_chars: int) -> str:
        normalized_sections = [str(item).strip() for item in sections if str(item).strip()]
        if not normalized_sections:
            return "No story context available."

        selected: List[str] = []
        remaining = max(800, int(max_chars or 6200))
        for index, section in enumerate(normalized_sections):
            if remaining <= 0:
                break
            separator_chars = 2 if selected else 0
            later_sections = normalized_sections[index + 1 :]
            reserve = min(
                max(0, remaining - separator_chars),
                sum(StoryProjectService._prompt_section_min_chars(item) for item in later_sections),
            )
            available = max(0, remaining - separator_chars - reserve)
            if available <= 0:
                continue
            fragment = StoryProjectService._truncate_text(section, max_chars=available)
            if not fragment:
                continue
            selected.append(fragment)
            remaining -= separator_chars + len(fragment)

        bundle = "\n\n".join(selected).strip()
        if bundle:
            return bundle
        return StoryProjectService._truncate_text(normalized_sections[0], max_chars=max(400, int(max_chars or 6200)))

    def _append_failure_index(self, workspace_root: Path, relative_path: str, title: str) -> None:
        index_path = self.agent_root(workspace_root) / "sessions" / "failure-index.md"
        existing = index_path.read_text(encoding="utf-8") if index_path.exists() else _FAILURES_INDEX_TEMPLATE.strip() + "\n"
        line = f"- [{title}]({relative_path.split('/')[-1]})"
        if line in existing:
            return
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(existing.rstrip() + "\n" + line + "\n", encoding="utf-8")

    def _build_snapshot_payload(
        self,
        *,
        previous_snapshot: Dict[str, Any],
        stage2_output: Dict[str, Any],
        segment_relative_path: str,
        operations: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        previous_full_state = previous_snapshot.get("full_state") if isinstance(previous_snapshot.get("full_state"), dict) else {}
        full_state = json.loads(json.dumps(previous_full_state, ensure_ascii=False)) if previous_full_state else {}
        self._apply_operations_to_full_state(full_state=full_state, operations=operations)
        metadata = self._segment_metadata_from_relative_path(segment_relative_path)
        previous_order = previous_snapshot.get("snapshot_order") if isinstance(previous_snapshot.get("snapshot_order"), int) else 0
        now_iso = datetime.now(timezone.utc).isoformat()
        base_revision = int(previous_snapshot.get("revision") or previous_order or 0)
        return {
            "schema_version": 2,
            "change_set_id": str(uuid4()),
            "base_revision": base_revision,
            "revision": base_revision + 1,
            "chapter_id": metadata["chapter_id"],
            "segment_id": metadata["segment_id"],
            "segment_path": segment_relative_path,
            "snapshot_order": previous_order + 1,
            "created_at": now_iso,
            "parent_snapshot": str(previous_snapshot.get("relativePath") or previous_snapshot.get("parent_snapshot") or ""),
            "operations": operations,
            "full_state": full_state,
            "variable_thoughts": stage2_output.get("variable_thoughts", []),
            "memory_updates": stage2_output.get("memory_updates", []),
            "character_updates": stage2_output.get("character_updates", []),
            "event_updates": stage2_output.get("event_updates", []),
            "snapshot_comment": stage2_output.get("snapshot_comment", ""),
        }

    def _read_previous_snapshot(self, workspace_root: Path, segment_relative_path: str) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        segments = self._ordered_segment_paths(root)
        normalized = self._normalize_relative_path(segment_relative_path)
        if not normalized or normalized not in segments:
            return {}
        index = segments.index(normalized)
        for candidate in reversed(segments[:index]):
            snapshot_relative = self.snapshot_relative_path(root, candidate)
            snapshot_path = root / snapshot_relative
            payload = self._read_json(snapshot_path)
            if isinstance(payload, dict) and payload:
                payload["relativePath"] = snapshot_relative
                return payload
        return {}

    def _segment_relative_from_legacy_snapshot(self, workspace_root: Path, snapshot_relative_path: str) -> str:
        root = Path(workspace_root).resolve()
        normalized = self._normalize_relative_path(snapshot_relative_path)
        if not normalized.endswith(".variables.json"):
            return ""
        base_relative = normalized[: -len(".variables.json")]
        for suffix in sorted(_TEXT_SEGMENT_SUFFIXES):
            candidate = root / f"{base_relative}{suffix}"
            if candidate.exists() and candidate.is_file():
                return f"{base_relative}{suffix}"
        return f"{base_relative}.md"

    def _ordered_segment_paths(self, workspace_root: Path) -> List[str]:
        root = Path(workspace_root).resolve()
        segments: List[Tuple[int, int, str]] = []
        for chapter_index, chapter in enumerate(self.list_chapter_states(root), start=1):
            chapter_path = root / chapter.relative_path
            if self._is_story_text_file(chapter_path):
                segment_files = [chapter_path]
            elif chapter_path.is_dir():
                segment_files = self._sorted_segment_files(chapter_path)
            else:
                segment_files = []
            for segment_index, file_path in enumerate(segment_files, start=1):
                relative = file_path.relative_to(root).as_posix()
                sort_number = self._extract_segment_number(file_path.stem) or segment_index
                segments.append((chapter.chapter_number or chapter_index, sort_number, relative))
        segments.sort(key=lambda item: (item[0], item[1], item[2]))
        return [item[2] for item in segments]


    def _sorted_chapter_dirs(self, chapters_root: Path) -> List[Path]:
        return sorted(
            [path for path in chapters_root.iterdir() if path.is_dir()],
            key=lambda path: (
                self._extract_chapter_number(path.name) or 999999,
                path.stat().st_mtime,
                path.name.lower(),
            ),
        )

    def _sorted_flat_chapter_files(self, chapters_root: Path) -> List[Path]:
        return sorted(
            [
                path
                for path in chapters_root.iterdir()
                if self._is_story_text_file(path)
            ],
            key=lambda path: (
                self._extract_chapter_number(path.stem) or 999999,
                path.stat().st_mtime,
                path.name.lower(),
            ),
        )

    def _uses_flat_chapter_files(self, workspace_root: Path) -> bool:
        chapters_root = Path(workspace_root).resolve() / "chapters"
        return chapters_root.exists() and any(self._sorted_flat_chapter_files(chapters_root))

    def _infer_flat_chapter_number_style(self, workspace_root: Path) -> str:
        chapters_root = Path(workspace_root).resolve() / "chapters"
        if not chapters_root.exists():
            return "chinese"
        for path in reversed(self._sorted_flat_chapter_files(chapters_root)):
            if re.match(r"^第\s*\d{1,4}\s*章", path.stem):
                return "arabic"
            if re.match(r"^第\s*[一二三四五六七八九十百千两零〇]+\s*章", path.stem):
                return "chinese"
        return "chinese"

    @staticmethod
    def _is_story_text_file(path: Path) -> bool:
        return (
            path.is_file()
            and path.suffix.lower() in _TEXT_SEGMENT_SUFFIXES
            and path.name.lower() != "readme.md"
        )

    @staticmethod
    def _normalize_max_segments_per_chapter(value: Any) -> int:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            normalized = 3
        return max(1, min(99, normalized))

    @staticmethod
    def _normalize_story_fragment_count(value: Any) -> int:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            normalized = 1
        return max(1, min(20, normalized))

    @staticmethod
    def _normalize_story_fragment_word_count(value: Any) -> int:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            normalized = 2000
        return max(100, min(20000, normalized))

    @staticmethod
    def _normalize_llm_call_count(value: Any, *, fallback: int) -> int:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            normalized = int(fallback or 1)
        return max(1, min(8, normalized))

    @staticmethod
    def _normalize_context_input_tokens(value: Any, *, fallback: int) -> int:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            normalized = int(fallback or 32000)
        return max(4000, min(256000, normalized))

    @staticmethod
    def _normalize_bool(value: Any, *, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        normalized = str(value or "").strip().lower()
        if normalized in {"1", "true", "yes", "on", "y", "是", "开启"}:
            return True
        if normalized in {"0", "false", "no", "off", "n", "否", "关闭"}:
            return False
        return default

    @staticmethod
    def _normalize_consistency_mode(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"block", "strict", "hard"}:
            return "block"
        if normalized in {"off", "disabled", "skip"}:
            return "off"
        return "warn"

    @staticmethod
    def _rewrite_prompt_requests_shortening(prompt: str) -> bool:
        text = str(prompt or "").strip().lower()
        if not text:
            return False
        shortening_terms = (
            "缩短",
            "精简",
            "压缩",
            "删减",
            "简短",
            "短一点",
            "少一点",
            "shorten",
            "shorter",
            "condense",
            "compress",
            "summarize",
        )
        return any(term in text for term in shortening_terms)

    def _collect_relevance_keywords(self, *, root: Path, prompt: str = "", active_file: str = "") -> Set[str]:
        """P2-d 相关性检索：从 prompt + active_file 内容 + 最新 segment 抽取关键词，
        用于给 characters/worldbook 文件相关性打分。项目无关。

        E 项升级：若环境装了 jieba，则优先用 jieba.cut 抽取中文词；
        否则回退到字符 N-gram（2-5 char 滑动窗口）。两者都对 score helper 是同源数据。"""
        keywords: Set[str] = set()
        text_parts: List[str] = []
        if prompt:
            text_parts.append(str(prompt))
        if active_file:
            try:
                active_path = root / self._normalize_relative_path(active_file)
                if active_path.exists() and active_path.is_file():
                    text_parts.append(read_bounded_text_preview(active_path, max_chars=3000))
            except Exception:
                pass
        combined = "\n".join(text_parts)
        if not combined.strip():
            return keywords

        jieba_used = False
        try:
            import jieba  # type: ignore
            for run in re.findall(r"[一-鿿]+", combined):
                for token in jieba.cut(run, cut_all=False):
                    token = token.strip()
                    if len(token) >= 2:
                        keywords.add(token)
            jieba_used = True
        except Exception:
            jieba_used = False

        if not jieba_used:
            for run in re.findall(r"[一-鿿]+", combined):
                for n in (2, 3, 4, 5):
                    if len(run) < n:
                        continue
                    for i in range(len(run) - n + 1):
                        keywords.add(run[i:i + n])

        for item in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,30}", combined):
            keywords.add(item)
        return keywords

    @staticmethod
    def _score_character_path_relevance(path: Path, keywords: Set[str]) -> int:
        if not keywords:
            return 0
        score = 0
        stem = path.stem
        stem_normalized = re.sub(r"^\d{1,3}[_\-]", "", stem)
        for kw in keywords:
            if not kw:
                continue
            if kw in stem_normalized or stem_normalized in kw:
                score += 5
        try:
            preview = read_bounded_text_preview(path, max_chars=3000)
        except Exception:
            preview = ""
        for kw in keywords:
            if kw and kw in preview:
                score += 1
        return score

    @staticmethod
    def _score_worldbook_path_relevance(path: Path, keywords: Set[str]) -> int:
        if not keywords:
            return 0
        score = 0
        stem = path.stem
        stem_normalized = re.sub(r"^\d{1,3}[_\-]", "", stem)
        for kw in keywords:
            if not kw:
                continue
            if kw in stem_normalized or stem_normalized in kw:
                score += 5
        try:
            preview = read_bounded_text_preview(path, max_chars=2500)
        except Exception:
            preview = ""
        for kw in keywords:
            if kw and kw in preview:
                score += 1
        return score

    def _chapter_has_capacity(self, chapter_dir: Path, *, max_segments: int) -> bool:
        segment_count = len(self._sorted_segment_files(chapter_dir))
        return segment_count < max(1, int(max_segments or 1))

    def _sorted_segment_files(self, chapter_dir: Path) -> List[Path]:
        return sorted(
            [
                path
                for path in chapter_dir.iterdir()
                if self._is_story_text_file(path)
            ],
            key=lambda path: (self._extract_segment_number(path.stem) or 999999, path.stat().st_mtime, path.name.lower()),
        )

    def _next_segment_path_in_chapter(self, *, chapter_dir: Path, workspace_root: Path, extension: str) -> str:
        segment_files = self._sorted_segment_files(chapter_dir)
        if not segment_files:
            return chapter_dir.relative_to(workspace_root).as_posix() + f"/{self._default_segment_name(extension)}"

        naming = self._detect_segment_naming(segment_files)
        latest_number = max(self._extract_segment_number(path.stem) or 0 for path in segment_files)
        next_number = latest_number + 1 if latest_number > 0 else len(segment_files) + 1

        if naming["style"] == "seg":
            next_name = f"seg-{next_number:04d}{extension}"
        elif naming["style"] == "prefix":
            width = int(naming.get("width") or 3)
            prefix = str(naming.get("prefix") or "")
            next_name = f"{prefix}{next_number:0{width}d}{extension}"
        else:
            next_name = f"{next_number:03d}{extension}"
        return chapter_dir.relative_to(workspace_root).as_posix() + f"/{next_name}"

    def _detect_segment_naming(self, segment_files: List[Path]) -> Dict[str, Any]:
        numeric = 0
        seg_style = 0
        prefix_samples: List[Tuple[str, int]] = []
        for path in segment_files:
            stem = path.stem
            if _NUMERIC_SEGMENT_RE.match(stem):
                numeric += 1
                continue
            if _SEG_STYLE_RE.match(stem):
                seg_style += 1
                continue
            prefix_match = _PREFIX_NUMBER_RE.match(stem)
            if prefix_match:
                prefix_samples.append((prefix_match.group("prefix"), len(prefix_match.group("number"))))

        if seg_style >= numeric and seg_style >= len(prefix_samples) and seg_style > 0:
            return {"style": "seg", "width": 4}
        if prefix_samples and len(prefix_samples) >= max(2, len(segment_files) // 2):
            prefix, width = prefix_samples[-1]
            return {"style": "prefix", "prefix": prefix, "width": width}
        return {"style": "numeric", "width": 3}

    def suggest_asset_relative_path(
        self,
        workspace_root: Path,
        *,
        category: str,
        prompt: str,
        index: int = 1,
    ) -> str:
        root = Path(workspace_root).resolve()
        normalized_category = str(category or "").strip().lower()
        category_mapping = {
            "character": ("characters", ".json", "角色"),
            "worldbook": ("worldbook", ".md", "设定"),
            "script": ("scripts", ".md", "剧本"),
        }
        child_dir, extension, fallback_label = category_mapping.get(normalized_category, ("scripts", ".md", "条目"))
        target_dir = self.storydex_root(root) / child_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        title = self._extract_asset_title(prompt, fallback=fallback_label)
        naming = self._detect_asset_naming(target_dir)
        sequence = self._next_asset_sequence(target_dir, extension=extension) + max(0, int(index) - 1)
        file_name = self._build_asset_file_name(
            sequence=sequence,
            title=title,
            extension=extension,
            naming=naming,
        )
        return (target_dir / file_name).relative_to(root).as_posix()

    def _refresh_project_naming_skill(self, workspace_root: Path) -> None:
        return
        root = Path(workspace_root).resolve()
        skill_path = self.agent_root(root) / "skills" / "项目命名约定.md"
        content = self._build_project_naming_skill(root)
        if skill_path.exists():
            try:
                if skill_path.read_text(encoding="utf-8") == content:
                    return
            except OSError:
                pass
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(content, encoding="utf-8")

    def _build_project_naming_skill(self, workspace_root: Path) -> str:
        root = Path(workspace_root).resolve()
        chapters_root = root / "chapters"
        chapter_dirs = self._sorted_chapter_dirs(chapters_root) if chapters_root.exists() else []
        segment_detection = self._detect_project_segment_naming(chapter_dirs)
        settings_payload = self._read_json(self.project_settings_path(root))
        settings = self.default_project_settings()
        if isinstance(settings_payload, dict):
            settings["maxSegmentsPerChapter"] = self._normalize_max_segments_per_chapter(
                settings_payload.get("maxSegmentsPerChapter")
            )
        asset_examples = {
            "characters": self._asset_naming_example(root, child_dir="characters", fallback_label="角色", extension=".json"),
            "worldbook": self._asset_naming_example(root, child_dir="worldbook", fallback_label="设定", extension=".md"),
            "scripts": self._asset_naming_example(root, child_dir="scripts", fallback_label="剧本", extension=".md"),
        }

        lines = [
            "# 项目命名约定",
            "",
            "1. 新剧情片段只能创建在 `chapters/<章节目录>/` 下，并优先续写当前未完结章节。",
            "2. 章节目录必须严格遵循“第X章 标题”的格式，X 使用阿拉伯数字（如“第1章 开端”、“第12章 决战”），禁止中文数字（“第一章”）与 `ch001_intro` 之类旧风格。",
            f"3. 当前项目观察到的剧情片段命名风格：{self._describe_segment_naming(segment_detection)}。",
            f"4. 每个章节最多容纳 {self._normalize_max_segments_per_chapter(settings.get('maxSegmentsPerChapter'))} 个剧情片段，写满后必须自动切到下一章。",
            "5. 若无法确认命名风格，默认使用 `001.md` 或 `001.txt`。",
            f"6. 角色文件默认示例：`{asset_examples['characters']}`。",
            f"7. 世界书条目默认示例：`{asset_examples['worldbook']}`。",
            f"8. 剧本条目默认示例：`{asset_examples['scripts']}`。",
            "9. 新建角色、世界书、剧本时，优先使用中文标题并带顺序编号，避免英文 slug 风格。",
        ]

        if chapter_dirs:
            observed = "、".join(path.name for path in chapter_dirs[:3])
            lines.extend(["", f"当前章节目录样例：{observed}"])

        return "\n".join(lines).rstrip() + "\n"

    def _detect_project_segment_naming(self, chapter_dirs: List[Path]) -> Dict[str, Any]:
        detected_samples: List[Dict[str, Any]] = []
        for chapter_dir in chapter_dirs:
            segment_files = self._sorted_segment_files(chapter_dir)
            if not segment_files:
                continue
            detected_samples.append(self._detect_segment_naming(segment_files))
            if len(detected_samples) >= 3:
                break
        if not detected_samples:
            return {"style": "numeric", "width": 3}

        seg_count = sum(1 for item in detected_samples if item.get("style") == "seg")
        prefix_count = sum(1 for item in detected_samples if item.get("style") == "prefix")
        numeric_count = sum(1 for item in detected_samples if item.get("style") == "numeric")

        if seg_count >= max(prefix_count, numeric_count):
            return {"style": "seg", "width": 4}
        if prefix_count > max(seg_count, numeric_count):
            for item in reversed(detected_samples):
                if item.get("style") == "prefix":
                    return item
        return {"style": "numeric", "width": 3}

    def _describe_segment_naming(self, naming: Dict[str, Any]) -> str:
        style = str(naming.get("style") or "numeric").strip().lower()
        if style == "seg":
            return "`seg-0001` 递增"
        if style == "prefix":
            prefix = str(naming.get("prefix") or "片段-")
            width = int(naming.get("width") or 3)
            example = f"{prefix}{1:0{width}d}"
            return f"`{example}` 递增"
        width = int(naming.get("width") or 3)
        example = f"{1:0{width}d}"
        return f"`{example}` 纯数字递增"

    def _asset_naming_example(self, workspace_root: Path, *, child_dir: str, fallback_label: str, extension: str) -> str:
        target_dir = self.storydex_root(workspace_root) / child_dir
        naming = self._detect_asset_naming(target_dir)
        example_title = {
            "characters": "林拾烟",
            "worldbook": "雾汐港",
            "scripts": "灯塔夜巡",
        }.get(child_dir, fallback_label)
        return (
            f".storydex/{child_dir}/"
            f"{self._build_asset_file_name(sequence=1, title=example_title, extension=extension, naming=naming)}"
        )

    def _detect_asset_naming(self, target_dir: Path) -> Dict[str, Any]:
        if target_dir.exists():
            for path in sorted([item for item in target_dir.iterdir() if item.is_file()], key=lambda item: item.name.lower()):
                match = re.match(r"^(?P<number>\d{2,3})(?P<separator>[_\-\s]?)(?P<title>.+)$", path.stem)
                if not match:
                    continue
                separator = match.group("separator") or "_"
                return {
                    "width": len(match.group("number")),
                    "separator": separator,
                }
        return {"width": 2, "separator": "_"}

    def _next_asset_sequence(self, target_dir: Path, *, extension: str) -> int:
        highest = 0
        if target_dir.exists():
            for path in target_dir.iterdir():
                if not path.is_file() or path.suffix.lower() != extension.lower():
                    continue
                match = re.match(r"^(?P<number>\d{2,3})", path.stem)
                if not match:
                    continue
                try:
                    highest = max(highest, int(match.group("number")))
                except ValueError:
                    continue
        return highest + 1

    def _build_asset_file_name(
        self,
        *,
        sequence: int,
        title: str,
        extension: str,
        naming: Dict[str, Any],
    ) -> str:
        width = max(2, int(naming.get("width") or 2))
        separator = str(naming.get("separator") or "_")
        normalized_title = self._sanitize_asset_title(title)
        return f"{sequence:0{width}d}{separator}{normalized_title}{extension}"

    def _extract_asset_title(self, prompt: str, *, fallback: str) -> str:
        text = str(prompt or "").strip()
        patterns = [
            r"(?:创建|新建|补充|生成|完善|整理|编写|写一份|写个|制作)([\u4e00-\u9fff]{2,16}?)(?:的?(?:详细)?(?:角色卡|角色档案|角色|世界书条目|世界书|设定条目|设定|剧本条目|剧本|脚本条目|脚本))",
            r"(?:关于|围绕|聚焦)([\u4e00-\u9fff]{2,16})",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                candidate = self._sanitize_asset_title(match.group(1))
                if candidate:
                    return candidate

        tokens = re.findall(r"[\u4e00-\u9fff]{2,16}", text)
        stop_words = {
            "创建",
            "新建",
            "补充",
            "生成",
            "完善",
            "整理",
            "详细",
            "角色卡",
            "角色档案",
            "角色",
            "世界书条目",
            "世界书",
            "设定条目",
            "设定",
            "剧本条目",
            "剧本",
            "脚本条目",
            "脚本",
            "包含背景",
            "背景目标",
            "当前状态",
        }
        for token in tokens:
            candidate = self._sanitize_asset_title(token)
            if candidate and candidate not in stop_words:
                return candidate
        return fallback

    def _sanitize_asset_title(self, raw: str) -> str:
        normalized = str(raw or "").strip()
        normalized = normalized.replace("/", " ").replace("\\", " ")
        normalized = _INVALID_FILE_CHARS.sub("", normalized)
        normalized = re.sub(r"\s+", "", normalized)
        normalized = re.sub(r"^(第[0-9一二三四五六七八九十百千两零〇]+章)", "", normalized)
        normalized = re.sub(r"^(角色|世界书|设定|剧本|脚本|条目)+", "", normalized)
        normalized = normalized.strip("._- ")
        return normalized or "未命名条目"

    def _sync_character_files_from_snapshot_payload(
        self,
        workspace_root: Path,
        *,
        snapshot_relative_path: str,
        payload: Dict[str, Any],
        full_state: Dict[str, Any],
        updated_at: str,
    ) -> List[str]:
        root = Path(workspace_root).resolve()
        updates = self._normalize_character_updates(payload.get("character_updates"))
        cast_state = full_state.get("cast") if isinstance(full_state.get("cast"), dict) else {}
        chapter_relative_path = ""
        segment_relative_path = str(payload.get("segment_path") or "").strip()

        if not segment_relative_path:
            segment_relative_path = self._segment_relative_from_snapshot_path(snapshot_relative_path)
        if segment_relative_path.startswith("chapters/"):
            chapter_relative_path = str(Path(segment_relative_path).parent.as_posix())

        written_paths: List[str] = []
        seen_paths: set[str] = set()
        for update in updates:
            character_name = str(update.get("character") or "").strip()
            if not character_name:
                continue
            state_from_cast = cast_state.get(character_name) if isinstance(cast_state.get(character_name), dict) else {}
            character_path = self._resolve_character_file_path(root, character_name)
            existing_payload = self._read_json(character_path)
            merged_payload = self._merge_character_payload(
                existing_payload=existing_payload,
                update=update,
                character_name=character_name,
                snapshot_relative_path=snapshot_relative_path,
                segment_relative_path=segment_relative_path,
                chapter_relative_path=chapter_relative_path,
                updated_at=updated_at,
                cast_state=state_from_cast,
            )
            serialized = json.dumps(merged_payload, ensure_ascii=False, indent=2) + "\n"
            current_text = character_path.read_text(encoding="utf-8") if character_path.exists() else ""
            if current_text != serialized:
                character_path.parent.mkdir(parents=True, exist_ok=True)
                character_path.write_text(serialized, encoding="utf-8")
            relative_path = character_path.relative_to(root).as_posix()
            if relative_path not in seen_paths:
                written_paths.append(relative_path)
                seen_paths.add(relative_path)
        return written_paths

    def _normalize_character_updates(self, value: Any) -> List[Dict[str, Any]]:
        if not isinstance(value, list):
            return []

        normalized: List[Dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            character_name = str(
                item.get("character")
                or item.get("name")
                or item.get("characterName")
                or item.get("target")
                or ""
            ).strip()
            if not character_name:
                continue

            raw_state = item.get("state")
            if not isinstance(raw_state, dict):
                raw_state = item.get("current_state")
            if not isinstance(raw_state, dict):
                raw_state = {}
            if item.get("status") and not raw_state.get("status"):
                raw_state["status"] = str(item.get("status") or "").strip()
            if item.get("emotion") and not raw_state.get("emotion"):
                raw_state["emotion"] = str(item.get("emotion") or "").strip()
            if item.get("location") and not raw_state.get("location"):
                raw_state["location"] = str(item.get("location") or "").strip()
            if item.get("goal") and not raw_state.get("goal"):
                raw_state["goal"] = str(item.get("goal") or "").strip()

            normalized.append(
                {
                    "action": str(item.get("action") or item.get("op") or "upsert").strip().lower() or "upsert",
                    "character": character_name,
                    "aliases": self._coerce_text_list(item.get("aliases")),
                    "role": str(item.get("role") or "").strip(),
                    "summary": str(item.get("summary") or "").strip(),
                    "appearance": str(item.get("appearance") or "").strip(),
                    "personality": str(item.get("personality") or "").strip(),
                    "background": str(item.get("background") or "").strip(),
                    "motivation": str(item.get("motivation") or "").strip(),
                    "relationships": self._normalize_relationships(item.get("relationships")),
                    "notes": self._coerce_text_list(item.get("notes")),
                    "state": raw_state,
                    "changes": self._coerce_text_list(item.get("changes")),
                    "evidence": str(item.get("evidence") or "").strip(),
                }
            )
        return normalized

    def _normalize_relationships(self, value: Any) -> List[Dict[str, Any]]:
        if not isinstance(value, list):
            return []
        relationships: List[Dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                target = str(item.get("target") or item.get("character") or "").strip()
                relation = str(item.get("relation") or item.get("status") or item.get("summary") or "").strip()
                if not target and not relation:
                    continue
                relationships.append(
                    {
                        "target": target,
                        "relation": relation,
                        "detail": str(item.get("detail") or item.get("description") or "").strip(),
                    }
                )
                continue
            text = str(item or "").strip()
            if text:
                relationships.append({"target": "", "relation": text, "detail": ""})
        return relationships

    def _resolve_character_file_path(self, workspace_root: Path, character_name: str) -> Path:
        root = Path(workspace_root).resolve()
        existing_path = self._find_character_file_by_name(root, character_name)
        if existing_path is not None:
            return existing_path
        relative_path = self.suggest_asset_relative_path(
            root,
            category="character",
            prompt=f"创建{character_name}的详细角色档案，包含外貌、性格、背景、动机、关系与当前状态。",
        )
        return root / relative_path

    def _find_character_file_by_name(self, workspace_root: Path, character_name: str) -> Optional[Path]:
        root = Path(workspace_root).resolve()
        character_root = self.storydex_root(root) / "characters"
        if not character_root.exists():
            return None

        lookup_key = self._normalize_character_lookup_key(character_name)
        if not lookup_key:
            return None

        for candidate in sorted(character_root.glob("*.json"), key=lambda item: item.name.lower()):
            payload = self._read_json(candidate)
            names = [
                str(payload.get("name") or "").strip(),
                self._strip_asset_sequence(candidate.stem),
            ]
            names.extend(self._coerce_text_list(payload.get("aliases")))
            for name in names:
                if self._normalize_character_lookup_key(name) == lookup_key:
                    return candidate
        return None

    def _merge_character_payload(
        self,
        *,
        existing_payload: Dict[str, Any],
        update: Dict[str, Any],
        character_name: str,
        snapshot_relative_path: str,
        segment_relative_path: str,
        chapter_relative_path: str,
        updated_at: str,
        cast_state: Dict[str, Any],
    ) -> Dict[str, Any]:
        payload = dict(existing_payload) if isinstance(existing_payload, dict) else {}
        is_new_character = not bool(payload)
        aliases = self._merge_text_lists(
            self._coerce_text_list(payload.get("aliases")),
            [character_name] + self._coerce_text_list(update.get("aliases")),
        )
        relationships = self._merge_relationship_lists(payload.get("relationships"), update.get("relationships"))
        notes = self._merge_text_lists(self._coerce_text_list(payload.get("notes")), self._coerce_text_list(update.get("notes")))
        changes = self._coerce_text_list(update.get("changes"))
        evidence_text = str(update.get("evidence") or "").strip()

        merged_payload: Dict[str, Any] = {
            "name": character_name,
            "aliases": [item for item in aliases if item and item != character_name],
            "role": self._character_field_text(update.get("role"), payload.get("role"), is_new_character=is_new_character),
            "summary": self._character_field_text(update.get("summary"), payload.get("summary"), is_new_character=False),
            "appearance": self._character_field_text(update.get("appearance"), payload.get("appearance"), is_new_character=is_new_character),
            "personality": self._character_field_text(update.get("personality"), payload.get("personality"), is_new_character=is_new_character),
            "background": self._character_field_text(update.get("background"), payload.get("background"), is_new_character=is_new_character),
            "motivation": self._character_field_text(update.get("motivation"), payload.get("motivation"), is_new_character=is_new_character),
            "relationships": relationships,
            "notes": notes,
            "state": self._merge_character_state(
                existing_state=payload.get("state"),
                update_state=update.get("state"),
                cast_state=cast_state,
            ),
        }
        if not merged_payload["summary"]:
            merged_payload["summary"] = self._infer_character_summary(update=update, cast_state=cast_state)
        if not merged_payload["summary"] and is_new_character:
            merged_payload["summary"] = _UNKNOWN_CHARACTER_FIELD_VALUE

        story_tracking = payload.get("storyTracking") if isinstance(payload.get("storyTracking"), dict) else {}
        if not story_tracking.get("firstSeenSegment"):
            story_tracking["firstSeenSegment"] = segment_relative_path
        if not story_tracking.get("firstSeenSnapshotPath"):
            story_tracking["firstSeenSnapshotPath"] = snapshot_relative_path
        story_tracking["latestSegment"] = segment_relative_path
        story_tracking["latestSnapshotPath"] = snapshot_relative_path
        story_tracking["activeChapter"] = chapter_relative_path
        story_tracking["updatedAt"] = updated_at
        merged_payload["storyTracking"] = story_tracking

        evidence_history = payload.get("evidence") if isinstance(payload.get("evidence"), list) else []
        change_history = payload.get("changeLog") if isinstance(payload.get("changeLog"), list) else []
        if evidence_text:
            evidence_history = self._append_limited_records(
                evidence_history,
                {
                    "updatedAt": updated_at,
                    "segmentPath": segment_relative_path,
                    "snapshotPath": snapshot_relative_path,
                    "evidence": evidence_text,
                },
            )
        if changes or evidence_text:
            change_history = self._append_limited_records(
                change_history,
                {
                    "updatedAt": updated_at,
                    "segmentPath": segment_relative_path,
                    "snapshotPath": snapshot_relative_path,
                    "changes": changes,
                    "evidence": evidence_text,
                },
            )
        merged_payload["evidence"] = evidence_history
        merged_payload["changeLog"] = change_history
        return merged_payload

    @staticmethod
    def _character_field_text(*values: Any, is_new_character: bool) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return _UNKNOWN_CHARACTER_FIELD_VALUE if is_new_character else ""

    def _merge_character_state(
        self,
        *,
        existing_state: Any,
        update_state: Any,
        cast_state: Any,
    ) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        for source in (existing_state, cast_state, update_state):
            if not isinstance(source, dict):
                continue
            for key, value in source.items():
                if value in (None, "", [], {}):
                    continue
                if isinstance(value, list):
                    merged[key] = self._merge_text_lists(merged.get(key), value)
                elif isinstance(value, dict):
                    base = merged.get(key) if isinstance(merged.get(key), dict) else {}
                    merged[key] = {**base, **value}
                else:
                    merged[key] = value
        return merged

    def _infer_character_summary(self, *, update: Dict[str, Any], cast_state: Dict[str, Any]) -> str:
        summary_candidates = [
            str(update.get("role") or "").strip(),
            str(update.get("appearance") or "").strip(),
            str(update.get("personality") or "").strip(),
            str(update.get("background") or "").strip(),
            str(update.get("motivation") or "").strip(),
        ]
        summary = "；".join(item for item in summary_candidates if item)
        if summary:
            return summary[:240]

        state_fragments: List[str] = []
        if isinstance(cast_state, dict):
            for key in ("status", "emotion", "location", "goal"):
                text = str(cast_state.get(key) or "").strip()
                if text:
                    state_fragments.append(f"{key}: {text}")
        change_fragments = self._coerce_text_list(update.get("changes"))
        return "；".join(state_fragments + change_fragments)[:240]

    def _coerce_text_list(self, value: Any) -> List[str]:
        if isinstance(value, list):
            results: List[str] = []
            for item in value:
                text = str(item or "").strip()
                if text:
                    results.append(text)
            return results
        text = str(value or "").strip()
        return [text] if text else []

    def _merge_text_lists(self, existing: Any, incoming: Any) -> List[str]:
        merged: List[str] = []
        seen: set[str] = set()
        for source in (existing, incoming):
            if not isinstance(source, list):
                source = self._coerce_text_list(source)
            for item in source:
                text = str(item or "").strip()
                if not text:
                    continue
                key = text.casefold()
                if key in seen:
                    continue
                seen.add(key)
                merged.append(text)
        return merged

    def _merge_relationship_lists(self, existing: Any, incoming: Any) -> List[Dict[str, Any]]:
        normalized_existing = self._normalize_relationships(existing)
        normalized_incoming = self._normalize_relationships(incoming)
        merged: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in normalized_existing + normalized_incoming:
            normalized_item = {
                "target": str(item.get("target") or "").strip(),
                "relation": str(item.get("relation") or "").strip(),
                "detail": str(item.get("detail") or "").strip(),
            }
            if not any(normalized_item.values()):
                continue
            key = json.dumps(normalized_item, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            merged.append(normalized_item)
        return merged

    def _append_limited_records(
        self,
        existing: List[Dict[str, Any]],
        entry: Dict[str, Any],
        *,
        limit: int = 12,
    ) -> List[Dict[str, Any]]:
        normalized_existing = [dict(item) for item in existing if isinstance(item, dict)]
        serialized_existing = {
            json.dumps(item, ensure_ascii=False, sort_keys=True)
            for item in normalized_existing
        }
        candidate = dict(entry)
        candidate_key = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
        if candidate_key not in serialized_existing:
            normalized_existing.append(candidate)
        return normalized_existing[-max(1, int(limit or 1)) :]

    @staticmethod
    def _normalize_character_lookup_key(value: str) -> str:
        normalized = str(value or "").strip().lower()
        normalized = re.sub(r"^\d{2,3}[_\-\s]*", "", normalized)
        normalized = re.sub(r"\s+", "", normalized)
        return normalized

    @staticmethod
    def _strip_asset_sequence(stem: str) -> str:
        return re.sub(r"^\d{2,3}[_\-\s]*", "", str(stem or "").strip())

    def _segment_relative_from_snapshot_path(self, snapshot_relative_path: str) -> str:
        normalized = self._normalize_relative_path(snapshot_relative_path)
        prefix = f"{self.settings.storydex_dir_name}/memory/chapters/"
        if not normalized.startswith(prefix) or not normalized.endswith(".variables.json"):
            return ""
        base_relative = normalized[len(prefix) : -len(".variables.json")]
        return (Path("chapters") / f"{base_relative}.md").as_posix()

    def _resolve_active_chapter_relative(self, active_file: str) -> str:
        normalized = self._normalize_relative_path(active_file)
        if not normalized or not normalized.startswith("chapters/"):
            return ""
        parts = Path(normalized).parts
        if len(parts) < 2:
            return ""
        if len(parts) == 2 and Path(normalized).suffix.lower() in _TEXT_SEGMENT_SUFFIXES:
            return normalized
        return Path(*parts[:2]).as_posix()

    @staticmethod
    def _extract_chapter_number(name: str) -> int:
        normalized = str(name or "").strip()
        match = _CHAPTER_NUMBER_RE.match(normalized)
        if match:
            return StoryProjectService._parse_chapter_number(match.group(1))
        digit_match = re.search(r"(\d{1,4})", normalized)
        if digit_match:
            try:
                return int(digit_match.group(1))
            except ValueError:
                return 0
        return 0

    @staticmethod
    def _parse_chapter_number(raw: str) -> int:
        text = str(raw or "").strip()
        if not text:
            return 0
        if text.isdigit():
            return int(text)
        numerals = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        if text == "十":
            return 10
        total = 0
        if "百" in text:
            parts = text.split("百", 1)
            total += numerals.get(parts[0] or "一", 1) * 100
            text = parts[1]
        if "十" in text:
            parts = text.split("十", 1)
            total += numerals.get(parts[0] or "一", 1) * 10
            text = parts[1]
        if text:
            total += numerals.get(text, 0)
        return total

    @staticmethod
    def _number_to_chinese(value: int) -> str:
        digits = "零一二三四五六七八九"
        if value <= 0:
            return "零"
        if value < 10:
            return digits[value]
        if value < 20:
            return "十" + (digits[value % 10] if value % 10 else "")
        if value < 100:
            tens, ones = divmod(value, 10)
            return digits[tens] + "十" + (digits[ones] if ones else "")
        hundreds, rem = divmod(value, 100)
        if rem == 0:
            return digits[hundreds] + "百"
        if rem < 10:
            return digits[hundreds] + "百零" + digits[rem]
        return digits[hundreds] + "百" + StoryProjectService._number_to_chinese(rem)

    def _build_chapter_display_name(self, raw_name: str, fallback_number: int = 0) -> str:
        name = str(raw_name or "").strip()
        chapter_number = self._extract_chapter_number(name) or fallback_number or 1
        title = name
        title = re.sub(r"^(?:第\s*[0-9一二三四五六七八九十百千两零〇]+\s*章)", "", title).strip()
        title = re.sub(r"^[A-Za-z]{0,3}\d{1,4}[_\-\s]*", "", title).strip()
        title = title.replace("_", " ").replace("-", " ").strip()
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            title = "未命名"
        # 规范名统一用阿拉伯数字（与默认模板「第1章 未命名」一致）。
        # 旧版用中文数字导致归一重命名与 LLM 落盘路径（阿拉伯）互相打架，
        # 每轮生成都会留下一个同名空目录。
        return f"第{chapter_number}章 {title}"

    def _build_new_chapter_name(self, chapter_number: int, title: str = "未命名", *, number_style: str = "arabic") -> str:
        chapter_number_text = str(chapter_number) if number_style == "arabic" else self._number_to_chinese(chapter_number)
        return f"第{chapter_number_text}章 {self._sanitize_chapter_title(title)}"

    def _chapter_name_from_template(self, template: Dict[str, Any], *, number: int, title: str) -> str:
        pattern = str(template.get("chapterNamePattern") or "第X章 标题").strip() or "第X章 标题"
        safe_title = self._sanitize_chapter_title(title)
        name = pattern.replace("X", str(number)).replace("{number}", str(number)).replace("{title}", safe_title)
        name = name.replace("标题", safe_title)
        return self._safe_template_path_part(name, fallback=f"第{number}章 {safe_title}")

    def _sanitize_chapter_title(self, raw: str) -> str:
        normalized = str(raw or "").strip()
        normalized = normalized.replace("/", " ").replace("\\", " ")
        normalized = _INVALID_FILE_CHARS.sub("", normalized)
        normalized = re.sub(r"^(?:第\s*[0-9一二三四五六七八九十百千两零〇]+\s*章)", "", normalized).strip()
        normalized = normalized.replace("_", " ").replace("-", " ")
        normalized = re.sub(r"\s+", " ", normalized).strip(" ._-")
        return normalized or "未命名"

    @staticmethod
    def _safe_template_path_part(value: str, *, fallback: str) -> str:
        normalized = str(value or "").strip().replace("\\", "/")
        normalized = normalized.split("/")[-1].strip()
        normalized = _INVALID_FILE_CHARS.sub("", normalized).strip()
        if not normalized or normalized in {".", ".."}:
            normalized = fallback
        return normalized

    @staticmethod
    def _safe_int(value: Any, *, fallback: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = fallback
        return max(minimum, min(maximum, parsed))

    def _suggest_new_chapter_title(
        self,
        workspace_root: Path,
        *,
        chapter_number: int,
        prompt: str,
        active_file: str = "",
    ) -> str:
        direct_title = self._extract_requested_chapter_title(prompt)
        if direct_title:
            return direct_title

        normalized_prompt = str(prompt or "").strip()
        if not normalized_prompt:
            return "未命名"

        del workspace_root, chapter_number, active_file
        return self._extract_prompt_topic(prompt)

    @staticmethod
    def _extract_requested_chapter_title(prompt: str) -> str:
        normalized_prompt = str(prompt or "").strip()
        patterns = [
            r"(?:章节名|章名|本章名|标题)[：:\s]*[《“\"]?(?P<title>[\u4e00-\u9fffA-Za-z0-9\s]{2,20})[》”\"]?",
            r"[《“\"](?P<title>[\u4e00-\u9fffA-Za-z0-9\s]{2,20})[》”\"]",
        ]
        for pattern in patterns:
            match = re.search(pattern, normalized_prompt)
            if not match:
                continue
            title = StoryProjectService._sanitize_static_chapter_title(match.group("title"))
            if title and title != "未命名":
                return title
        return ""

    @staticmethod
    def _extract_prompt_topic(prompt: str) -> str:
        normalized_prompt = str(prompt or "").strip()
        tokens = re.findall(r"[\u4e00-\u9fff]{2,12}", normalized_prompt)
        stop_words = {
            "继续",
            "续写",
            "生成",
            "创建",
            "新建",
            "剧情",
            "片段",
            "章节",
            "故事",
            "内容",
            "推进",
            "发展",
            "场景",
            "玩家",
            "角色",
            "世界书",
            "设定",
            "剧本",
            "预设",
            "记忆",
            "当前",
            "这个",
            "那个",
        }
        for token in tokens:
            title = StoryProjectService._sanitize_static_chapter_title(token)
            if title and title not in stop_words:
                return title
        return "未命名"

    @staticmethod
    def _sanitize_static_chapter_title(raw: str) -> str:
        normalized = str(raw or "").strip()
        normalized = normalized.replace("/", " ").replace("\\", " ")
        normalized = _INVALID_FILE_CHARS.sub("", normalized)
        normalized = re.sub(r"^(?:第\s*[0-9一二三四五六七八九十百千两零〇]+\s*章)", "", normalized).strip()
        normalized = normalized.replace("_", " ").replace("-", " ")
        normalized = re.sub(r"\s+", " ", normalized).strip(" ._-")
        return normalized or "未命名"

    def _normalize_chapter_directories(self, workspace_root: Path) -> Dict[str, str]:
        root = Path(workspace_root).resolve()
        chapters_root = root / "chapters"
        if not chapters_root.exists():
            return {}

        with self._lock:
            self._prune_duplicate_empty_chapter_dirs(root)
            chapter_dirs = self._sorted_chapter_dirs(chapters_root)
            pending_renames: List[Tuple[Path, Path]] = []
            reserved_targets = {path.name for path in chapter_dirs}

            for index, chapter_dir in enumerate(chapter_dirs, start=1):
                canonical_name = self._build_chapter_display_name(
                    chapter_dir.name,
                    fallback_number=self._extract_chapter_number(chapter_dir.name) or index,
                )
                if canonical_name == chapter_dir.name:
                    continue
                if canonical_name in reserved_targets:
                    continue
                target_dir = chapters_root / canonical_name
                pending_renames.append((chapter_dir, target_dir))
                reserved_targets.add(canonical_name)

            if not pending_renames:
                return {}

            chapter_mapping: Dict[str, str] = {}
            snapshot_root = self.storydex_root(root) / "memory" / "chapters"
            for source_dir, target_dir in pending_renames:
                source_relative = source_dir.relative_to(root).as_posix()
                target_relative = target_dir.relative_to(root).as_posix()
                source_dir.rename(target_dir)
                chapter_mapping[source_relative] = target_relative
                self._move_snapshot_chapter_dir(snapshot_root / source_dir.name, snapshot_root / target_dir.name)

            if chapter_mapping:
                self._rewrite_chapter_progress_after_rename(root, chapter_mapping)
                self._rewrite_story_state_after_chapter_rename(root, chapter_mapping)
            return chapter_mapping

    @staticmethod
    def _rewrite_segment_path_for_chapter_mapping(
        segment_relative_path: str,
        chapter_mapping: Dict[str, str],
    ) -> str:
        """Rewrite a segment path when its chapter directory was renamed."""
        normalized = str(segment_relative_path or "").replace("\\", "/").strip("/")
        if not normalized or not chapter_mapping:
            return normalized
        path = Path(normalized)
        if len(path.parts) < 3 or path.parts[0] != "chapters":
            return normalized
        chapter_relative = Path(*path.parts[:2]).as_posix()
        visited: Set[str] = set()
        while chapter_relative in chapter_mapping and chapter_relative not in visited:
            visited.add(chapter_relative)
            chapter_relative = str(chapter_mapping[chapter_relative])
        if chapter_relative == Path(*path.parts[:2]).as_posix():
            return normalized
        return (Path(chapter_relative) / Path(*path.parts[2:])).as_posix()

    @staticmethod
    def _move_snapshot_chapter_dir(source_dir: Path, target_dir: Path) -> None:
        if not source_dir.exists():
            return
        if not target_dir.exists():
            source_dir.rename(target_dir)
            return
        target_dir.mkdir(parents=True, exist_ok=True)
        for child in list(source_dir.iterdir()):
            destination = target_dir / child.name
            if destination.exists():
                continue
            child.rename(destination)
        try:
            source_dir.rmdir()
        except OSError:
            pass

    def _rewrite_chapter_progress_after_rename(self, workspace_root: Path, chapter_mapping: Dict[str, str]) -> None:
        progress_path = self.chapter_progress_path(workspace_root)
        payload = self._read_json(progress_path)
        if not isinstance(payload, dict):
            return
        chapters = payload.get("chapters") if isinstance(payload.get("chapters"), dict) else {}
        rewritten: Dict[str, Any] = {}
        for key, value in chapters.items():
            normalized_key = self._normalize_relative_path(str(key or ""))
            if not normalized_key:
                continue
            next_key = chapter_mapping.get(normalized_key, normalized_key)
            entry = dict(value) if isinstance(value, dict) else {}
            entry["displayName"] = self._build_chapter_display_name(Path(next_key).name)
            rewritten[next_key] = entry
        payload["chapters"] = rewritten
        payload["updatedAt"] = datetime.now(timezone.utc).isoformat()
        progress_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _rewrite_story_state_after_chapter_rename(self, workspace_root: Path, chapter_mapping: Dict[str, str]) -> None:
        current_state_root = self.storydex_root(workspace_root) / "memory" / "current-state"
        for json_path in current_state_root.glob("*.json"):
            self._rewrite_story_json_file(json_path, chapter_mapping)

        snapshot_root = self.storydex_root(workspace_root) / "memory" / "chapters"
        for snapshot_path in snapshot_root.rglob("*.json"):
            self._rewrite_story_json_file(snapshot_path, chapter_mapping)

    def _rewrite_story_json_file(self, path: Path, chapter_mapping: Dict[str, str]) -> None:
        payload = self._read_json(path)
        if not isinstance(payload, dict):
            return
        rewritten = self._rewrite_story_payload(payload, chapter_mapping)
        if rewritten == payload:
            return
        path.write_text(json.dumps(rewritten, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _rewrite_story_payload(self, payload: Dict[str, Any], chapter_mapping: Dict[str, str]) -> Dict[str, Any]:
        snapshot_mapping = self._build_snapshot_path_mapping(chapter_mapping)
        chapter_name_mapping = {
            Path(source).name: Path(target).name
            for source, target in chapter_mapping.items()
        }

        def rewrite(value: Any, *, key: str = "") -> Any:
            if isinstance(value, dict):
                return {item_key: rewrite(item_value, key=item_key) for item_key, item_value in value.items()}
            if isinstance(value, list):
                return [rewrite(item, key=key) for item in value]
            if not isinstance(value, str):
                return value

            normalized = value.strip()
            if key in {"latestSnapshotPath", "snapshotPath", "snapshot_path", "parent_snapshot"}:
                for source_prefix, target_prefix in snapshot_mapping.items():
                    if normalized.startswith(source_prefix):
                        return target_prefix + normalized[len(source_prefix) :]
                return value
            if key in {"segmentPath", "segment_path"}:
                for source_prefix, target_prefix in chapter_mapping.items():
                    source_prefix = f"{source_prefix}/"
                    target_prefix = f"{target_prefix}/"
                    if normalized.startswith(source_prefix):
                        return target_prefix + normalized[len(source_prefix) :]
                return value
            if key in {"chapter_id", "chapterId"}:
                return chapter_name_mapping.get(normalized, value)
            return value

        return rewrite(payload)

    def _build_snapshot_path_mapping(self, chapter_mapping: Dict[str, str]) -> Dict[str, str]:
        snapshot_mapping: Dict[str, str] = {}
        for source, target in chapter_mapping.items():
            snapshot_mapping[
                f"{self.settings.storydex_dir_name}/memory/chapters/{Path(source).name}/"
            ] = f"{self.settings.storydex_dir_name}/memory/chapters/{Path(target).name}/"
        return snapshot_mapping

    @staticmethod
    def _default_segment_name(extension: str) -> str:
        return f"001{extension}"

    @staticmethod
    def _extract_segment_number(stem: str) -> int:
        raw = str(stem or "").strip()
        match = _NUMERIC_SEGMENT_RE.match(raw)
        if match:
            return int(match.group("number"))
        match = _SEG_STYLE_RE.match(raw)
        if match:
            return int(match.group("number"))
        match = _PREFIX_NUMBER_RE.match(raw)
        if match:
            try:
                return int(match.group("number"))
            except ValueError:
                return 0
        return 0

    @staticmethod
    def _segment_metadata_from_relative_path(segment_relative_path: str) -> Dict[str, str]:
        normalized_path = str(segment_relative_path or "").strip().replace("\\", "/")
        path = Path(normalized_path)
        chapter_id = path.stem if len(path.parts) == 2 and path.parent.as_posix() == "chapters" else path.parent.name
        return {
            "chapter_id": chapter_id or "第一章 未命名",
            "segment_id": path.stem or "001",
        }

    def _normalize_story_increment_fragments(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        raw_fragments = payload.get("fragments") if isinstance(payload.get("fragments"), list) else []
        fragments = [dict(item) for item in raw_fragments if isinstance(item, dict)]
        if fragments:
            return fragments
        fallback: Dict[str, Any] = {}
        for source_key, target_key in (
            ("segmentPath", "path"),
            ("segment_path", "path"),
            ("path", "path"),
            ("segmentText", "text"),
            ("segment_text", "text"),
            ("text", "text"),
            ("content", "text"),
        ):
            value = payload.get(source_key)
            if value not in (None, ""):
                fallback[target_key] = value
        return [fallback] if fallback else []

    @staticmethod
    def _story_increment_fragment_text(fragment: Dict[str, Any]) -> str:
        for key in ("text", "segmentText", "segment_text", "content"):
            value = fragment.get(key)
            if value not in (None, ""):
                return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
        return ""

    def _resolve_story_increment_segment_path(
        self,
        workspace_root: Path,
        fragment: Dict[str, Any],
        *,
        active_file: str,
        prompt: str,
        settings: Dict[str, Any],
    ) -> str:
        root = Path(workspace_root).resolve()
        raw_path = str(
            fragment.get("path")
            or fragment.get("segmentPath")
            or fragment.get("segment_path")
            or ""
        ).strip()
        if not raw_path and active_file.startswith("chapters/"):
            raw_path = active_file
        if not raw_path:
            raw_path = self.compute_next_segment_path(root, active_file=active_file, prompt=prompt)

        normalized = self._normalize_relative_path(raw_path)
        path = Path(normalized)
        extension = "." + self._normalize_story_segment_format(settings.get("storySegmentFormat"))
        if not path.suffix:
            normalized = f"{normalized.rstrip('/')}{extension}"
            path = Path(normalized)
        if not normalized.startswith("chapters/") or path.suffix.lower() not in _TEXT_SEGMENT_SUFFIXES:
            raise StoryProjectServiceError(
                "Story increment segment path must point to a chapters/*.md or chapters/*.txt file.",
                code="story_increment_segment_path_invalid",
                details={"segmentPath": normalized},
            )
        if any(part in {"", ".", ".."} for part in path.parts):
            raise StoryProjectServiceError(
                "Story increment segment path contains unsafe path parts.",
                code="story_increment_segment_path_invalid",
                details={"segmentPath": normalized},
            )
        candidate = (root / normalized).resolve()
        chapters_root = (root / "chapters").resolve()
        try:
            candidate.relative_to(chapters_root)
        except ValueError as exc:
            raise StoryProjectServiceError(
                "Story increment segment path must stay under chapters/.",
                code="story_increment_segment_path_invalid",
                details={"segmentPath": normalized},
            ) from exc
        return self._canonicalize_segment_chapter_dir(root, candidate.relative_to(root).as_posix())

    def _canonicalize_segment_chapter_dir(self, workspace_root: Path, segment_relative_path: str) -> str:
        """章节目录别名归一：目标不存在但有同章号或唯一同标题目录时，重定向到现有目录。

        防止「第一章/第1章」或「Prologue/第1章 Prologue」这类异体命名
        （LLM 自选路径或归一重命名后的旧路径）在磁盘上分裂出第二个章节目录。
        """
        path = Path(segment_relative_path)
        if len(path.parts) < 3 or path.parts[0] != "chapters":
            return segment_relative_path
        root = Path(workspace_root).resolve()
        chapter_number = self._extract_chapter_number(path.parts[1])
        # 先列章节状态：内部会归一目录命名并清掉同章号的空目录，
        # 之后再判断目标目录是否存在，避免对着即将被清理的空壳落盘。
        states = self.list_chapter_states(root)
        chapter_dir = root / path.parts[0] / path.parts[1]
        if chapter_dir.exists():
            return segment_relative_path
        if chapter_number > 0:
            candidates = [state for state in states if state.chapter_number == chapter_number]
        else:
            requested_title = self._sanitize_static_chapter_title(path.parts[1])
            candidates = [
                state
                for state in states
                if self._sanitize_static_chapter_title(Path(state.relative_path).name) == requested_title
            ]
        if len(candidates) == 1:
            existing_dir = root / candidates[0].relative_path
            if existing_dir.is_dir():
                return (Path(candidates[0].relative_path) / Path(*path.parts[2:])).as_posix()
        return segment_relative_path

    def _prune_duplicate_empty_chapter_dirs(self, workspace_root: Path) -> List[str]:
        """清理与现有章节同章号的空目录（历史命名分裂留下的残骸）。

        只删「空且章号与某个非空章节重复」的目录，用户手动新建的
        待写作空章节（章号唯一）不会被触碰。
        """
        root = Path(workspace_root).resolve()
        chapters_root = root / "chapters"
        removed: List[str] = []
        if not chapters_root.is_dir():
            return removed
        numbers_with_content: Set[int] = set()
        empty_dirs: List[Tuple[int, Path]] = []
        for directory in chapters_root.iterdir():
            if not directory.is_dir():
                continue
            number = self._extract_chapter_number(directory.name)
            if number <= 0:
                continue
            if any(directory.iterdir()):
                numbers_with_content.add(number)
            else:
                empty_dirs.append((number, directory))
        for number, directory in empty_dirs:
            if number not in numbers_with_content:
                continue
            try:
                directory.rmdir()
            except OSError:
                continue
            removed.append(directory.relative_to(root).as_posix())
        return removed

    def _normalize_story_increment_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            payload = {}
        normalized = self._normalize_stage2_output(
            {
                "memory_updates": self._first_list(payload, "memoryUpdates", "memory_updates"),
                "variable_updates": self._first_list(payload, "variableUpdates", "variable_updates"),
                "item_updates": self._first_list(payload, "itemUpdates", "item_updates", "objectUpdates", "object_updates", "items"),
                "character_updates": self._first_list(payload, "characterUpdates", "character_updates"),
                "event_updates": self._first_list(payload, "eventUpdates", "event_updates"),
                "snapshot_comment": payload.get("snapshotComment", payload.get("snapshot_comment", "")),
            }
        )
        normalized["character_updates"] = self._merge_character_update_lists(
            normalized.get("character_updates", []),
            self._unknown_character_updates(payload),
        )
        normalized["item_updates"] = self._merge_item_update_lists(
            normalized.get("item_updates", []),
            self._unknown_item_updates(payload),
        )
        normalized["variable_thoughts"] = self._normalize_variable_thoughts(payload)
        normalized["fact_updates"] = self._normalize_fact_updates(
            self._first_list(payload, "factUpdates", "fact_updates", "facts")
        )
        normalized["relationship_updates"] = self._normalize_relationship_updates(
            self._first_list(payload, "relationshipUpdates", "relationship_updates", "relationships")
        )
        return normalized

    def _merge_story_increment_payloads(self, *payloads: Dict[str, Any]) -> Dict[str, Any]:
        merged: Dict[str, Any] = {
            "memory_updates": [],
            "fact_updates": [],
            "variable_updates": [],
            "item_updates": [],
            "variable_thoughts": [],
            "character_updates": [],
            "event_updates": [],
            "relationship_updates": [],
            "snapshot_comment": "",
        }
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            for key in ("memory_updates", "fact_updates", "variable_updates", "item_updates", "event_updates", "relationship_updates"):
                values = payload.get(key)
                if isinstance(values, list):
                    merged[key].extend(item for item in values if isinstance(item, dict))
            thoughts = payload.get("variable_thoughts")
            if isinstance(thoughts, list):
                merged["variable_thoughts"].extend(str(item).strip() for item in thoughts if str(item).strip())
            merged["character_updates"] = self._merge_character_update_lists(
                merged.get("character_updates", []),
                payload.get("character_updates", []),
            )
            merged["item_updates"] = self._merge_item_update_lists(
                merged.get("item_updates", []),
                payload.get("item_updates", []),
            )
            if payload.get("snapshot_comment"):
                merged["snapshot_comment"] = str(payload.get("snapshot_comment") or "").strip()
        return merged

    @staticmethod
    def _first_list(payload: Dict[str, Any], *keys: str) -> List[Any]:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return []

    def _normalize_variable_thoughts(self, payload: Dict[str, Any]) -> List[str]:
        thoughts: List[str] = []
        for key in (
            "variableThoughts",
            "variable_thoughts",
            "variableNotes",
            "variable_notes",
            "variableMarkdown",
            "variable_markdown",
            "variablesThinking",
            "variables_thinking",
        ):
            if key not in payload:
                continue
            value = payload.get(key)
            if isinstance(value, str):
                text = value.strip()
                if text:
                    thoughts.append(text)
            elif isinstance(value, list):
                for item in value:
                    text = self._render_variable_thought_item(item)
                    if text:
                        thoughts.append(text)
            elif isinstance(value, dict):
                text = self._render_variable_thought_item(value)
                if text:
                    thoughts.append(text)
        return list(dict.fromkeys(thoughts))

    def _render_variable_thought_item(self, value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            lines: List[str] = []
            for key, item in value.items():
                label = str(key or "").strip()
                if not label:
                    continue
                if isinstance(item, list):
                    nested = "；".join(str(child).strip() for child in item if str(child).strip())
                elif isinstance(item, dict):
                    nested = "；".join(
                        f"{child_key}: {child_value}"
                        for child_key, child_value in item.items()
                        if str(child_value).strip()
                    )
                else:
                    nested = str(item or "").strip()
                if nested:
                    lines.append(f"- {label}: {nested}")
            return "\n".join(lines).strip()
        text = str(value or "").strip()
        return text

    def _unknown_character_updates(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        names = self._coerce_text_list(payload.get("newCharacters") or payload.get("new_characters"))
        names.extend(self._coerce_text_list(payload.get("mentionedCharacters") or payload.get("mentioned_characters")))
        updates: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for name in names:
            key = self._normalize_character_lookup_key(name)
            if not key or key in seen:
                continue
            seen.add(key)
            updates.append({"character": name})
        return updates

    def _unknown_item_updates(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        names = self._coerce_text_list(payload.get("newItems") or payload.get("new_items"))
        names.extend(self._coerce_text_list(payload.get("mentionedItems") or payload.get("mentioned_items")))
        names.extend(self._coerce_text_list(payload.get("newObjects") or payload.get("new_objects")))
        names.extend(self._coerce_text_list(payload.get("mentionedObjects") or payload.get("mentioned_objects")))
        updates: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for name in names:
            key = self._normalize_character_lookup_key(name)
            if not key or key in seen:
                continue
            seen.add(key)
            updates.append({"item": name})
        return updates

    def _merge_character_update_lists(self, existing: Any, incoming: Any) -> List[Dict[str, Any]]:
        updates = self._normalize_character_updates(existing) + self._normalize_character_updates(incoming)
        merged: Dict[str, Dict[str, Any]] = {}
        for update in updates:
            character_name = str(update.get("character") or "").strip()
            key = self._normalize_character_lookup_key(character_name)
            if not key:
                continue
            current = merged.get(key)
            if current is None:
                merged[key] = dict(update)
                continue
            for field in ("role", "summary", "appearance", "personality", "background", "motivation", "evidence"):
                if update.get(field):
                    current[field] = update[field]
            for field in ("aliases", "relationships", "notes", "changes"):
                current[field] = self._merge_text_or_mapping_lists(current.get(field), update.get(field))
            state = current.get("state") if isinstance(current.get("state"), dict) else {}
            incoming_state = update.get("state") if isinstance(update.get("state"), dict) else {}
            current["state"] = {**state, **incoming_state}
        return list(merged.values())

    def _merge_text_or_mapping_lists(self, existing: Any, incoming: Any) -> List[Any]:
        merged: List[Any] = []
        seen: set[str] = set()
        for source in (existing, incoming):
            if not isinstance(source, list):
                source = self._coerce_text_list(source)
            for item in source:
                if item in (None, "", [], {}):
                    continue
                key = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, dict) else str(item)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)
        return merged

    def _merge_item_update_lists(self, existing: Any, incoming: Any) -> List[Dict[str, Any]]:
        updates = self._normalize_item_updates(existing) + self._normalize_item_updates(incoming)
        merged: Dict[str, Dict[str, Any]] = {}
        for update in updates:
            item_name = str(update.get("item") or "").strip()
            key = self._normalize_character_lookup_key(item_name)
            if not key:
                continue
            current = merged.get(key)
            if current is None:
                merged[key] = dict(update)
                continue
            for field in ("kind", "status", "summary", "owner", "location", "state", "evidence", "source_segment"):
                if update.get(field):
                    current[field] = update[field]
            for field in ("aliases", "tags", "changes", "notes"):
                current[field] = self._merge_text_or_mapping_lists(current.get(field), update.get(field))
        return list(merged.values())

    def _normalize_item_updates(self, value: Any) -> List[Dict[str, Any]]:
        if not isinstance(value, list):
            value = self._coerce_text_list(value)
        normalized: List[Dict[str, Any]] = []
        for item in value:
            if isinstance(item, str):
                name = self._clean_increment_text(item)
                if name:
                    normalized.append({"item": name})
                continue
            if not isinstance(item, dict):
                continue
            name = self._clean_increment_text(
                item.get("item")
                or item.get("name")
                or item.get("object")
                or item.get("title")
            )
            if not name:
                continue
            normalized.append(
                {
                    "id": self._clean_increment_text(item.get("id")),
                    "item": name,
                    "aliases": self._coerce_text_list(item.get("aliases")),
                    "kind": self._clean_increment_text(item.get("kind") or item.get("type") or item.get("category")) or "item",
                    "status": self._clean_increment_text(item.get("status")) or "active",
                    "summary": self._clean_increment_text(item.get("summary") or item.get("description") or item.get("detail")) or _UNKNOWN_CHARACTER_FIELD_VALUE,
                    "owner": self._clean_increment_text(item.get("owner") or item.get("holder")),
                    "location": self._clean_increment_text(item.get("location")),
                    "state": self._clean_increment_text(item.get("state") or item.get("condition")),
                    "evidence": self._clean_increment_text(item.get("evidence")),
                    "source_segment": self._clean_increment_text(item.get("source_segment") or item.get("sourceSegment") or item.get("segmentPath")),
                    "tags": self._coerce_text_list(item.get("tags")),
                    "changes": self._coerce_text_list(item.get("changes")),
                    "notes": self._coerce_text_list(item.get("notes")),
                }
            )
        return normalized

    def _normalize_fact_updates(self, value: Any) -> List[Dict[str, Any]]:
        if not isinstance(value, list):
            return []
        normalized: List[Dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            subject = self._clean_increment_text(item.get("subject"))
            predicate = self._clean_increment_text(item.get("predicate") or item.get("relation") or item.get("key"))
            obj = self._clean_increment_text(item.get("object") or item.get("value") or item.get("detail"))
            if not subject or not predicate or not obj:
                continue
            normalized.append(
                {
                    "id": self._clean_increment_text(item.get("id")),
                    "subject": subject,
                    "predicate": predicate,
                    "object": obj,
                    "confidence": self._clean_increment_text(item.get("confidence")).lower() or "canon",
                    "established_in": self._clean_increment_text(item.get("established_in") or item.get("establishedIn")),
                    "updated_at": self._clean_increment_text(item.get("updated_at") or item.get("updatedAt")),
                    "evidence": self._clean_increment_text(item.get("evidence")),
                }
            )
        return normalized

    def _normalize_relationship_updates(self, value: Any) -> List[Dict[str, Any]]:
        if not isinstance(value, list):
            return []
        normalized: List[Dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            source = self._clean_increment_text(item.get("source") or item.get("from"))
            target = self._clean_increment_text(item.get("target") or item.get("to") or item.get("character"))
            if not source or not target or source == target:
                continue
            normalized.append(
                {
                    "source": source,
                    "target": target,
                    "dimension": self._clean_increment_text(item.get("dimension") or item.get("type")) or "relationship",
                    "current_level": self._safe_relationship_level(item.get("current_level", item.get("currentLevel"))),
                    "delta": self._clean_increment_text(item.get("delta")),
                    "magnitude": self._clean_increment_text(item.get("magnitude")),
                    "detail": self._clean_increment_text(item.get("detail") or item.get("summary")),
                    "evidence": self._clean_increment_text(item.get("evidence")),
                    "last_updated_in": self._clean_increment_text(item.get("last_updated_in") or item.get("lastUpdatedIn")),
                }
            )
        return normalized

    def _story_increment_has_variable_payload(self, payload: Dict[str, Any]) -> bool:
        return any(
            isinstance(payload.get(key), list) and bool(payload.get(key))
            for key in (
                "variable_updates",
                "variable_thoughts",
                "item_updates",
                "fact_updates",
                "relationship_updates",
                "memory_updates",
                "event_updates",
            )
        )

    @staticmethod
    def _story_increment_has_structured_variable_operations(payload: Dict[str, Any]) -> bool:
        return any(
            isinstance(payload.get(key), list) and bool(payload.get(key))
            for key in ("variable_updates", "memory_updates", "event_updates")
        )

    def _render_variable_thought_markdown(
        self,
        *,
        segment_relative_path: str,
        thoughts: List[str],
        stage2_output: Dict[str, Any],
        updated_at: str,
    ) -> str:
        lines = [
            f"# 变量思考：{Path(segment_relative_path).stem}",
            "",
            f"- 片段：`{segment_relative_path}`",
            f"- 更新时间：{updated_at}",
            "",
            "## 思考记录",
            "",
        ]
        for index, thought in enumerate(thoughts, start=1):
            text = str(thought or "").strip()
            if not text:
                continue
            if len(thoughts) > 1:
                lines.extend([f"### {index}", "", text, ""])
            else:
                lines.extend([text, ""])
        variable_ops = stage2_output.get("variable_updates") if isinstance(stage2_output.get("variable_updates"), list) else []
        item_updates = stage2_output.get("item_updates") if isinstance(stage2_output.get("item_updates"), list) else []
        fact_updates = stage2_output.get("fact_updates") if isinstance(stage2_output.get("fact_updates"), list) else []
        relationship_updates = (
            stage2_output.get("relationship_updates") if isinstance(stage2_output.get("relationship_updates"), list) else []
        )
        if variable_ops or item_updates or fact_updates or relationship_updates:
            lines.extend(
                [
                    "## 可选机器整理",
                    "",
                    f"- 结构化变量操作：{len(variable_ops)} 条",
                    f"- 物品增量：{len(item_updates)} 条",
                    f"- 事实增量：{len(fact_updates)} 条",
                    f"- 关系增量：{len(relationship_updates)} 条",
                    "",
                    "> 这些统计只是后端可合并层的摘要；变量思考正文以 Markdown 为准。",
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    def _upsert_entities_from_character_updates(
        self,
        workspace_root: Path,
        character_updates: List[Dict[str, Any]],
        *,
        updated_at: str,
    ) -> List[str]:
        root = Path(workspace_root).resolve()
        entities_path = self.storydex_root(root) / "memory" / "current" / "entities.json"
        payload = self._read_json(entities_path)
        if not isinstance(payload, dict):
            payload = {"version": 1, "entities": []}
        entities = payload.get("entities") if isinstance(payload.get("entities"), list) else []
        by_name: Dict[str, Dict[str, Any]] = {}
        for item in entities:
            if not isinstance(item, dict):
                continue
            canonical = self._clean_increment_text(item.get("canonical_name") or item.get("canonicalName") or item.get("name"))
            key = self._normalize_character_lookup_key(canonical)
            if key:
                by_name[key] = dict(item)

        for update in character_updates:
            name = self._clean_increment_text(update.get("character"))
            key = self._normalize_character_lookup_key(name)
            if not key:
                continue
            entry = by_name.get(key, {"canonical_name": name, "kind": "character", "status": "active"})
            aliases = self._merge_text_lists(entry.get("aliases"), update.get("aliases"))
            entry["canonical_name"] = self._clean_increment_text(entry.get("canonical_name") or name)
            entry["aliases"] = [alias for alias in aliases if alias and alias != entry["canonical_name"]]
            entry["kind"] = self._clean_increment_text(entry.get("kind")) or "character"
            entry["status"] = self._clean_increment_text(entry.get("status")) or "active"
            entry["updatedAt"] = updated_at
            by_name[key] = entry

        payload["version"] = 1
        payload["updatedAt"] = updated_at
        payload["entities"] = sorted(by_name.values(), key=lambda item: str(item.get("canonical_name") or ""))
        serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        current = entities_path.read_text(encoding="utf-8") if entities_path.exists() else ""
        if current == serialized:
            return []
        entities_path.parent.mkdir(parents=True, exist_ok=True)
        entities_path.write_text(serialized, encoding="utf-8")
        return [entities_path.relative_to(root).as_posix()]

    def _apply_fact_updates(
        self,
        workspace_root: Path,
        fact_updates: List[Dict[str, Any]],
        *,
        updated_at: str,
    ) -> List[str]:
        root = Path(workspace_root).resolve()
        facts_path = self.storydex_root(root) / "memory" / "current" / "facts.json"
        payload = self._read_json(facts_path)
        if not isinstance(payload, dict):
            payload = {"version": 1, "facts": []}
        facts = payload.get("facts") if isinstance(payload.get("facts"), list) else []
        by_key: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for item in facts:
            if not isinstance(item, dict):
                continue
            key = (
                self._clean_increment_text(item.get("subject")),
                self._clean_increment_text(item.get("predicate")),
                self._clean_increment_text(item.get("object")),
            )
            if all(key):
                by_key[key] = dict(item)

        for update in fact_updates:
            key = (update["subject"], update["predicate"], update["object"])
            entry = by_key.get(key, {})
            stable_id = update.get("id") or entry.get("id") or "fact_" + sha256("|".join(key).encode("utf-8")).hexdigest()[:16]
            entry.update(
                {
                    "id": stable_id,
                    "subject": update["subject"],
                    "predicate": update["predicate"],
                    "object": update["object"],
                    "confidence": update.get("confidence") or entry.get("confidence") or "canon",
                    "established_in": update.get("established_in") or entry.get("established_in") or "",
                    "updated_at": updated_at,
                    "evidence": update.get("evidence") or entry.get("evidence") or "",
                }
            )
            by_key[key] = entry

        payload["version"] = 1
        payload["updatedAt"] = updated_at
        payload["facts"] = sorted(by_key.values(), key=lambda item: (str(item.get("subject")), str(item.get("predicate")), str(item.get("object"))))
        facts_path.parent.mkdir(parents=True, exist_ok=True)
        facts_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return [facts_path.relative_to(root).as_posix()]

    def _apply_item_updates(
        self,
        workspace_root: Path,
        item_updates: List[Dict[str, Any]],
        *,
        updated_at: str,
    ) -> List[str]:
        root = Path(workspace_root).resolve()
        items_path = self.storydex_root(root) / "memory" / "current" / "items.json"
        payload = self._read_json(items_path)
        if not isinstance(payload, dict):
            payload = {"version": 1, "items": []}
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        by_key: Dict[str, Dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            name = self._clean_increment_text(item.get("name") or item.get("item"))
            key = self._normalize_character_lookup_key(name)
            if key:
                by_key[key] = dict(item)

        for update in item_updates:
            name = self._clean_increment_text(update.get("item") or update.get("name"))
            key = self._normalize_character_lookup_key(name)
            if not key:
                continue
            entry = by_key.get(key, {})
            stable_id = update.get("id") or entry.get("id") or "item_" + sha256(key.encode("utf-8")).hexdigest()[:16]
            aliases = self._merge_text_lists(entry.get("aliases"), update.get("aliases"))
            history = entry.get("history") if isinstance(entry.get("history"), list) else []
            history_record = {
                "updatedAt": updated_at,
                "segmentPath": update.get("source_segment") or entry.get("latestSegment") or "",
                "changes": update.get("changes") or [],
                "evidence": update.get("evidence") or "",
            }
            if any(history_record.values()):
                history = self._append_limited_records(history, history_record)
            entry.update(
                {
                    "id": stable_id,
                    "name": name,
                    "aliases": [alias for alias in aliases if alias and alias != name],
                    "kind": update.get("kind") or entry.get("kind") or "item",
                    "status": update.get("status") or entry.get("status") or "active",
                    "summary": update.get("summary") or entry.get("summary") or _UNKNOWN_CHARACTER_FIELD_VALUE,
                    "owner": update.get("owner") or entry.get("owner") or "",
                    "location": update.get("location") or entry.get("location") or "",
                    "state": update.get("state") or entry.get("state") or "",
                    "tags": self._merge_text_lists(entry.get("tags"), update.get("tags")),
                    "notes": self._merge_text_lists(entry.get("notes"), update.get("notes")),
                    "latestSegment": update.get("source_segment") or entry.get("latestSegment") or "",
                    "updatedAt": updated_at,
                    "history": history,
                }
            )
            by_key[key] = entry

        payload["version"] = 1
        payload["updatedAt"] = updated_at
        payload["items"] = sorted(by_key.values(), key=lambda item: str(item.get("name") or ""))
        serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        current = items_path.read_text(encoding="utf-8") if items_path.exists() else ""
        if current == serialized:
            return []
        items_path.parent.mkdir(parents=True, exist_ok=True)
        items_path.write_text(serialized, encoding="utf-8")
        return [items_path.relative_to(root).as_posix()]

    def _apply_relationship_updates(
        self,
        workspace_root: Path,
        relationship_updates: List[Dict[str, Any]],
        *,
        updated_at: str,
    ) -> List[str]:
        root = Path(workspace_root).resolve()
        graph_path = self.storydex_root(root) / "memory" / "current" / "relationship_graph.json"
        payload = self._read_json(graph_path)
        if not isinstance(payload, dict):
            payload = {"version": 1, "edges": []}
        edges = payload.get("edges") if isinstance(payload.get("edges"), list) else []
        by_key: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for item in edges:
            if not isinstance(item, dict):
                continue
            key = (
                self._clean_increment_text(item.get("source")),
                self._clean_increment_text(item.get("target")),
                self._clean_increment_text(item.get("dimension")) or "relationship",
            )
            if key[0] and key[1]:
                by_key[key] = dict(item)

        for update in relationship_updates:
            key = (update["source"], update["target"], update["dimension"])
            entry = by_key.get(key, {"source": key[0], "target": key[1], "dimension": key[2], "history": []})
            current_level = update.get("current_level")
            if current_level is None:
                base_level = self._safe_relationship_level(entry.get("current_level"))
                current_level = self._clamp_relationship_level(
                    (base_level if base_level is not None else 0) + self._relationship_delta_value(update)
                )
            history = entry.get("history") if isinstance(entry.get("history"), list) else []
            history.append(
                {
                    "updated_at": updated_at,
                    "last_updated_in": update.get("last_updated_in") or "",
                    "delta": update.get("delta") or "",
                    "magnitude": update.get("magnitude") or "",
                    "detail": update.get("detail") or "",
                    "evidence": update.get("evidence") or "",
                }
            )
            entry.update(
                {
                    "source": update["source"],
                    "target": update["target"],
                    "dimension": update["dimension"],
                    "current_level": self._clamp_relationship_level(current_level),
                    "last_updated_at": updated_at,
                    "last_updated_in": update.get("last_updated_in") or entry.get("last_updated_in") or "",
                    "history": history[-20:],
                }
            )
            by_key[key] = entry

        payload["version"] = 1
        payload["updatedAt"] = updated_at
        payload["edges"] = sorted(by_key.values(), key=lambda item: (str(item.get("source")), str(item.get("target")), str(item.get("dimension"))))
        graph_path.parent.mkdir(parents=True, exist_ok=True)
        graph_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return [graph_path.relative_to(root).as_posix()]

    @staticmethod
    def _clean_increment_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip())

    @staticmethod
    def _safe_relationship_level(value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            return StoryProjectService._clamp_relationship_level(int(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _clamp_relationship_level(value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 0
        return max(-10, min(10, parsed))

    @staticmethod
    def _relationship_delta_value(update: Dict[str, Any]) -> int:
        delta = str(update.get("delta") or "").strip().lower()
        magnitude = str(update.get("magnitude") or "").strip().lower()
        step = 2 if magnitude in {"strong", "major", "large", "高", "强", "大"} else 1
        if delta in {"increase", "forge", "improve", "up", "增强", "上升", "建立"}:
            return step
        if delta in {"decrease", "break", "worsen", "down", "减弱", "下降", "破裂"}:
            return -step
        return 0

    def _normalize_stage2_output(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        memory_updates = payload.get("memory_updates") if isinstance(payload.get("memory_updates"), list) else []
        fact_updates = payload.get("fact_updates") if isinstance(payload.get("fact_updates"), list) else []
        item_updates = self._normalize_item_updates(payload.get("item_updates"))
        variable_updates = payload.get("variable_updates") if isinstance(payload.get("variable_updates"), list) else []
        character_updates = self._normalize_character_updates(payload.get("character_updates"))
        event_updates = payload.get("event_updates") if isinstance(payload.get("event_updates"), list) else []

        normalized_variables: List[Dict[str, Any]] = []
        for item in variable_updates:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            normalized_item = {
                "op": str(item.get("op") or "set").strip().lower() or "set",
                "path": path,
                "value": item.get("value"),
                "evidence": str(item.get("evidence") or "").strip(),
            }
            if "requiresReview" in item:
                normalized_item["requiresReview"] = bool(item.get("requiresReview"))
            normalized_variables.append(normalized_item)

        return {
            "memory_updates": memory_updates,
            "fact_updates": fact_updates,
            "item_updates": item_updates,
            "variable_updates": normalized_variables,
            "character_updates": character_updates,
            "event_updates": event_updates,
            "snapshot_comment": str(payload.get("snapshot_comment") or "").strip(),
        }

    @staticmethod
    def _normalize_snapshot_operations(operations: Any) -> List[Dict[str, Any]]:
        if not isinstance(operations, list):
            return []
        normalized: List[Dict[str, Any]] = []
        for item in operations:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip().strip(".")
            op = str(item.get("op") or "set").strip().lower() or "set"
            if not path or ".." in path or "/" in path or op not in {"set", "replace", "add", "remove"}:
                continue
            evidence = str(item.get("evidence") or "").strip()
            review_reasons: List[str] = []
            if bool(item.get("requiresReview", False)):
                review_reasons.append("explicit_requires_review")
            if op == "remove":
                review_reasons.append("remove_operation")
            if not evidence:
                review_reasons.append("missing_evidence")
            normalized.append(
                {
                    "op": op,
                    "path": path,
                    "value": item.get("value"),
                    "evidence": evidence,
                    "confidence": float(item.get("confidence") or (1.0 if evidence else 0.5)),
                    "requiresReview": bool(review_reasons),
                    "reviewReasons": review_reasons,
                }
            )
        return normalized

    @staticmethod
    def _partition_snapshot_operations(
        operations: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        accepted: List[Dict[str, Any]] = []
        review_required: List[Dict[str, Any]] = []
        for operation in operations:
            if bool(operation.get("requiresReview", False)):
                reasons = operation.get("reviewReasons")
                review_required.append(
                    {
                        "path": str(operation.get("path") or ""),
                        "op": str(operation.get("op") or "set"),
                        "reasons": list(reasons) if isinstance(reasons, list) else ["requires_review"],
                    }
                )
                continue
            accepted.append({key: value for key, value in operation.items() if key != "reviewReasons"})
        return accepted, review_required

    def _apply_operations_to_full_state(self, *, full_state: Dict[str, Any], operations: List[Dict[str, Any]]) -> None:
        for operation in operations:
            op = str(operation.get("op") or "set").lower()
            path = str(operation.get("path") or "")
            value = operation.get("value")
            if not path:
                continue
            if op == "remove":
                self._remove_nested_path(full_state, path)
            elif op == "add":
                self._add_nested_path(full_state, path, value)
            else:
                self._set_nested_path(full_state, path, value)

    @staticmethod
    def _set_nested_path(target: Dict[str, Any], path: str, value: Any) -> None:
        parts = [part for part in path.split(".") if part]
        if not parts:
            return
        cursor: Dict[str, Any] = target
        for part in parts[:-1]:
            child = cursor.get(part)
            if not isinstance(child, dict):
                child = {}
                cursor[part] = child
            cursor = child
        cursor[parts[-1]] = value

    @staticmethod
    def _add_nested_path(target: Dict[str, Any], path: str, value: Any) -> None:
        parts = [part for part in path.split(".") if part]
        if not parts:
            return
        cursor: Dict[str, Any] = target
        for part in parts[:-1]:
            child = cursor.get(part)
            if not isinstance(child, dict):
                child = {}
                cursor[part] = child
            cursor = child
        leaf = parts[-1]
        existing = cursor.get(leaf)
        if isinstance(existing, (int, float)) and isinstance(value, (int, float)):
            cursor[leaf] = existing + value
        else:
            cursor[leaf] = value

    @staticmethod
    def _remove_nested_path(target: Dict[str, Any], path: str) -> None:
        parts = [part for part in path.split(".") if part]
        if not parts:
            return
        cursor: Dict[str, Any] = target
        for part in parts[:-1]:
            child = cursor.get(part)
            if not isinstance(child, dict):
                return
            cursor = child
        cursor.pop(parts[-1], None)

    def _refresh_project_preset_skill(self, workspace_root: Path) -> None:
        root = Path(workspace_root).resolve()
        skill_path = self.agent_root(root) / "skills" / "story_preset_constraints.md"
        content = self._build_project_preset_skill(root)
        if skill_path.exists():
            try:
                if skill_path.read_text(encoding="utf-8") == content:
                    return
            except OSError:
                pass
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(content, encoding="utf-8")

    # ---- T-C: 结构化预设管理 -------------------------------------------

    def preset_root(self, workspace_root: Path) -> Path:
        return self.storydex_root(Path(workspace_root).resolve()) / "presets"

    def active_pointer_path(self, workspace_root: Path) -> Path:
        return self.preset_root(workspace_root) / "active.json"

    def read_active_pointer(self, workspace_root: Path) -> Dict[str, Any]:
        path = self.active_pointer_path(workspace_root)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def write_active_pointer(self, workspace_root: Path, data: Dict[str, Any]) -> None:
        path = self.active_pointer_path(workspace_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def load_preset_sidecar(self, md_path: Path) -> Optional[PresetDocument]:
        sidecar = find_sidecar_path(md_path)
        if not sidecar.exists():
            return None
        doc, _warnings = _load_preset_sidecar_impl(sidecar)
        return doc

    def load_active_preset_document(self, workspace_root: Path) -> Optional[PresetDocument]:
        root = Path(workspace_root).resolve()
        pointer = self.read_active_pointer(root)
        rel = pointer.get("activeMainPreset", "") if isinstance(pointer, dict) else ""
        if not rel:
            files = self._runtime_preset_files(root, max_files=1)
            if not files:
                return None
            md_path = files[0]
        else:
            md_path = (root / rel).resolve()
        if not md_path.exists():
            return None
        return self.load_preset_sidecar(md_path)

    def write_preset_sidecar(self, md_path: Path, doc: PresetDocument) -> None:
        _write_preset_sidecar_impl(md_path, doc)

    def list_presets(self, workspace_root: Path) -> Dict[str, List[Dict[str, Any]]]:
        root = Path(workspace_root).resolve()
        preset_root = self.preset_root(root)
        result: Dict[str, List[Dict[str, Any]]] = {"active": [], "library": []}
        for section in ("active", "library"):
            section_dir = preset_root / section
            if not section_dir.exists():
                continue
            for path in section_dir.rglob("*.md"):
                if path.name.lower() == "readme.md":
                    continue
                sidecar = find_sidecar_path(path)
                result[section].append({
                    "name": path.stem,
                    "path": path.relative_to(root).as_posix(),
                    "hasSidecar": sidecar.exists(),
                })
            result[section].sort(key=lambda item: item["name"].lower())
        return result

    def _move_preset_pair(self, md_path: Path, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        new_md = dest_dir / md_path.name
        md_path.replace(new_md)
        sidecar = find_sidecar_path(md_path)
        if sidecar.exists():
            new_sidecar = dest_dir / sidecar.name
            sidecar.replace(new_sidecar)
        return new_md

    def activate_preset(self, workspace_root: Path, relative_md_path: str) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        src = (root / relative_md_path).resolve()
        if not src.exists() or src.suffix.lower() != ".md":
            raise StorydexError(f"preset not found or not a markdown file: {relative_md_path}")
        active_dir = self.preset_root(root) / "active"
        if src.parent != active_dir:
            src = self._move_preset_pair(src, active_dir)
        pointer = self.read_active_pointer(root)
        pointer["activeMainPreset"] = src.relative_to(root).as_posix()
        self.write_active_pointer(root, pointer)
        self._refresh_project_preset_skill(root)
        return pointer

    def deactivate_preset(self, workspace_root: Path, relative_md_path: str) -> Dict[str, Any]:
        root = Path(workspace_root).resolve()
        src = (root / relative_md_path).resolve()
        library_dir = self.preset_root(root) / "library"
        if src.exists() and src.parent == self.preset_root(root) / "active":
            self._move_preset_pair(src, library_dir)
        pointer = self.read_active_pointer(root)
        if pointer.get("activeMainPreset") == relative_md_path:
            pointer["activeMainPreset"] = ""
            self.write_active_pointer(root, pointer)
        self._refresh_project_preset_skill(root)
        return pointer

    # ---- /T-C ----------------------------------------------------------

    def _collect_preset_entries(
        self,
        workspace_root: Path,
        *,
        max_files: int = 5,
        max_chars_per_file: int = 720,
        runtime_context: Optional[Dict[str, Any]] = None,
        st_max_chars_per_file: Optional[int] = None,
        compile_errors: Optional[List[str]] = None,
    ) -> List[Tuple[str, str]]:
        root = Path(workspace_root).resolve()
        preset_root = self.storydex_root(root) / "presets"
        if not preset_root.exists() or not preset_root.is_dir():
            return []

        files = self._runtime_preset_files(root, max_files=max_files)

        entries: List[Tuple[str, str]] = []
        for path in files:
            preview = self._read_text_preview(path, max_chars=max_chars_per_file)
            if path.suffix.lower() == ".md":
                sidecar = find_sidecar_path(path)
                if sidecar.exists():
                    doc, _warnings = _load_preset_sidecar_impl(sidecar)
                    compiled = self._compile_preset_sidecar_text(
                        doc,
                        runtime_context=runtime_context,
                        max_chars=max_chars_per_file,
                        st_max_chars=st_max_chars_per_file,
                        source_path=path.relative_to(root).as_posix(),
                        compile_errors=compile_errors,
                    )
                    if compiled:
                        entries.append((path.relative_to(root).as_posix(), compiled))
                        continue
                    summary = summarize_preset_sidecar(doc, max_chars=max_chars_per_file)
                    if summary:
                        preview = preview + "\n\n---\n\n[Structured Preset Sidecar]\n" + summary
            if not preview:
                continue
            entries.append((path.relative_to(root).as_posix(), preview))
        return entries

    def _compile_preset_sidecar_text(
        self,
        doc: PresetDocument,
        *,
        runtime_context: Optional[Dict[str, Any]],
        max_chars: int,
        st_max_chars: Optional[int] = None,
        source_path: str = "",
        compile_errors: Optional[List[str]] = None,
    ) -> str:
        try:
            from services.preset_compiler import compile_preset

            result = compile_preset(doc, runtime_context=runtime_context)
            compiled = result.compiled_text
            # ST 绝对注入（injection_position == 1）没有独立的消息层可挂，
            # 按 depth 从大到小追加到文本尾部：depth 越小离生成越近。
            if result.injections:
                injection_texts = [
                    injection.text.strip()
                    for injection in sorted(result.injections, key=lambda item: -item.depth)
                    if injection.text.strip()
                ]
                if injection_texts:
                    compiled = "\n\n".join(part for part in [compiled, *injection_texts] if part)
        except Exception as exc:
            # 编译失败不能静默：用户会以为预设生效了。记录日志并把错误
            # 传给调用方（进入上下文组装 notes / 运行时状态工具）。
            message = f"{source_path or 'preset sidecar'}: {type(exc).__name__}: {exc}"
            _PRESET_COMPILE_LOGGER.warning("Preset sidecar compile failed: %s", message)
            if compile_errors is not None:
                compile_errors.append(message)
            return ""
        effective_max = max_chars
        if st_max_chars is not None and self._is_silly_tavern_document(doc):
            effective_max = max(max_chars, int(st_max_chars))
        return self._truncate_text(compiled, max_chars=effective_max)

    @staticmethod
    def _is_silly_tavern_document(doc: PresetDocument) -> bool:
        # 外部导入的预设都允许使用运行时大预算。
        return str(getattr(doc.meta, "source_format", "") or "").strip().lower() in {"sillytavern", "generic"}

    def _runtime_preset_files(self, workspace_root: Path, *, max_files: int) -> List[Path]:
        root = Path(workspace_root).resolve()
        preset_root = self.storydex_root(root) / "presets"
        if not preset_root.exists() or not preset_root.is_dir():
            return []

        active_files = self._preset_files_from_dir(preset_root / "active", root=root)
        if active_files:
            return active_files[:1]

        compiled_files = [
            path
            for path in self._preset_files_from_dir(preset_root / "compiled", root=root)
            if path.name.lower().endswith(_RUNTIME_PRESET_JSON_SUFFIX) or path.suffix.lower() in _RUNTIME_PRESET_TEXT_SUFFIXES
        ]
        if compiled_files:
            return compiled_files[: max(1, int(max_files or 1))]

        legacy_files = [
            path
            for path in preset_root.iterdir()
            if path.is_file()
            and path.suffix.lower() in _RUNTIME_PRESET_TEXT_SUFFIXES
            and path.name.lower() != "readme.md"
        ]
        legacy_files.sort(key=lambda item: (item.name.lower(), item.stat().st_mtime))
        return legacy_files[: max(1, int(max_files or 1))]

    def _preset_files_from_dir(self, directory: Path, *, root: Path) -> List[Path]:
        if not directory.exists() or not directory.is_dir():
            return []

        files: List[Path] = []
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            if any(part.lower() in _PRESET_LIBRARY_DIRS for part in path.relative_to(directory).parts[:-1]):
                continue
            if path.name.lower() == "readme.md":
                continue
            if path.suffix.lower() in _RUNTIME_PRESET_TEXT_SUFFIXES or path.name.lower().endswith(_RUNTIME_PRESET_JSON_SUFFIX):
                files.append(path)
        files.sort(key=lambda item: (item.relative_to(root).as_posix().lower(), item.stat().st_mtime))
        return files

    def _build_project_preset_skill(self, workspace_root: Path) -> str:
        entries = self._collect_preset_entries(workspace_root, max_files=8, max_chars_per_file=900)
        templates = self._read_builtin_skill_templates()
        base = str(templates.get("story_preset_constraints.md") or "").strip()
        if not base:
            raise StoryProjectServiceError("Missing docs/skills/story_preset_constraints.md")
        inactive_message = (
            "当前没有启用项目预设。将一个经过审阅的 `.md`、`.txt` 或已编译预设 sidecar 放入 "
            "`.storydex/presets/active/` 后，才会影响故事生成。"
        )
        if not entries:
            return base + "\n"

        active_content = "\n\n".join(f"### {relative_path}\n\n{content}" for relative_path, content in entries)
        if inactive_message in base:
            return base.replace(inactive_message, active_content).rstrip() + "\n"
        return f"{base}\n\n## 当前激活预设\n\n{active_content}\n"

    def _build_preset_context(
        self,
        workspace_root: Path,
        *,
        max_files: int = 5,
        max_chars_per_file: int = 720,
        total_chars: int = 2400,
        runtime_context: Optional[Dict[str, Any]] = None,
        compile_errors: Optional[List[str]] = None,
    ) -> str:
        entries = self._collect_preset_entries(
            workspace_root,
            max_files=max_files,
            max_chars_per_file=max_chars_per_file,
            runtime_context=runtime_context,
            st_max_chars_per_file=_ST_RUNTIME_PRESET_MAX_CHARS_PER_FILE,
            compile_errors=compile_errors,
        )
        if not entries:
            return ""

        lines: List[str] = [
            "[Active Project Preset]",
            "The rules below are the authoritative creative directives for this generation.",
            "Follow them strictly for style, POV, formatting, pacing, and content decisions; they take precedence over generic style defaults.",
            "Raw preset export JSON and regex/display scripts remain source metadata unless a compatibility layer applies them.",
        ]
        for relative_path, content in entries:
            lines.extend(["", f"### {relative_path}", content])
        # 外部导入预设编译文本远超 Storydex 自有预设的预算；出现超长条目时
        # 放开总预算，保持社区预设全量注入。
        effective_total = int(total_chars or 2400)
        if any(len(content) > max_chars_per_file for _, content in entries):
            effective_total = max(effective_total, _ST_RUNTIME_PRESET_TOTAL_CHARS)
        return self._truncate_text("\n".join(lines).strip(), max_chars=effective_total)

    def _build_character_hard_constraints_context(
        self,
        workspace_root: Path,
        *,
        max_files: int = 8,
        max_chars_per_file: int = 600,
        total_chars: int = 1800,
        prompt: str = "",
        active_file: str = "",
    ) -> str:
        root = Path(workspace_root).resolve()
        character_root = self.storydex_root(root) / "characters"
        if not character_root.exists() or not character_root.is_dir():
            return ""

        candidates: List[Path] = []
        # 一级目录：用户手工创建的角色卡（命名通常带序号前缀，如 01_陈思齐.json）
        for pattern in ("*.json", "*.md", "*.txt"):
            for path in character_root.glob(pattern):
                if path.is_file() and path.name.lower() != "readme.md":
                    candidates.append(path)
        # cards/ subdirectory: legacy/imported character card files.
        cards_dir = character_root / "cards"
        if cards_dir.exists() and cards_dir.is_dir():
            for path in cards_dir.glob("*.json"):
                if path.is_file():
                    candidates.append(path)
        # states/ subdirectory: legacy/imported character state files.
        states_dir = character_root / "states"
        if states_dir.exists() and states_dir.is_dir():
            for path in states_dir.glob("*.json"):
                if path.is_file():
                    candidates.append(path)
        if not candidates:
            return ""

        # Within the same dedup tag (i.e. same character within the same
        # subdirectory), prefer user-authored .md/.txt cards over Stage-2
        # auto-generated .json cards. Stage-2 will happily emit
        # `01_陈思齐.json` next to a hand-written `01_陈思齐.md`; without this
        # sort the JSON wins (glob enumerates `*.json` before `*.md`) and the
        # richer hand-written card silently disappears from the prompt.
        # Stable sort: only affects same-tag siblings.
        def _format_priority(path: Path) -> int:
            suffix = path.suffix.lower()
            return {".md": 0, ".txt": 1, ".json": 2}.get(suffix, 3)

        candidates.sort(key=lambda path: (_format_priority(path), path.name.lower()))

        # 同名去重：一级目录 > cards/ > states/，保留最高优先级版本的路径分组
        seen_keys: Set[str] = set()
        deduped: List[Path] = []
        for path in candidates:
            try:
                if path.suffix.lower() == ".json":
                    data = self._load_character_constraint_json(path)
                    if isinstance(data, dict):
                        key = str(data.get("name") or path.stem).strip().lower()
                    else:
                        key = path.stem.lower()
                else:
                    key = path.stem.lower()
            except Exception:
                key = path.stem.lower()
            # 序号前缀剥离：01_陈思齐 → 陈思齐
            key = re.sub(r"^\d{1,3}[_-]", "", key)
            tag = f"{key}::{path.parent.name}"
            if tag in seen_keys:
                continue
            seen_keys.add(tag)
            deduped.append(path)

        candidates = deduped
        # P2-d 相关性排序：按 prompt + active_file 中是否提到该角色名/aliases 评分
        relevance_keywords = self._collect_relevance_keywords(root=root, prompt=prompt, active_file=active_file)
        scored: List[Tuple[int, int, Path]] = []
        priority_order = {"characters": 0, "cards": 1, "states": 2}
        for path in candidates:
            score = self._score_character_path_relevance(path, relevance_keywords)
            tier = priority_order.get(path.parent.name, 3)
            scored.append((score, tier, path))
        if any(score > 0 for score, _, _ in scored):
            scored = [(score, tier, path) for score, tier, path in scored if score > 0]
        scored.sort(key=lambda item: (-item[0], item[1], item[2].name.lower()))
        candidates = [item[2] for item in scored][: max(1, int(max_files or 1))]

        lines: List[str] = [
            "[Project Characters Hard Constraints]",
            "以下角色档案是项目硬设定，写作必须严格遵守，不得违背、改写、淡化或自行替换。",
            "Background、motivation、relationships、state 字段所述的事实（包括失踪/死亡/远行/在场状态）必须忠实采用。",
        ]

        for path in candidates:
            relative_path = path.relative_to(root).as_posix()
            if path.suffix.lower() == ".json":
                rendered = self._render_character_json_constraints(path, max_chars=max_chars_per_file)
            else:
                rendered = self._render_character_md_constraints(path, max_chars=max_chars_per_file)
            rendered = (rendered or "").strip()
            if not rendered:
                continue
            lines.extend(["", f"### {relative_path}", rendered])

        if len(lines) <= 3:
            return ""
        return self._truncate_text("\n".join(lines).strip(), max_chars=total_chars)

    @staticmethod
    def _load_character_constraint_json(path: Path) -> Optional[Dict[str, Any]]:
        try:
            read = read_bounded_text_limited(path, _CHARACTER_CONSTRAINT_JSON_READ_LIMIT)
            if read.truncated:
                return None
            data = json.loads(read.text)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _render_character_json_constraints(self, path: Path, *, max_chars: int) -> str:
        data = self._load_character_constraint_json(path)
        if not isinstance(data, dict):
            return self._render_character_md_constraints(path, max_chars=max_chars)

        lines: List[str] = []
        name = str(data.get("name") or path.stem).strip()
        if name:
            lines.append(f"name: {name}")
        aliases = data.get("aliases")
        if isinstance(aliases, list) and aliases:
            alias_text = ", ".join(str(item).strip() for item in aliases if str(item).strip())
            if alias_text:
                lines.append(f"aliases: {alias_text}")
        for field in ("role", "summary", "background", "motivation"):
            value = data.get(field)
            if isinstance(value, str) and value.strip():
                lines.append(f"{field}: {value.strip()}")
            elif isinstance(value, list) and value:
                joined = "; ".join(str(item).strip() for item in value if str(item).strip())
                if joined:
                    lines.append(f"{field}: {joined}")
        relationships = data.get("relationships")
        if isinstance(relationships, list) and relationships:
            rel_lines: List[str] = []
            for item in relationships:
                if not isinstance(item, dict):
                    continue
                target = str(item.get("target") or "").strip()
                relation = str(item.get("relation") or "").strip()
                detail = str(item.get("detail") or "").strip()
                fragment = " - ".join(part for part in (target, relation, detail) if part)
                if fragment:
                    rel_lines.append(f"  - {fragment}")
            if rel_lines:
                lines.append("relationships:")
                lines.extend(rel_lines)
        state = data.get("state")
        if isinstance(state, dict) and state:
            state_lines: List[str] = []
            for key in ("status", "emotion", "location", "goal"):
                if state.get(key) not in (None, ""):
                    state_lines.append(f"  {key}: {state.get(key)}")
            if state_lines:
                lines.append("state:")
                lines.extend(state_lines)
        notes = data.get("notes")
        if isinstance(notes, list) and notes:
            note_text = "; ".join(str(item).strip() for item in notes if str(item).strip())
            if note_text:
                lines.append(f"notes: {note_text}")
        elif isinstance(notes, str) and notes.strip():
            lines.append(f"notes: {notes.strip()}")

        text = "\n".join(lines).strip()
        if not text:
            return ""
        return self._truncate_text(text, max_chars=max_chars)

    def _render_character_md_constraints(self, path: Path, *, max_chars: int) -> str:
        """实战反馈第三轮: 渲染 .md 角色卡时，把"亲属关系/身份"关键句前置，
        避免被 max_chars 截断后 LLM 看不到关键关系信息（如"叔叔/邻居/掌柜"）。
        关键关系命中行会作为 *Key relations* 段先列出，再附原文 preview。"""
        try:
            raw = read_bounded_text_preview(path, max_chars=max(max_chars, 4000))
        except Exception:
            return ""
        if not raw.strip():
            return ""

        relation_keywords = (
            "叔叔", "伯父", "舅父", "姑父", "姨父", "外公", "外婆", "祖父", "祖母", "爷爷", "奶奶",
            "兄长", "哥哥", "姐姐", "弟弟", "妹妹", "堂兄", "堂弟", "表兄", "表妹", "侄子", "侄女",
            "邻居", "街坊", "朋友", "故交", "师父", "师傅", "师兄", "师弟", "师妹", "师姐",
            "掌柜", "老板", "东家", "学徒", "下属", "上司", "管家",
            "父亲", "母亲", "爹", "娘", "爸爸", "妈妈", "妻子", "丈夫", "夫君",
            "失踪", "已死", "已故", "遇难", "下落不明", "存亡未卜", "远行", "在外", "未归",
            "寄住", "寄养", "收养", "抚养",
        )
        key_lines: List[str] = []
        for raw_line in raw.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if any(kw in line for kw in relation_keywords):
                if line not in key_lines:
                    key_lines.append(line)
            if len(key_lines) >= 8:
                break

        preview = self._read_text_preview(path, max_chars=max(120, max_chars - 240))
        if key_lines:
            head = "Key relations:\n" + "\n".join(f"- {ln}" for ln in key_lines)
            combined = head + "\n\n--- preview ---\n" + (preview or "")
        else:
            combined = preview or ""
        return self._truncate_text(combined.strip(), max_chars=max_chars)

    def _build_worldbook_hard_constraints_context(
        self,
        workspace_root: Path,
        *,
        max_files: int = 6,
        max_chars_per_file: int = 500,
        total_chars: int = 1400,
        prompt: str = "",
        active_file: str = "",
    ) -> str:
        root = Path(workspace_root).resolve()
        worldbook_root = self.storydex_root(root) / "worldbook"
        if not worldbook_root.exists() or not worldbook_root.is_dir():
            return ""

        candidates: List[Path] = []
        for pattern in ("*.md", "*.txt", "*.json"):
            for path in worldbook_root.glob(pattern):
                if path.is_file() and path.name.lower() != "readme.md":
                    candidates.append(path)
        if not candidates:
            return ""

        # P2-d 相关性排序：按 prompt + active_file 中是否提到该条目名/文本内容评分
        relevance_keywords = self._collect_relevance_keywords(root=root, prompt=prompt, active_file=active_file)
        scored: List[Tuple[int, Path]] = []
        for path in candidates:
            score = self._score_worldbook_path_relevance(path, relevance_keywords)
            scored.append((score, path))
        if any(score > 0 for score, _ in scored):
            scored = [(score, path) for score, path in scored if score > 0]
        scored.sort(key=lambda item: (-item[0], item[1].name.lower()))
        candidates = [item[1] for item in scored][: max(1, int(max_files or 1))]

        lines: List[str] = [
            "[Project Worldbook Hard Constraints]",
            "以下世界书条目是项目硬设定，写作必须严格遵守世界观、地理、时代、习俗、语境约束。",
            "禁止引入与下列设定冲突的器物、地名、时代词汇、社会结构或科技层级。",
        ]

        for path in candidates:
            relative_path = path.relative_to(root).as_posix()
            preview = self._read_text_preview(path, max_chars=max_chars_per_file)
            preview = (preview or "").strip()
            if not preview:
                continue
            lines.extend(["", f"### {relative_path}", preview])

        if len(lines) <= 3:
            return ""
        return self._truncate_text("\n".join(lines).strip(), max_chars=total_chars)

    def _read_template_context(self, workspace_root: Path) -> Dict[str, str]:
        del workspace_root
        return {
            "project_rules": "",
            "variable_skill": "",
            "naming_skill": "",
            "preset_skill": "",
        }

    @staticmethod
    def _renderable_foreshadow_thread(thread_key: str, raw_thread: Dict[str, Any]) -> Dict[str, str]:
        thread_id = str(raw_thread.get("id") or thread_key).strip()
        status = str(raw_thread.get("status") or "open").strip().lower() or "open"
        planted = raw_thread.get("planted_at") if isinstance(raw_thread.get("planted_at"), dict) else {}
        callbacks = raw_thread.get("callbacks") if isinstance(raw_thread.get("callbacks"), list) else []
        latest_callback = next(
            (item for item in reversed(callbacks) if isinstance(item, dict)),
            {},
        )
        summary = str(latest_callback.get("summary") or planted.get("summary") or "").strip()
        evidence = str(latest_callback.get("evidence") or planted.get("evidence") or "").strip()
        return {
            "id": thread_id,
            "status": status,
            "summary": summary or thread_id,
            "evidence": evidence,
        }

    @staticmethod
    def _score_foreshadow_thread(thread: Dict[str, str], keywords: Set[str]) -> int:
        if not keywords:
            return 0
        text = " ".join(
            str(thread.get(key) or "")
            for key in ("id", "summary", "evidence")
        )
        score = 0
        for keyword in keywords:
            if not keyword:
                continue
            if keyword in text:
                score += 3
            elif len(keyword) >= 3 and keyword.lower() in text.lower():
                score += 3
        return score

    @staticmethod
    def _read_json(path: Path) -> Dict[str, Any]:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _normalize_relative_path(value: str) -> str:
        normalized = str(value or "").strip().replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized.lstrip("/")

    @staticmethod
    def _normalize_story_segment_format(value: Any) -> str:
        raw = str(value or "md").strip().lower()
        return "txt" if raw == "txt" else "md"

    @staticmethod
    def _character_template_key_from_title(title: str, index: int) -> str:
        normalized_title = str(title or "").strip()
        if normalized_title in _CHARACTER_TEMPLATE_TITLE_KEYS:
            return _CHARACTER_TEMPLATE_TITLE_KEYS[normalized_title]
        ascii_key = re.sub(r"[^a-z0-9_]+", "_", normalized_title.lower()).strip("_")
        return ascii_key or f"section_{index}"

    @staticmethod
    def _safe_leaf_name(value: str) -> str:
        compact = _INVALID_FILE_CHARS.sub("_", str(value or "").strip())
        compact = re.sub(r"\s+", " ", compact).strip()
        return compact[:48] if compact else "状态"

    @staticmethod
    def _truncate_text(value: str, max_chars: int = 2000) -> str:
        text = str(value or "").strip()
        if len(text) <= max_chars:
            return text
        return text[: max(0, max_chars - 18)] + "\n... [truncated]"


@lru_cache(maxsize=1)
def get_story_project_service() -> StoryProjectService:
    return StoryProjectService()


def _extract_rewrite_chapter_number(prompt: str) -> Optional[int]:
    match = _REWRITE_CHAPTER_RE.search(str(prompt or ""))
    if not match:
        return None
    raw = str(match.group(1) or "").strip()
    if raw.isdigit():
        return int(raw)
    parsed = StoryProjectService._parse_chapter_number(raw)
    return parsed or None
