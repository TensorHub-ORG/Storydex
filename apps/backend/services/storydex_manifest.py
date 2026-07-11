from __future__ import annotations

from pathlib import Path
from typing import List, TypedDict


class DirSpec(TypedDict, total=False):
    path: str
    layer: str
    role: str
    create_on_init: bool
    description: str
    readme: str


# This manifest is the single source of truth for the default Storydex project
# skeleton. It creates a compact but complete set of directories on project open.
STORYDEX_MANIFEST: List[DirSpec] = [
    {
        "path": "chapters",
        "layer": "content",
        "role": "story",
        "create_on_init": True,
        "description": "Story chapters and segments",
        "readme": "# 正文目录\n\n存放小说正文、章节和剧情片段。\n",
    },
    {
        "path": ".storydex",
        "layer": "project",
        "role": "root",
        "create_on_init": True,
        "description": "Storydex project metadata",
        "readme": "# Storydex 项目数据\n\n存放本项目的配置、设定、记忆、WIKI、审计和 Agent 运行资产。\n",
    },
    {
        "path": ".storydex/config",
        "layer": "project",
        "role": "settings",
        "create_on_init": True,
        "description": "Project settings",
        "readme": "# 项目配置\n\n存放 Storydex 项目级配置文件。\n",
    },
    {
        "path": ".storydex/presets",
        "layer": "project",
        "role": "presets",
        "create_on_init": True,
        "description": "Writing presets and style constraints",
        "readme": "# 创作预设\n\n存放当前启用预设指针和可管理的写作约束。\n",
    },
    {
        "path": ".storydex/presets/active",
        "layer": "project",
        "role": "presets",
        "create_on_init": True,
        "description": "Active runtime presets",
        "readme": "# 启用预设\n\n存放当前会影响生成的已审阅预设文件。\n",
    },
    {
        "path": ".storydex/presets/library",
        "layer": "project",
        "role": "presets",
        "create_on_init": True,
        "description": "Preset library",
        "readme": "# 预设库\n\n存放已导入但未直接启用的预设文件。\n",
    },
    {
        "path": ".storydex/presets/compiled",
        "layer": "project",
        "role": "presets",
        "create_on_init": True,
        "description": "Compiled safe presets",
        "readme": "# 编译预设\n\n存放经过清洗和编译后的安全预设。\n",
    },
    {
        "path": ".storydex/presets/blocked",
        "layer": "project",
        "role": "presets",
        "create_on_init": True,
        "description": "Blocked presets",
        "readme": "# 阻止预设\n\n存放被禁用或暂不允许进入运行时的预设。\n",
    },
    {
        "path": ".storydex/characters",
        "layer": "content",
        "role": "characters",
        "create_on_init": True,
        "description": "Character cards and character facts",
        "readme": "# 角色档案\n\n存放角色文件；新角色缺失信息时写为“未知”。\n",
    },
    {
        "path": ".storydex/worldbook",
        "layer": "content",
        "role": "worldbook",
        "create_on_init": True,
        "description": "Worldbuilding entries",
        "readme": "# 世界书\n\n存放设定、地点、势力、物品、功法等世界观条目。\n",
    },
    {
        "path": ".storydex/scripts",
        "layer": "content",
        "role": "scripts",
        "create_on_init": True,
        "description": "Plot scripts and outlines",
        "readme": "# 剧本与大纲\n\n存放剧情规划、章节规划、大纲和剧本。\n",
    },
    {
        "path": ".storydex/templates",
        "layer": "project",
        "role": "templates",
        "create_on_init": True,
        "description": "Project templates",
        "readme": "# 模板\n\n存放项目资产的格式模板。\n",
    },
    {
        "path": ".storydex/templates/characters",
        "layer": "project",
        "role": "templates",
        "create_on_init": True,
        "description": "Character templates",
        "readme": "# 角色模板\n\n存放角色档案的默认格式模板。\n",
    },
    {
        "path": ".storydex/templates/chapters",
        "layer": "project",
        "role": "templates",
        "create_on_init": True,
        "description": "Chapter directory templates",
        "readme": "# 章节目录模板\n\n存放新故事开始前可选择的章节与片段目录模板。\n",
    },
    {
        "path": ".storydex/memory",
        "layer": "project",
        "role": "memory",
        "create_on_init": True,
        "description": "Story state and summaries",
        "readme": """# Storydex 长期记忆与变量

本目录只保存需要跨会话长期使用的故事记忆与变量。禁止保存聊天记录、历史会话、执行过程、工具日志、任务方案和临时草稿；历史会话必须保存在 `.storydex/.agent/sessions/`。

目录采用受约束的自适应布局：AI可按项目实际需要创建模块，但必须优先复用现有模块，并在 `catalog.json` 或模块 README 中登记用途、数据类型、权威来源、读取场景、写入条件、消费者、schemaVersion、生命周期及 canonical/derived/index 分类。

正文与用户确认是最高优先级证据。正式状态必须使用稳定实体ID，并通过包含 baseRevision、来源章节、证据和操作列表的变更集校验后原子写入，同时追加 `change-ledger.jsonl`。删除、冲突、低置信度、角色合并和重大关系变化必须确认。派生摘要不得覆盖正式事实，变量思考 Markdown 只是辅助说明。

`.storydex/temp/` 是普通临时工作台。除非用户明确要求或当前任务依赖，否则 Agent 不得读取、检索或注入其中内容。旧项目迁移必须先备份并展示冲突，不自动删除旧数据；无用或重复模块只能建议合并或归档。
""",
    },
    {
        "path": ".storydex/memory/current-state",
        "layer": "project",
        "role": "memory",
        "create_on_init": True,
        "description": "Current variable state",
        "readme": "# 当前变量\n\n存放项目当前变量总状态和最新状态切片。\n",
    },
    {
        "path": ".storydex/memory/current",
        "layer": "project",
        "role": "memory",
        "create_on_init": True,
        "description": "Canonical entity, fact, and relationship memory",
        "readme": "# 当前事实记忆\n\n存放实体、事实和关系图等可审计的结构化故事记忆。\n",
    },
    {
        "path": ".storydex/memory/chapters",
        "layer": "project",
        "role": "memory",
        "create_on_init": True,
        "description": "Per-chapter variable snapshots",
        "readme": "# 章节变量快照\n\n存放每章或每个剧情片段对应的变量快照。\n",
    },
    {
        "path": ".storydex/wiki",
        "layer": "project",
        "role": "wiki",
        "create_on_init": True,
        "description": "Wiki entries and knowledge graph",
        "readme": "# WIKI\n\n存放 WIKI 条目、知识图谱和索引。\n",
    },
    {
        "path": ".storydex/temp",
        "layer": "project",
        "role": "temp",
        "create_on_init": True,
        "description": "Storydex project temporary files",
        "readme": """# 临时工作台

这是一个普通、灵活的临时文件夹，可用于角色草案、世界观设计、剧本、候选方案和其他中间文件。它没有索引、诊断、生命周期、自动清理或自动迁移等系统功能。

内容不是正式记忆，不能直接影响后续剧情。Agent只需知道这里可能有临时创作文件；除非用户明确要求或当前任务需要，否则不要读取、关注或注入上下文。Agent运行中间数据应放在 `.storydex/.agent/temp/`。
""",
    },
    {
        "path": ".storydex/.agent",
        "layer": "runtime",
        "role": "agent",
        "create_on_init": True,
        "description": "Agent runtime assets",
        "readme": "# Agent 运行资产\n\n存放技能、会话、计划和运行时临时文件。\n",
    },
    {
        "path": ".storydex/.agent/skills",
        "layer": "runtime",
        "role": "skills",
        "create_on_init": True,
        "description": "Agent skills",
        "readme": "# Agent 技能\n\n存放默认技能和项目自定义技能；Agent 使用技能时从这里加载。\n",
    },
    {
        "path": ".storydex/.agent/sessions",
        "layer": "runtime",
        "role": "sessions",
        "create_on_init": True,
        "description": "Agent sessions and working memory",
        "readme": "# Agent 会话\n\n存放会话历史、工作记忆和上下文压缩记录。\n",
    },
    {
        "path": ".storydex/.agent/plans",
        "layer": "runtime",
        "role": "plans",
        "create_on_init": True,
        "description": "Agent plans",
        "readme": "# Agent 计划\n\n存放计划文档、长任务拆解和执行路线。\n",
    },
    {
        "path": ".storydex/.agent/temp",
        "layer": "runtime",
        "role": "agent_temp",
        "create_on_init": True,
        "description": "Agent runtime temporary files",
        "readme": "# Agent 临时文件\n\n存放 Agent 运行时产生的临时文件。\n",
    },
]


