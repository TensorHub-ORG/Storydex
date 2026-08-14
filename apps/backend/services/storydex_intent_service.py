"""Storydex 意图识别服务（项目语义接地版）。

两层路由（layered intent routing）：
1. 纯语法信号（slash 命令、空输入）直接短路；
2. LLM 输出正交的 artifact/effect/constraints/evidence 结构，应用代码据此
   编译写权限。超时、截断、歧义或非法 JSON 一律关闭写权限并安全澄清，
   不使用关键词正则猜测语义写操作。

项目语义接地：
- 意图目录（intent catalog）在内置标签之上动态合并项目
  `.storydex/.agent/skills/registry.json`：每个意图携带资产落点
  （assetTargets，如 character_work → .storydex/characters/）与
  项目已注册技能名；自定义技能声明的新 intent 会成为可选标签。
- 分类结果帧携带 assetTargets / matchedSkills，下游（TurnContract
  system prompt、任务规划器）据此知道该意图的产出应写到哪里、
  该用哪些技能。
- 会话级上一轮记忆（prompt + 意图）注入分类上下文，使"继续"
  "然后呢"等省略式请求能延续正确意图。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List


INTENT_LABELS: tuple[str, ...] = (
    "story_generation",
    "character_work",
    "worldbook_work",
    "script_work",
    "wiki_work",
    "project_organization",
    "general",
)
_CONFIDENCE_LEVELS = {"high", "medium", "low"}
# 操作类型：区分"新建内容"与"修改现有文件"，这是修复"重构被误判为剧情生成"的关键维度。
_OPERATION_TYPES = {"create_new", "modify_existing", "inquiry", "greeting", "other"}
_COMPLEXITY_LEVELS = {"simple", "complex"}
_DECISIONS = {"decided", "needs_clarification"}
_EFFECTS = {"respond_only", "create", "modify", "delete", "execute"}
_WRITE_EFFECTS = {"create", "modify", "delete", "execute"}
_ARTIFACTS = {
    "chapter_prose",
    "plot_plan",
    "character",
    "worldbook",
    "wiki",
    "project_files",
    "app_help",
    "general",
}
_TARGET_SCOPES = {
    "none",
    "current_fragment",
    "current_chapter",
    "next_chapter",
    "chapter_number",
    "named_asset",
}
_NO_PROJECT_WRITE = "no_project_write"
_ARTIFACT_BY_PRIMARY = {
    "story_generation": "chapter_prose",
    "character_work": "character",
    "worldbook_work": "worldbook",
    "script_work": "plot_plan",
    "wiki_work": "wiki",
    "project_organization": "project_files",
    "general": "general",
}
_DEFAULT_OPERATION_TYPE = "other"
_DEFAULT_COMPLEXITY = "simple"
_INTENT_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
DEFAULT_LLM_TIMEOUT_SECONDS = 20.0
_MAX_SESSION_MEMORY = 256
_INTENT_MAX_OUTPUT_TOKENS = 384
_ADVISORY_RE = re.compile(
    r"(建议|意见|评价|点评|评估|分析一下|怎么看|怎么样|写得如何|写得怎么样|如何看|你觉得|好不好|是否合理|"
    r"有什么问题|哪里有问题|优缺点|可行吗|应该吗|怎么理解|为什么|如何|怎样|"
    r"advice|suggest(?:ion)?|review|evaluate|assessment|opinion|what do you think|"
    r"how (?:should|can|do)|why\b|explain)",
    re.IGNORECASE,
)
_MUTATION_REQUEST_RE = re.compile(
    r"(请|帮我|替我|直接|立即|现在|需要你)?.{0,8}"
    r"(修改|改成|改写|重写|续写|扩写|创建|生成|新增|添加|删除|移除|更新|整理|同步|"
    r"归档|执行|实现|修复|写入|保存|落盘|提交|应用|替换|移动|重命名|"
    r"edit|rewrite|continue writing|create|generate|add|delete|remove|update|organize|"
    r"sync|execute|implement|fix|save|apply|replace|move|rename)",
    re.IGNORECASE,
)

# 内置意图目录：描述、资产落点（与 TurnContract assetTargets 对齐）、少样本示例。
_BUILTIN_INTENT_CATALOG: Dict[str, Dict[str, Any]] = {
    "story_generation": {
        "description": "撰写、续写、改写或扩写小说正文（章节、场景、片段、正文）",
        "assetTargets": ["chapters/", ".storydex/memory/chapters/"],
        "examples": ["续写下一段", "然后呢", "写第三章的开头"],
    },
    "character_work": {
        "description": "创建或更新角色卡、人物设定、性格、背景与人物关系",
        "assetTargets": [".storydex/characters/"],
        "examples": ["设计一个反派角色", "把女主的背景改成孤儿出身"],
    },
    "worldbook_work": {
        "description": "创建或更新世界书/世界观/设定集条目（地理、势力、魔法体系、历史等）",
        "assetTargets": [".storydex/worldbook/"],
        "examples": ["完善大陆的魔法体系设定", "给北境王国加一条世界书"],
    },
    "script_work": {
        "description": "设计剧本、大纲、分镜、台词或情节骨架",
        "assetTargets": [".storydex/scripts/"],
        "examples": ["帮我列一份第二卷的大纲", "把这场冲突写成剧本"],
    },
    "wiki_work": {
        "description": "整理或同步项目 WIKI / 知识图谱（实体、关系、伏笔、设定关系）",
        "assetTargets": [".storydex/wiki/"],
        "examples": ["整理一下知识图谱", "把最近几章的设定同步到 WIKI"],
    },
    "project_organization": {
        "description": "整理项目目录或文件结构",
        "assetTargets": [".storydex/", "chapters/"],
        "examples": ["整理一下项目目录"],
    },
    "general": {
        "description": "提问、闲聊、反馈、软件使用问题或其他不属于以上类别的请求",
        "assetTargets": [],
        "examples": ["这个软件怎么导出章节", "你觉得这段写得怎么样"],
    },
}

# 旧调用方与纯语法短路的兼容解析器；自然语言 Provider 失败时不调用它。
_STORY_INTENT_RE = re.compile(
    r"(续写|写(一|1)?段|写第|生成.*(剧情|故事|章节|片段)|创作.*(剧情|故事)|正文|剧情|章节|片段|story|chapter|scene|continue)",
    re.IGNORECASE,
)
_CHARACTER_INTENT_RE = re.compile(r"(角色|人物|character|cast)", re.IGNORECASE)
_WORLDBOOK_INTENT_RE = re.compile(r"(世界书|世界观|设定集|worldbook|lorebook|lore)", re.IGNORECASE)
_SCRIPT_INTENT_RE = re.compile(r"(剧本|分镜|台词|大纲|screenplay|script)", re.IGNORECASE)
_WIKI_INTENT_RE = re.compile(r"(wiki|知识图谱|知识库|整理设定|整理关系)", re.IGNORECASE)
_EXPLICIT_KNOWLEDGE_BINDING_RE = re.compile(
    r"(绑定|关联|隶属|栖息于|位于|属于|拥有|服务于)",
    re.IGNORECASE,
)
_EXPLICIT_KNOWLEDGE_CONFIRMATION_RE = re.compile(
    r"^\s*(?:确认(?:写入|应用|执行)?|同意|可以|好的?|执行|应用|继续)(?:吧|。|！|!|，.*)?\s*$",
    re.IGNORECASE,
)
_EXPLICIT_KNOWLEDGE_PREPARE_RE = re.compile(r"\bprepare_explicit\b", re.IGNORECASE)
_EXPLICIT_KNOWLEDGE_APPLY_RE = re.compile(r"\bapply_explicit\b", re.IGNORECASE)
_BROAD_NO_PROJECT_WRITE_RE = re.compile(
    r"(?:不要|不得|禁止|请勿|无需|无须|不用|不必|别|切勿)"
    r"[^，,。！？!?；;\n]{0,24}"
    r"(?:任何|全部|所有)?[^，,。！？!?；;\n]{0,8}"
    r"(?:写入|修改|保存|落盘|改变)(?:任何|全部|所有)?(?:项目|文件|内容)?|"
    r"(?:do\s+not|don't|never|must\s+not)\s+"
    r"[^.,;!?\n]{0,24}(?:write|modify|save|change)\s+(?:any\s+)?(?:project\s+)?files?",
    re.IGNORECASE,
)
_EXPLICIT_GLOBAL_NO_WRITE_RE = re.compile(
    r"(?:不要|不得|禁止|请勿|无需|无须|不用|不必|别|切勿)"
    r"[^，,。！？!?；;\n]{0,24}(?:写入|修改|保存|落盘|改变)"
    r"[^，,。！？!?；;\n]{0,12}(?:任何|全部|所有)(?:项目)?文件|"
    r"(?:do\s+not|don't|never|must\s+not)\s+"
    r"[^.,;!?\n]{0,24}(?:write|modify|save|change)\s+"
    r"(?:any|all)\s+(?:project\s+)?files?",
    re.IGNORECASE,
)
_PROJECT_ORGANIZE_RE = re.compile(
    r"(整理目录|项目目录|整理项目|目录结构|组织方式|资料整理|盘点.*(?:章节|目录)|organize)",
    re.IGNORECASE,
)

# 旧注入帧的 operationType 兼容解析：
# 修改现有文件的强信号——重构/整理/调整/清理/更新既有内容，绝不是"新建"。
_MODIFY_EXISTING_RE = re.compile(
    r"(重构|重写|改写|重新组织|重新整理|整理一下|梳理|调整|优化|修订|修改|修正|清理|删除|移除|"
    r"归档|合并|拆分|同步|refactor|restructure|reorganize|rework|revise|adjust|clean\s*up|"
    r"tidy|consolidate|merge|split|update\s+the\s+existing)",
    re.IGNORECASE,
)
# 新建内容的强信号——续写/新增/生成全新片段。
_CREATE_NEW_RE = re.compile(
    r"(续写|新写|新增|新建|创建|再写|再来(一|1)?段|生成.*(剧情|故事|章节|片段|新)|创作|写第|写一段新|"
    r"continue\s+writing|write\s+(a\s+)?new|add\s+(a\s+)?new|generate\s+(a\s+)?new|create\s+(a\s+)?new)",
    re.IGNORECASE,
)
_NEGATED_OPERATION_PREFIX_RE = re.compile(
    r"(?:不要|请勿|禁止|无需|无须|不用|不必|避免|不得|切勿|别|"
    r"do\s+not|don't|must\s+not|never)\s*[^，,。！？!?；;\n]{0,32}$",
    re.IGNORECASE,
)
_PASSIVE_OPERATION_PREFIX_RE = re.compile(r"(?:被|已被|曾被|遭|遭到)\s*$", re.IGNORECASE)
_OPERATION_CLAUSE_BOUNDARIES = "，,。！？!?；;\n"
# 问候语。
_GREETING_RE = re.compile(
    r"^\s*(你好|您好|hi|hello|hey|在吗|在么|哈喽|嗨|早上好|下午好|晚上好|早安|晚安|"
    r"good\s+(morning|afternoon|evening|night))[\s。.!！?？~～]*$",
    re.IGNORECASE,
)
_EXPLICIT_FILE_READ_RE = re.compile(
    r"(读取|阅读|查看|检查|打开|总结|概括|read|inspect|review|summari[sz]e)",
    re.IGNORECASE,
)
_FILE_PATH_SIGNAL_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|(?:^|[\s（(\[\"'`])(?:\.{0,2}[\\/])?[^\s，,。！？!?；;]+[\\/]"
    r"|[^\s，,。！？!?；;\\/]+\.(?:md|txt|json|jsonl|yaml|yml|toml|py|rs|ts|tsx|js|jsx|vue))",
    re.IGNORECASE,
)


def _has_positive_operation_match(
    pattern: re.Pattern[str],
    text: str,
    *,
    ignore_passive_description: bool = False,
) -> bool:
    for match in pattern.finditer(text):
        clause_start = max(
            (text.rfind(boundary, 0, match.start()) for boundary in _OPERATION_CLAUSE_BOUNDARIES),
            default=-1,
        )
        prefix = text[clause_start + 1 : match.start()]
        if _NEGATED_OPERATION_PREFIX_RE.search(prefix):
            continue
        if ignore_passive_description and _PASSIVE_OPERATION_PREFIX_RE.search(prefix):
            continue
        return True
    return False


def _heuristic_operation_type(text: str, *, primary: str) -> str:
    """为没有 v2 语义字段的旧调用方推断 operationType。"""
    stripped = str(text or "").strip()
    if not stripped:
        return _DEFAULT_OPERATION_TYPE
    if _GREETING_RE.match(stripped):
        return "greeting"
    has_modify = _has_positive_operation_match(
        _MODIFY_EXISTING_RE,
        stripped,
        ignore_passive_description=True,
    )
    has_create = _has_positive_operation_match(_CREATE_NEW_RE, stripped)
    if has_modify and not has_create:
        return "modify_existing"
    if has_create and not has_modify:
        return "create_new"
    if has_modify and has_create:
        # 同时出现时（如"重构后再新增一段"），以修改现有为主，避免误触发新片段规划。
        return "modify_existing"
    # 无明确读写动词：咨询/讨论类归为 inquiry；有明确内容意图但无动词的兜底为 create_new。
    if is_advisory_request(stripped):
        return "inquiry"
    if primary in {"general"}:
        return "inquiry"
    return "create_new"


def build_intent_catalog(
    *,
    workspace_root: Path | None = None,
    story_project_service: Any = None,
) -> Dict[str, Dict[str, Any]]:
    """内置目录 + 项目 skill registry 合并出的意图目录。

    registry 中每个技能按其声明的 intent 归入对应条目（技能名进 skills、
    assetTargets 合并去重）；声明了未知 intent 的自定义技能会新增一个
    可选标签，使按项目扩展的技能也能被路由到。
    """
    catalog: Dict[str, Dict[str, Any]] = {
        label: {
            "description": str(entry.get("description") or ""),
            "assetTargets": list(entry.get("assetTargets") or []),
            "skills": [],
            "examples": list(entry.get("examples") or []),
        }
        for label, entry in _BUILTIN_INTENT_CATALOG.items()
    }
    if workspace_root is None:
        return catalog
    try:
        if story_project_service is None:
            from services.story_project_service import get_story_project_service

            story_project_service = get_story_project_service()
        registry = story_project_service.read_agent_skill_registry(Path(workspace_root))
    except Exception:
        return catalog
    skills = registry.get("skills") if isinstance(registry, dict) and isinstance(registry.get("skills"), list) else []
    for item in skills:
        if not isinstance(item, dict):
            continue
        intent = str(item.get("intent") or "").strip()
        name = str(item.get("name") or item.get("id") or "").strip()
        if not intent or not _INTENT_SLUG_RE.match(intent):
            continue
        entry = catalog.setdefault(
            intent,
            {"description": f"项目自定义技能意图（{name}）", "assetTargets": [], "skills": [], "examples": []},
        )
        if name and name not in entry["skills"]:
            entry["skills"].append(name)
        targets = item.get("assetTargets") if isinstance(item.get("assetTargets"), list) else []
        for target in targets:
            normalized = str(target or "").strip()
            if normalized and normalized not in entry["assetTargets"]:
                entry["assetTargets"].append(normalized)
    return catalog


def heuristic_intent_frame(*, prompt: str, active_file: str) -> Dict[str, Any]:
    """兼容旧调用方；不得用于自然语言 Provider 失败后的写权限授权。"""
    text = str(prompt or "")
    signals: List[str] = []
    primary = "general"
    if _EXPLICIT_KNOWLEDGE_BINDING_RE.search(text):
        primary = "wiki_work"
        signals.append("explicit_knowledge_binding")
    elif _PROJECT_ORGANIZE_RE.search(text):
        primary = "project_organization"
        signals.append("project_organization_keywords")
    elif _STORY_INTENT_RE.search(text):
        primary = "story_generation"
        signals.append("story_keywords")
    elif _CHARACTER_INTENT_RE.search(text):
        primary = "character_work"
        signals.append("character_keywords")
    elif _WORLDBOOK_INTENT_RE.search(text):
        primary = "worldbook_work"
        signals.append("worldbook_keywords")
    elif _SCRIPT_INTENT_RE.search(text):
        primary = "script_work"
        signals.append("script_keywords")
    elif _WIKI_INTENT_RE.search(text):
        primary = "wiki_work"
        signals.append("wiki_keywords")
    if active_file.startswith("chapters/") and primary == "general":
        primary = "story_generation"
        signals.append("active_chapter_file")
    operation_type = (
        "modify_existing"
        if _EXPLICIT_KNOWLEDGE_BINDING_RE.search(text)
        else _heuristic_operation_type(text, primary=primary)
    )
    effect = _effect_from_operation_type(operation_type)
    return {
        "primary": primary,
        "confidence": "medium" if signals else "low",
        "signals": signals,
        "method": "heuristic",
        "operationType": operation_type,
        "decision": "decided",
        "effect": effect,
        "artifact": _ARTIFACT_BY_PRIMARY.get(primary, "general"),
        "targetScope": "none",
        "targetValue": "",
        "explicitConstraints": [],
        "ambiguities": [],
        "evidence": [],
        "canWrite": effect in _WRITE_EFFECTS,
        "complexity": _heuristic_complexity(text),
        **(
            {
                "knowledgeWriteMode": "explicit_binding",
                "knowledgeConfirmationRequired": True,
                "knowledgeConfirmed": False,
            }
            if _EXPLICIT_KNOWLEDGE_BINDING_RE.search(text)
            else {}
        ),
    }


def is_explicit_knowledge_binding_request(prompt: Any) -> bool:
    return bool(_EXPLICIT_KNOWLEDGE_BINDING_RE.search(str(prompt or "")))


def _effect_from_operation_type(operation_type: str) -> str:
    normalized = str(operation_type or "").strip().lower()
    if normalized == "create_new":
        return "create"
    if normalized == "modify_existing":
        return "modify"
    return "respond_only"


def _operation_type_from_effect(effect: str, *, requested_operation: str = "") -> str:
    normalized = str(effect or "").strip().lower()
    if normalized == "create":
        return "create_new"
    if normalized in {"modify", "delete", "execute"}:
        return "modify_existing"
    return "greeting" if str(requested_operation or "").strip().lower() == "greeting" else "inquiry"


def intent_frame_allows_project_writes(frame: Dict[str, Any] | None) -> bool:
    """Compile the model's semantic decision into a project-write capability.

    New structured frames carry an explicit ``canWrite`` bit. Legacy injected
    frames remain compatible, but only the two historical mutation operations
    can authorise writes. Missing or unknown operations are read-only.
    """

    payload = frame if isinstance(frame, dict) else {}
    if "canWrite" in payload:
        return bool(payload.get("canWrite"))
    if str(payload.get("decision") or "decided").strip().lower() != "decided":
        return False
    constraints = payload.get("explicitConstraints")
    if isinstance(constraints, list) and _NO_PROJECT_WRITE in {
        str(item or "").strip().lower() for item in constraints
    }:
        return False
    effect = str(payload.get("effect") or "").strip().lower()
    if effect:
        return effect in _WRITE_EFFECTS
    return str(payload.get("operationType") or "").strip().lower() in {
        "create_new",
        "modify_existing",
    }


def safe_fallback_intent_frame(*, reason: str = "intent_classifier_unavailable") -> Dict[str, Any]:
    """Return a fail-closed frame without guessing semantics from user text."""

    normalized_reason = str(reason or "intent_classifier_unavailable").strip()[:120]
    return {
        "primary": "general",
        "confidence": "low",
        "signals": [normalized_reason],
        "method": "safe_fallback",
        "reason": "Intent could not be classified reliably; project writes are disabled for this turn.",
        "operationType": "inquiry",
        "decision": "needs_clarification",
        "effect": "respond_only",
        "artifact": "general",
        "targetScope": "none",
        "targetValue": "",
        "explicitConstraints": [_NO_PROJECT_WRITE],
        "ambiguities": [normalized_reason],
        "evidence": [],
        "canWrite": False,
        "complexity": _DEFAULT_COMPLEXITY,
        "assetTargets": [],
        "matchedSkills": [],
    }


def _heuristic_complexity(text: str) -> str:
    """为没有 v2 语义字段的旧调用方推断任务复杂度。"""
    stripped = str(text or "").strip()
    if not stripped:
        return _DEFAULT_COMPLEXITY
    # 多步骤连接词 + 多个动作动词 → 复杂任务（如"重构后更新变量和WIKI"）。
    step_markers = len(re.findall(r"(然后|接着|之后|再|并且|同时|以及|、|，然后)", stripped))
    mutation_verbs = len(_MUTATION_REQUEST_RE.findall(stripped))
    if step_markers >= 2 and mutation_verbs >= 2:
        return "complex"
    if mutation_verbs >= 3:
        return "complex"
    return _DEFAULT_COMPLEXITY


def is_advisory_request(prompt: str) -> bool:
    """Return True for requests that ask for judgment or guidance, not mutation.

    In particular, the bare Chinese character ``写`` is intentionally not a
    mutation signal: phrases such as ``这段写得怎么样`` are advisory.
    """

    normalized = " ".join(str(prompt or "").strip().split())
    if not normalized or normalized.startswith("/"):
        return False
    return bool(_ADVISORY_RE.search(normalized)) and not bool(_MUTATION_REQUEST_RE.search(normalized))


def _is_explicit_read_only_file_request(prompt: str) -> bool:
    text = str(prompt or "")
    return bool(
        _EXPLICIT_GLOBAL_NO_WRITE_RE.search(text)
        and _EXPLICIT_FILE_READ_RE.search(text)
        and _FILE_PATH_SIGNAL_RE.search(text)
    )


class _BoundedIntentProvider:
    """Apply Storydex metadata-call limits at the concrete provider boundary."""

    def __init__(self, provider: Any) -> None:
        object.__setattr__(self, "_provider", provider)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_provider":
            object.__setattr__(self, name, value)
        else:
            setattr(self._provider, name, value)

    async def chat(
        self,
        messages: list[Dict[str, Any]],
        tools: list[Dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> Any:
        del tools, kwargs
        direct = await _bounded_metadata_chat(self._provider, messages)
        if direct is not None:
            return direct
        request_options = getattr(self._provider, "storydex_intent_request_options", None)
        if callable(request_options):
            configured = request_options()
            bounded_options = dict(configured) if isinstance(configured, dict) else {}
            return await _invoke_provider_chat(self._provider, messages, **bounded_options)
        chat = getattr(self._provider, "chat", None)
        try:
            parameter = inspect.signature(chat).parameters.get("max_output_tokens")
        except (TypeError, ValueError):
            parameter = None
        bounded_kwargs = (
            {"max_output_tokens": _INTENT_MAX_OUTPUT_TOKENS}
            if parameter is not None and parameter.kind is not inspect.Parameter.VAR_KEYWORD
            else {}
        )
        return await _invoke_provider_chat(self._provider, messages, **bounded_kwargs)


async def _bounded_metadata_chat(provider: Any, messages: list[Dict[str, Any]]) -> Any | None:
    """Use strict, short, low-reasoning requests when the provider exposes its client.

    The pinned Coomi providers accept ``**kwargs`` but currently do not forward
    metadata options to their SDK clients.  Storydex keeps this narrow adapter
    local to intent routing so the desktop wheel remains untouched and normal
    agent calls retain their existing provider behavior.
    """

    config = getattr(provider, "config", None)
    provider_type = _normalize_provider_mode(getattr(config, "type", ""))
    model = str(getattr(provider, "model", "") or "").strip()
    client = getattr(provider, "client", None)
    schema = _intent_response_schema()

    if provider_type == "openai_responses" and model:
        create_response = getattr(getattr(client, "responses", None), "create", None)
        build_params = getattr(provider, "_build_params", None)
        if callable(create_response) and callable(build_params):
            try:
                params = dict(build_params(messages, None, stream=False))
                params.update(
                    {
                        "text": {
                            "format": {
                                "type": "json_schema",
                                "name": "storydex_intent",
                                "schema": schema,
                                "strict": True,
                            }
                        },
                        "max_output_tokens": _INTENT_MAX_OUTPUT_TOKENS,
                        "store": False,
                    }
                )
                if _is_reasoning_model(model):
                    params["reasoning"] = {"effort": "low"}
                else:
                    params["temperature"] = 0
                raw_response = create_response(**params)
                if inspect.isawaitable(raw_response):
                    raw_response = await raw_response
                return _metadata_llm_response(
                    content=str(getattr(raw_response, "output_text", "") or ""),
                )
            except Exception:
                # A direct metadata request is already a physical provider call.
                # Propagate failure so classification fails closed instead of
                # silently issuing a second, unconstrained request.
                raise

    completions = getattr(getattr(client, "chat", None), "completions", None)
    create_completion = getattr(completions, "create", None)
    if provider_type == "openai_compatible" and callable(create_completion) and model:
        params: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            # Compatible relays vary widely in JSON-schema support. JSON
            # object mode remains the bounded common denominator while the
            # system prompt supplies the exact schema.
            "response_format": {"type": "json_object"},
        }
        if _is_deepseek_model(model):
            params.update(
                {
                    "max_tokens": _INTENT_MAX_OUTPUT_TOKENS,
                    "temperature": 0,
                    # DeepSeek thinking is on by default, and reasoning_effort
                    # may be promoted by agent-compatible relays. Disable it at
                    # the provider-native layer for this small metadata call.
                    "extra_body": {"thinking": {"type": "disabled"}},
                }
            )
        elif _is_reasoning_model(model):
            params.update(
                {
                    "max_completion_tokens": _INTENT_MAX_OUTPUT_TOKENS,
                    "reasoning_effort": "low",
                }
            )
        else:
            params.update({"max_tokens": _INTENT_MAX_OUTPUT_TOKENS, "temperature": 0})
        try:
            raw_response = create_completion(**params)
            if inspect.isawaitable(raw_response):
                raw_response = await raw_response
            choice = raw_response.choices[0]
            message = choice.message
            return _metadata_llm_response(
                content=str(getattr(message, "content", "") or ""),
                reasoning_content=str(getattr(message, "reasoning_content", "") or ""),
            )
        except Exception:
            # Do not turn one failed direct request into a second provider call.
            raise

    create_message = getattr(getattr(client, "messages", None), "create", None)
    convert_messages = getattr(provider, "_convert_messages", None)
    if callable(create_message) and callable(convert_messages) and model:
        try:
            system, converted = convert_messages(messages)
            params = {
                "model": model,
                "max_tokens": _INTENT_MAX_OUTPUT_TOKENS,
                "temperature": 0,
                "messages": converted,
            }
            if system:
                params["system"] = system
            raw_response = create_message(**params)
            if inspect.isawaitable(raw_response):
                raw_response = await raw_response
            content = "".join(
                str(getattr(block, "text", "") or "")
                for block in (getattr(raw_response, "content", None) or [])
                if str(getattr(block, "type", "") or "") == "text"
            )
            return _metadata_llm_response(content=content)
        except Exception:
            raise
    return None


def _intent_response_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "decision": {"type": "string", "enum": ["decided", "needs_clarification"]},
            "primary": {"type": "string"},
            "secondary": {"type": "string"},
            "operationType": {
                "type": "string",
                "enum": ["create_new", "modify_existing", "inquiry", "greeting", "other"],
            },
            "effect": {
                "type": "string",
                "enum": ["respond_only", "create", "modify", "delete", "execute"],
            },
            "artifact": {
                "type": "string",
                "enum": [
                    "chapter_prose",
                    "plot_plan",
                    "character",
                    "worldbook",
                    "wiki",
                    "project_files",
                    "app_help",
                    "general",
                ],
            },
            "targetScope": {
                "type": "string",
                "enum": [
                    "none",
                    "current_fragment",
                    "current_chapter",
                    "next_chapter",
                    "chapter_number",
                    "named_asset",
                ],
            },
            "targetValue": {"type": "string"},
            "explicitConstraints": {
                "type": "array",
                "items": {"type": "string", "enum": ["no_project_write"]},
                "maxItems": 3,
            },
            "ambiguities": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 3,
            },
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 3,
            },
            "complexity": {"type": "string", "enum": ["simple", "complex"]},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "reason": {"type": "string"},
        },
        "required": [
            "decision",
            "primary",
            "secondary",
            "operationType",
            "effect",
            "artifact",
            "targetScope",
            "targetValue",
            "explicitConstraints",
            "ambiguities",
            "evidence",
            "complexity",
            "confidence",
            "reason",
        ],
    }


def _normalize_provider_mode(value: Any) -> str:
    normalized = str(value or "openai_compatible").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"openai", "responses", "response", "openai_response", "openai_responses"}:
        return "openai_responses"
    if normalized in {"anthropic", "anthropic_message", "anthropic_messages", "messages"}:
        return "anthropic_messages"
    return "openai_compatible"


def _is_reasoning_model(model: str) -> bool:
    lowered = str(model or "").strip().lower()
    return lowered.startswith(("o1", "o3", "o4")) or "gpt-5" in lowered


def _is_deepseek_model(model: str) -> bool:
    return "deepseek" in str(model or "").strip().lower()


async def _invoke_provider_chat(
    provider: Any,
    messages: list[Dict[str, Any]],
    **kwargs: Any,
) -> Any:
    chat = getattr(provider, "chat")
    if inspect.iscoroutinefunction(chat):
        return await chat(messages, None, **kwargs)
    response = await asyncio.to_thread(chat, messages, None, **kwargs)
    return await response if inspect.isawaitable(response) else response


def _metadata_llm_response(*, content: str, reasoning_content: str = "") -> Any:
    from services.coomi_bridge_client import BridgeLLMResponse

    return BridgeLLMResponse(
        content=content,
        tool_calls=None,
        usage=None,
        reasoning_content=reasoning_content or None,
    )


def is_valid_intent_frame(frame: Any) -> bool:
    """校验分类管线产出的意图帧：primary 为合法 slug 且带 method 出处标记。"""
    if not isinstance(frame, dict):
        return False
    primary = str(frame.get("primary") or "")
    if not _INTENT_SLUG_RE.match(primary):
        return False
    return bool(str(frame.get("method") or ""))


def _extract_json_object(content: str) -> Any:
    text = str(content or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*|\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None


def _normalized_string_list(value: Any, *, limit: int = 3, max_chars: int = 160) -> List[str]:
    if not isinstance(value, list):
        return []
    result: List[str] = []
    for item in value:
        normalized = str(item or "").strip()
        if normalized and normalized not in result:
            result.append(normalized[:max_chars])
        if len(result) >= limit:
            break
    return result


def _parse_intent_frame(
    content: str,
    *,
    valid_labels: set[str],
    prompt: str = "",
    require_v2: bool = False,
) -> Dict[str, Any] | None:
    payload = _extract_json_object(content)
    if not isinstance(payload, dict):
        return None
    if require_v2:
        required_v2_fields = {
            "decision",
            "primary",
            "secondary",
            "operationType",
            "effect",
            "artifact",
            "targetScope",
            "targetValue",
            "explicitConstraints",
            "ambiguities",
            "evidence",
            "complexity",
            "confidence",
            "reason",
        }
        if not required_v2_fields.issubset(payload):
            return None
        if (
            str(payload.get("decision") or "").strip().lower() not in _DECISIONS
            or str(payload.get("operationType") or "").strip().lower() not in _OPERATION_TYPES
            or str(payload.get("effect") or "").strip().lower() not in _EFFECTS
            or str(payload.get("artifact") or "").strip().lower() not in _ARTIFACTS
            or str(payload.get("targetScope") or "").strip().lower() not in _TARGET_SCOPES
            or str(payload.get("complexity") or "").strip().lower() not in _COMPLEXITY_LEVELS
            or str(payload.get("confidence") or "").strip().lower() not in _CONFIDENCE_LEVELS
            or not isinstance(payload.get("explicitConstraints"), list)
            or not isinstance(payload.get("ambiguities"), list)
            or not isinstance(payload.get("evidence"), list)
        ):
            return None
    primary = str(payload.get("primary") or "").strip()
    if primary not in valid_labels:
        return None
    secondary = str(payload.get("secondary") or "").strip()
    if secondary not in valid_labels or secondary == primary:
        secondary = ""
    confidence = str(payload.get("confidence") or "").strip().lower()
    if confidence not in _CONFIDENCE_LEVELS:
        confidence = "medium"
    reason = str(payload.get("reason") or "").strip()
    decision = str(payload.get("decision") or "decided").strip().lower()
    if decision not in _DECISIONS:
        decision = "needs_clarification"

    requested_operation = str(payload.get("operationType") or "").strip().lower()
    if requested_operation not in _OPERATION_TYPES:
        requested_operation = ""
    effect = str(payload.get("effect") or "").strip().lower()
    has_structured_effect = effect in _EFFECTS
    if not has_structured_effect:
        effect = _effect_from_operation_type(requested_operation)
    operation_type = (
        _operation_type_from_effect(effect, requested_operation=requested_operation)
        if has_structured_effect
        else requested_operation or "inquiry"
    )

    artifact = str(payload.get("artifact") or "").strip().lower()
    if artifact not in _ARTIFACTS:
        artifact = _ARTIFACT_BY_PRIMARY.get(primary, "general")
    target_scope = str(payload.get("targetScope") or "none").strip().lower()
    if target_scope not in _TARGET_SCOPES:
        target_scope = "none"
    target_value = str(payload.get("targetValue") or "").strip()[:160]
    constraints = [
        item
        for item in _normalized_string_list(payload.get("explicitConstraints"))
        if item == _NO_PROJECT_WRITE
    ]
    ambiguities = _normalized_string_list(payload.get("ambiguities"))
    evidence = _normalized_string_list(payload.get("evidence"), max_chars=80)

    # For structured frames, a write capability must be grounded in at least
    # one exact span from the user's current message. This is semantic evidence
    # supplied by the model, not a keyword list maintained by application code.
    grounded_write_evidence = any(item in str(prompt or "") for item in evidence)
    if has_structured_effect and effect in _WRITE_EFFECTS and not grounded_write_evidence:
        decision = "needs_clarification"
        ambiguities = [*ambiguities, "missing_grounded_write_evidence"][:3]
    if decision != "decided" or _NO_PROJECT_WRITE in constraints:
        effect = "respond_only"
        operation_type = "inquiry"

    complexity = str(payload.get("complexity") or "").strip().lower()
    if complexity not in _COMPLEXITY_LEVELS:
        complexity = _DEFAULT_COMPLEXITY
    can_write = bool(
        decision == "decided"
        and effect in _WRITE_EFFECTS
        and _NO_PROJECT_WRITE not in constraints
        and (grounded_write_evidence or not has_structured_effect)
    )
    frame = {
        "schemaVersion": 2,
        "primary": primary,
        "confidence": confidence,
        "signals": ["llm_classifier"],
        "method": "llm",
        "reason": reason[:200],
        "operationType": operation_type,
        "decision": decision,
        "effect": effect,
        "artifact": artifact,
        "targetScope": target_scope,
        "targetValue": target_value,
        "explicitConstraints": constraints,
        "ambiguities": ambiguities,
        "evidence": evidence,
        "canWrite": can_write,
        "complexity": complexity,
    }
    if secondary:
        frame["secondary"] = secondary
    return frame


def _catalog_prompt_lines(catalog: Dict[str, Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    for label, entry in catalog.items():
        targets = ", ".join(str(t) for t in entry.get("assetTargets") or []) or "(no fixed output path)"
        skills = ", ".join(str(s) for s in entry.get("skills") or [])
        examples = " / ".join(f'"{e}"' for e in (entry.get("examples") or [])[:3])
        line = f"- {label}: {entry.get('description') or label}. Outputs go under: {targets}."
        if skills:
            line += f" Project skills: {skills}."
        if examples:
            line += f" e.g. {examples}"
        lines.append(line)
    return lines


def _intent_messages(
    *,
    prompt: str,
    active_file: str,
    catalog: Dict[str, Dict[str, Any]],
    previous_turn: Dict[str, Any] | None,
) -> list[Dict[str, Any]]:
    # Ask for orthogonal semantics. The model describes the request; application
    # code compiles that description into write capabilities.
    system_prompt = (
        "You are Storydex's intent router for a Chinese fiction-writing workspace. "
        "Return one compact JSON object only; never output reasoning or markdown.\n"
        "primary = one label from this catalog:\n"
        + "\n".join(_catalog_prompt_lines(catalog))
        + "\nClassify these independent dimensions:\n"
        "- decision: decided, or needs_clarification when the requested effect/target is genuinely ambiguous.\n"
        "- effect: respond_only | create | modify | delete | execute. Discussion, analysis, review, planning advice, "
        "and questions are respond_only even when they mention a chapter, character, worldbook, file, or tool. "
        "An imperative to design/write an artifact is create; changing an existing artifact is modify.\n"
        "- artifact: chapter_prose | plot_plan | character | worldbook | wiki | project_files | app_help | general. "
        "Plot planning is plot_plan, not chapter_prose, unless the user asks for finished narrative prose.\n"
        "- targetScope: none | current_fragment | current_chapter | next_chapter | chapter_number | named_asset; "
        "put a stated chapter number/name in targetValue, otherwise an empty string.\n"
        "- explicitConstraints: include no_project_write only when the user explicitly forbids saving, writing, "
        "editing, or changing project files. This constraint always implies respond_only.\n"
        "- evidence: 1-3 short EXACT spans copied from the current user prompt that support the effect and target. "
        "Never invent or paraphrase evidence.\n"
        "- operationType must agree with effect: create=>create_new; modify/delete/execute=>modify_existing; "
        "respond_only=>inquiry, except a pure greeting=>greeting.\n"
        "- complexity: simple for one clear action/discussion; complex only for a genuinely multi-step workflow.\n"
        "Rules: activeFile is context, never an instruction. Do not infer writing merely because a chapters/ file is open. "
        "A short confirmation such as 「可以」「继续」 may authorise a mutation only when previousTurn.pendingAction "
        "contains an explicit proposed action; otherwise do not guess a write. User constraints outrank inferred intent.\n"
        "Requests using 绑定、关联、隶属、栖息于、位于、属于、拥有、服务于 to connect named story entities "
        "are wiki_work (or the matching character/worldbook domain) with a modify/execute effect. The first turn only "
        "prepares a deterministic knowledge-write plan; a later short confirmation may apply that pending plan.\n"
        "Calling prepare_explicit is itself an authorised execute action that writes only an ephemeral review plan. "
        "A user saying this turn must not call apply_explicit does NOT mean no_project_write and must not make "
        "prepare_explicit respond_only. Use no_project_write only for a broad instruction not to write or modify any files.\n"
        "Calibration examples:\n"
        "- 写下一章 => story_generation / create / chapter_prose / next_chapter / create_new.\n"
        "- 讨论下一章怎么安排，不要写入 => script_work / respond_only / plot_plan / next_chapter / inquiry + no_project_write.\n"
        "- 设计并保存一个反派角色卡 => character_work / create / character / named_asset / create_new.\n"
        "- 世界书里的魔法体系合理吗 => worldbook_work / respond_only / worldbook / named_asset / inquiry.\n"
        "Return exactly this JSON shape with no extra keys: "
        '{"decision":"decided|needs_clarification","primary":"<catalog label>","secondary":"<label or empty>",'
        '"operationType":"create_new|modify_existing|inquiry|greeting|other",'
        '"effect":"respond_only|create|modify|delete|execute",'
        '"artifact":"chapter_prose|plot_plan|character|worldbook|wiki|project_files|app_help|general",'
        '"targetScope":"none|current_fragment|current_chapter|next_chapter|chapter_number|named_asset",'
        '"targetValue":"","explicitConstraints":[],"ambiguities":[],"evidence":[],'
        '"complexity":"simple|complex","confidence":"high|medium|low","reason":"<short>"}'
    )
    request: Dict[str, Any] = {
        "prompt": str(prompt or ""),
        "activeFile": str(active_file or ""),
        "activeFileIsChapter": str(active_file or "").startswith("chapters/"),
        "previousTurn": previous_turn or None,
    }
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
    ]


def _enrich_frame(frame: Dict[str, Any], catalog: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    entry = catalog.get(str(frame.get("primary") or "")) or {}
    can_write = intent_frame_allows_project_writes(frame)
    frame["canWrite"] = can_write
    frame["assetTargets"] = list(entry.get("assetTargets") or []) if can_write else []
    frame["matchedSkills"] = list(entry.get("skills") or []) if can_write else []
    return frame


def _apply_knowledge_write_semantics(
    frame: Dict[str, Any],
    *,
    prompt: str,
    previous_turn: Dict[str, Any] | None,
) -> Dict[str, Any]:
    prompt_text = str(prompt or "")
    explicit_binding = is_explicit_knowledge_binding_request(prompt_text)
    previous_mode = str((previous_turn or {}).get("knowledgeWriteMode") or "").strip()
    confirmation = bool(
        previous_mode == "explicit_binding"
        and _EXPLICIT_KNOWLEDGE_CONFIRMATION_RE.match(prompt_text)
    )
    if not explicit_binding and not confirmation:
        return frame
    if explicit_binding and str(frame.get("primary") or "") not in {
        "wiki_work",
        "worldbook_work",
        "character_work",
    }:
        frame["primary"] = "wiki_work"
        frame["artifact"] = "wiki"
    frame["knowledgeWriteMode"] = "explicit_binding"
    frame["knowledgeConfirmationRequired"] = True
    frame["knowledgeConfirmed"] = confirmation
    prepare_requested = bool(_EXPLICIT_KNOWLEDGE_PREPARE_RE.search(prompt_text))
    apply_requested = bool(_EXPLICIT_KNOWLEDGE_APPLY_RE.search(prompt_text))
    broad_no_write = bool(_BROAD_NO_PROJECT_WRITE_RE.search(prompt_text))
    model_classified = str(frame.get("method") or "").strip() == "llm"
    explicitly_authorized = bool(
        model_classified
        and not broad_no_write
        and (
            (explicit_binding and prepare_requested and not confirmation)
            or (confirmation and (apply_requested or not prepare_requested))
        )
    )
    if explicitly_authorized:
        frame["decision"] = "decided"
        frame["effect"] = "execute"
        frame["operationType"] = "modify_existing"
        frame["canWrite"] = True
        constraints = frame.get("explicitConstraints") if isinstance(frame.get("explicitConstraints"), list) else []
        frame["explicitConstraints"] = [
            item for item in constraints if str(item or "").strip().lower() != _NO_PROJECT_WRITE
        ]
        ambiguities = frame.get("ambiguities") if isinstance(frame.get("ambiguities"), list) else []
        frame["ambiguities"] = [
            item for item in ambiguities if str(item or "").strip() != "missing_grounded_write_evidence"
        ]
    signals = frame.get("signals") if isinstance(frame.get("signals"), list) else []
    markers = ["explicit_knowledge_confirmation" if confirmation else "explicit_knowledge_binding"]
    if explicitly_authorized:
        markers.append("explicit_knowledge_apply" if confirmation else "explicit_knowledge_prepare")
    for marker in markers:
        if marker not in signals:
            signals.append(marker)
    frame["signals"] = signals
    return frame


def _apply_full_prompt_constraints(frame: Dict[str, Any], *, prompt: str) -> Dict[str, Any]:
    match = _BROAD_NO_PROJECT_WRITE_RE.search(str(prompt or ""))
    if match is None:
        return frame
    constraints = (
        list(frame.get("explicitConstraints"))
        if isinstance(frame.get("explicitConstraints"), list)
        else []
    )
    if _NO_PROJECT_WRITE not in constraints:
        constraints.append(_NO_PROJECT_WRITE)
    frame["explicitConstraints"] = constraints[:3]
    frame["effect"] = "respond_only"
    frame["operationType"] = "inquiry"
    frame["canWrite"] = False
    signals = list(frame.get("signals")) if isinstance(frame.get("signals"), list) else []
    if "full_prompt_no_project_write" not in signals:
        signals.append("full_prompt_no_project_write")
    frame["signals"] = signals
    evidence = list(frame.get("evidence")) if isinstance(frame.get("evidence"), list) else []
    exact_constraint = match.group(0).strip()
    if exact_constraint and exact_constraint not in evidence:
        evidence.append(exact_constraint[:80])
    frame["evidence"] = evidence[:3]
    return frame


class StorydexIntentService:
    def __init__(
        self,
        *,
        llm_timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS,
        story_project_service: Any = None,
    ) -> None:
        self.llm_timeout_seconds = llm_timeout_seconds
        self._story_project_service = story_project_service
        self._session_turns: OrderedDict[str, Dict[str, Any]] = OrderedDict()

    async def classify_intent(
        self,
        *,
        prompt: str,
        active_file: str = "",
        workspace_root: Path | None = None,
        session_id: str = "",
    ) -> Dict[str, Any]:
        normalized_prompt = str(prompt or "").strip()
        session_key = self._session_key(workspace_root=workspace_root, session_id=session_id)

        # 唯一保留的零成本短路：slash 命令与空输入。它们语义确定，不需要模型
        # 判断，也不应触发冷文件系统 / provider 初始化。
        if not normalized_prompt or normalized_prompt.startswith("/"):
            frame = heuristic_intent_frame(prompt=normalized_prompt, active_file=active_file)
            frame["method"] = "deterministic"
            catalog = build_intent_catalog()
            frame = _apply_knowledge_write_semantics(
                frame,
                prompt=normalized_prompt,
                previous_turn=None,
            )
            frame = _apply_full_prompt_constraints(frame, prompt=normalized_prompt)
            _enrich_frame(frame, catalog)
            self._remember(
                session_key=session_key,
                prompt=normalized_prompt,
                primary=str(frame.get("primary") or ""),
                frame=frame,
            )
            return frame

        # Registry and trace-history reads are filesystem work.  Keep them off
        # the request loop so the outer hard deadline also covers cold storage.
        catalog = await asyncio.to_thread(self._catalog, workspace_root)
        previous_turn = self._session_turns.get(session_key) if session_key else None
        if session_id and previous_turn is None:
            persisted_turn = await asyncio.to_thread(
                self._load_persisted_turn,
                session_id=session_id,
                workspace_root=workspace_root,
            )
            if persisted_turn:
                previous_turn = {**persisted_turn, **(previous_turn or {})}
        # A first-turn request that names a concrete file, explicitly asks to
        # read/inspect/summarize it, and forbids project writes is already a
        # complete permission decision.  Keep the locally inferred domain but
        # skip the provider round whose only safety-relevant result would be
        # the same hard no-write boundary.  Semantic discussions, follow-ups,
        # and custom project intents still use the model path.
        deterministic_read_only = heuristic_intent_frame(
            prompt=normalized_prompt,
            active_file=active_file,
        )
        if (
            previous_turn is None
            and _is_explicit_read_only_file_request(normalized_prompt)
            and set(catalog).issubset(set(INTENT_LABELS))
        ):
            frame = deterministic_read_only
            frame["method"] = "deterministic_no_project_write"
            frame = _apply_knowledge_write_semantics(
                frame,
                prompt=normalized_prompt,
                previous_turn=None,
            )
            frame = _apply_full_prompt_constraints(frame, prompt=normalized_prompt)
            _enrich_frame(frame, catalog)
            self._remember(
                session_key=session_key,
                prompt=normalized_prompt,
                primary=str(frame.get("primary") or ""),
                frame=frame,
            )
            return frame
        # The model is the semantic path for every natural-language turn,
        # including short confirmations. previousTurn is evidence, not a local
        # rule that can silently authorise a mutation.
        frame = await self._llm_intent_frame(
            prompt=normalized_prompt,
            active_file=active_file,
            catalog=catalog,
            previous_turn=previous_turn,
        )
        if frame is None:
            frame = safe_fallback_intent_frame(reason="invalid_or_unavailable_model_output")
        frame = _apply_knowledge_write_semantics(
            frame,
            prompt=normalized_prompt,
            previous_turn=previous_turn,
        )
        frame = _apply_full_prompt_constraints(frame, prompt=normalized_prompt)
        _enrich_frame(frame, catalog)
        self._remember(
            session_key=session_key,
            prompt=normalized_prompt,
            primary=str(frame.get("primary") or ""),
            previous_turn=previous_turn,
            frame=frame,
        )
        return frame

    def _catalog(self, workspace_root: Path | None) -> Dict[str, Dict[str, Any]]:
        try:
            return build_intent_catalog(
                workspace_root=workspace_root,
                story_project_service=self._story_project_service,
            )
        except Exception:
            return build_intent_catalog()

    def clear_session(self, *, session_id: str, workspace_root: Path | None = None) -> None:
        key = self._session_key(workspace_root=workspace_root, session_id=session_id)
        if key:
            self._session_turns.pop(key, None)
        if workspace_root is None:
            suffix = f"::{str(session_id or 'default').strip() or 'default'}"
            for candidate in [item for item in self._session_turns if item.endswith(suffix)]:
                self._session_turns.pop(candidate, None)

    @staticmethod
    def _session_key(*, workspace_root: Path | None, session_id: str) -> str:
        normalized_session = str(session_id or "").strip()
        if not normalized_session:
            return ""
        workspace = str(Path(workspace_root).resolve()) if workspace_root is not None else "default"
        return f"{workspace}::{normalized_session}"

    def _remember(
        self,
        *,
        session_key: str,
        prompt: str,
        primary: str,
        previous_turn: Dict[str, Any] | None = None,
        frame: Dict[str, Any] | None = None,
    ) -> None:
        if not session_key or not prompt or not primary:
            return
        remembered = {
            "prompt": prompt[:200],
            "intent": primary,
        }
        semantic_frame = frame if isinstance(frame, dict) else {}
        for key in (
            "decision",
            "effect",
            "artifact",
            "targetScope",
            "targetValue",
            "operationType",
            "canWrite",
            "knowledgeWriteMode",
            "knowledgeConfirmationRequired",
            "knowledgeConfirmed",
        ):
            if key in semantic_frame:
                remembered[key] = semantic_frame[key]
        for key in ("explicitConstraints", "ambiguities", "evidence"):
            if isinstance(semantic_frame.get(key), list):
                remembered[key] = list(semantic_frame[key])[:3]
        if previous_turn:
            assistant_reply = str(previous_turn.get("assistantReply") or "").strip()
            pending_action = str(previous_turn.get("pendingAction") or "").strip()
            if assistant_reply:
                remembered["assistantReply"] = assistant_reply[:1200]
            if pending_action:
                remembered["pendingAction"] = pending_action[:500]
        self._session_turns[session_key] = remembered
        self._session_turns.move_to_end(session_key)
        while len(self._session_turns) > _MAX_SESSION_MEMORY:
            self._session_turns.popitem(last=False)

    @staticmethod
    def _load_persisted_turn(*, session_id: str, workspace_root: Path | None) -> Dict[str, Any] | None:
        try:
            from services.trace_history_service import get_trace_history_service

            records = get_trace_history_service().list_records(session_id=session_id, limit=5)
        except Exception:
            return None
        expected_workspace = str(Path(workspace_root).resolve()) if workspace_root is not None else ""
        for record in records:
            if not isinstance(record, dict):
                continue
            record_workspace = str(record.get("workspaceRoot") or "").strip()
            if expected_workspace and record_workspace:
                try:
                    if str(Path(record_workspace).resolve()) != expected_workspace:
                        continue
                except Exception:
                    continue
            prompt = str(record.get("prompt") or "").strip()
            reply = str(record.get("reply") or "").strip()
            intent = ""
            knowledge_write_mode = ""
            knowledge_confirmed = False
            audit = record.get("audit") if isinstance(record.get("audit"), list) else []
            for item in reversed(audit):
                if isinstance(item, dict) and item.get("action") == "storydex_turn_contract":
                    intent = str(item.get("intent") or "").strip()
                    break
            events = record.get("events") if isinstance(record.get("events"), list) else []
            for event in reversed(events):
                if not isinstance(event, dict) or event.get("event") != "TurnContract":
                    continue
                data = event.get("data") if isinstance(event.get("data"), dict) else {}
                intent_frame = data.get("intentFrame") if isinstance(data.get("intentFrame"), dict) else {}
                intent = intent or str(intent_frame.get("primary") or "").strip()
                knowledge_policy = (
                    data.get("knowledgeWritePolicy")
                    if isinstance(data.get("knowledgeWritePolicy"), dict)
                    else {}
                )
                knowledge_write_mode = str(knowledge_policy.get("mode") or "").strip()
                knowledge_confirmed = bool(knowledge_policy.get("confirmed"))
                if intent and knowledge_write_mode:
                    break
            if not prompt and not reply:
                continue
            pending_action = reply[-800:] if reply and ("?" in reply or "？" in reply or "是否" in reply) else ""
            result = {
                "prompt": prompt[:200],
                "intent": intent or "general",
                "assistantReply": reply[-1200:],
                "pendingAction": pending_action,
            }
            if knowledge_write_mode:
                result["knowledgeWriteMode"] = knowledge_write_mode
                result["knowledgeConfirmed"] = knowledge_confirmed
            return result
        return None

    async def _llm_intent_frame(
        self,
        *,
        prompt: str,
        active_file: str,
        catalog: Dict[str, Dict[str, Any]],
        previous_turn: Dict[str, Any] | None,
    ) -> Dict[str, Any] | None:
        try:
            from services.coomi_agent_service import _call_provider_chat, _storydex_coomi_home

            with _storydex_coomi_home():
                from services.llm_replay import get_replayable_llm_provider, llm_purpose

                def create_provider() -> Any:
                    from services.coomi_bridge_client import get_bridge_provider

                    bounded_provider = _BoundedIntentProvider(get_bridge_provider(fast=True))
                    return get_replayable_llm_provider(bounded_provider)

                with llm_purpose("intent"):
                    provider = await asyncio.to_thread(create_provider)
                    response = await asyncio.wait_for(
                        _call_provider_chat(
                            provider,
                            _intent_messages(
                                prompt=prompt,
                                active_file=active_file,
                                catalog=catalog,
                                previous_turn=previous_turn,
                            ),
                            None,
                        ),
                        timeout=self.llm_timeout_seconds,
                    )
        except Exception:
            return None
        return _parse_intent_frame(
            str(getattr(response, "content", "") or ""),
            valid_labels=set(catalog),
            prompt=prompt,
            require_v2=True,
        )


_SERVICE = StorydexIntentService()


def get_storydex_intent_service() -> StorydexIntentService:
    return _SERVICE