def ensure_manifest(project_root: Path, *, allow_legacy: bool = False) -> List[str]:
    del allow_legacy
    project_root = Path(project_root).resolve()
    project_root.mkdir(parents=True, exist_ok=True)

    created: List[str] = []
    for spec in STORYDEX_MANIFEST:
        if not spec["create_on_init"]:
            continue
        relative_path = spec["path"]
        target = project_root / relative_path
        if _is_manifest_file(relative_path):
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_text("{}\n", encoding="utf-8")
                created.append(relative_path)
            continue
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
            created.append(relative_path)
        _ensure_directory_readme(target, spec)
    return created


def manifest_paths(*, only_create_on_init: bool = False, directories_only: bool = False) -> List[str]:
    specs = [spec for spec in STORYDEX_MANIFEST if spec["create_on_init"] or not only_create_on_init]
    paths = [spec["path"] for spec in specs]
    if directories_only:
        return [path for path in paths if not _is_manifest_file(path)]
    return paths


def _is_manifest_file(relative_path: str) -> bool:
    return relative_path.endswith(".json")


def _ensure_directory_readme(target: Path, spec: DirSpec) -> None:
    content = str(spec.get("readme") or "").strip()
    if not content:
        return
    readme = target / "README.md"
    if readme.exists():
        return
    readme.write_text(content + "\n", encoding="utf-8")
