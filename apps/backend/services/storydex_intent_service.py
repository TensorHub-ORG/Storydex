"""Storydex 意图识别服务（项目语义接地版）。

三层混合路由（layered intent routing）：
1. 确定性信号（slash 命令、空输入）直接短路，零成本零误判；
2. LLM 结构化分类：封闭标签集 + 严格 JSON 输出 + 超时控制，
   复用 Coomi 已配置的 LLM provider，无需额外密钥；
3. 关键词启发式兜底：LLM 不可用/超时/输出不合法时退回正则逻辑，
   保证离线与故障场景下功能不中断。

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
_INTENT_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
DEFAULT_LLM_TIMEOUT_SECONDS = 2.0
_MAX_PROMPT_CHARS = 2000
_MAX_SESSION_MEMORY = 256
_INTENT_MAX_OUTPUT_TOKENS = 160
_FOLLOW_UP_RE = re.compile(r"^(执行|继续|确认|好的?|可以|是的|就这么做|开始|继续执行)[。.!！\s]*$")
_VARIABLE_ACTION_RE = re.compile(r"(变量|状态).*(整理|更新|同步|归档)|(整理|更新|同步|归档).*(变量|状态)")
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

# 启发式兜底正则（LLM 不可用时使用；仅覆盖内置标签）。
_STORY_INTENT_RE = re.compile(
    r"(续写|写(一|1)?段|写第|生成.*(剧情|故事|章节|片段)|创作.*(剧情|故事)|正文|剧情|章节|片段|story|chapter|scene|continue)",
    re.IGNORECASE,
)
_CHARACTER_INTENT_RE = re.compile(r"(角色|人物|character|cast)", re.IGNORECASE)
_WORLDBOOK_INTENT_RE = re.compile(r"(世界书|世界观|设定集|worldbook|lorebook|lore)", re.IGNORECASE)
_SCRIPT_INTENT_RE = re.compile(r"(剧本|分镜|台词|大纲|screenplay|script)", re.IGNORECASE)
_WIKI_INTENT_RE = re.compile(r"(wiki|知识图谱|知识库|整理设定|整理关系)", re.IGNORECASE)
_PROJECT_ORGANIZE_RE = re.compile(
    r"(整理目录|项目目录|整理项目|目录结构|组织方式|资料整理|盘点.*(?:章节|目录)|organize)",
    re.IGNORECASE,
)


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
    """关键词启发式分类，作为 LLM 不可用时的兜底路径。"""
    text = str(prompt or "")
    signals: List[str] = []
    primary = "general"
    if _PROJECT_ORGANIZE_RE.search(text):
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
    return {
        "primary": primary,
        "confidence": "medium" if signals else "low",
        "signals": signals,
        "method": "heuristic",
    }


def is_advisory_request(prompt: str) -> bool:
    """Return True for requests that ask for judgment or guidance, not mutation.

    In particular, the bare Chinese character ``写`` is intentionally not a
    mutation signal: phrases such as ``这段写得怎么样`` are advisory.
    """

    normalized = " ".join(str(prompt or "").strip().split())
    if not normalized or normalized.startswith("/"):
        return False
    return bool(_ADVISORY_RE.search(normalized)) and not bool(_MUTATION_REQUEST_RE.search(normalized))


class _BoundedIntentProvider:
    """Apply metadata-call limits without modifying the pinned Coomi wheel."""

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
        return await _invoke_provider_chat(self._provider, messages)


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
                # The outer stage deadline still applies. Providers without
                # strict Responses JSON support fall back to Coomi's normal
                # chat call and the strict system prompt.
                return None

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
        if _is_reasoning_model(model):
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
            # The outer stage deadline still applies.  Providers without JSON
            # schema/response-format support fall back to the strict prompt.
            return None

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
            return None
    return None


def _intent_response_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "primary": {"type": "string"},
            "secondary": {"type": "string"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "reason": {"type": "string"},
        },
        "required": ["primary", "secondary", "confidence", "reason"],
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


async def _invoke_provider_chat(provider: Any, messages: list[Dict[str, Any]]) -> Any:
    chat = getattr(provider, "chat")
    if inspect.iscoroutinefunction(chat):
        return await chat(messages, None)
    response = await asyncio.to_thread(chat, messages, None)
    return await response if inspect.isawaitable(response) else response


def _metadata_llm_response(*, content: str, reasoning_content: str = "") -> Any:
    try:
        from coomi.types import LLMResponse

        return LLMResponse(
            content=content,
            tool_calls=None,
            usage=None,
            reasoning_content=reasoning_content or None,
        )
    except (ImportError, TypeError):
        from types import SimpleNamespace

        return SimpleNamespace(content=content, tool_calls=None, usage=None, reasoning_content=reasoning_content or None)


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


def _parse_intent_frame(content: str, *, valid_labels: set[str]) -> Dict[str, Any] | None:
    payload = _extract_json_object(content)
    if not isinstance(payload, dict):
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
    frame = {
        "primary": primary,
        "confidence": confidence,
        "signals": ["llm_classifier"],
        "method": "llm",
        "reason": reason[:200],
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
    previous_turn: Dict[str, str] | None,
) -> list[Dict[str, Any]]:
    system_prompt = (
        "You are Storydex's intent router for a fiction-writing workspace. "
        "Classify the user's request into exactly one primary intent label from this project's catalog:\n"
        + "\n".join(_catalog_prompt_lines(catalog))
        + "\n\nRules:\n"
        "- The user usually writes Chinese; requests may be indirect or elliptical.\n"
        "- Short continuations like 「继续」「然后呢」「再来一段」 normally keep previousTurn's intent "
        "unless the topic clearly changed.\n"
        "- Use activeFile as context (an open chapters/ file suggests story continuation), not as an override.\n"
        "- If the request mixes two intents, pick the one the user wants executed now as primary "
        "and put the other in secondary.\n"
        "Return ONLY a JSON object: "
        '{"primary": "<label>", "secondary": "<label or empty string>", '
        '"confidence": "high"|"medium"|"low", "reason": "<short sentence>"}. '
        "No markdown, no extra keys, no chain-of-thought."
    )
    request: Dict[str, Any] = {
        "prompt": str(prompt or "")[:_MAX_PROMPT_CHARS],
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
    frame["assetTargets"] = list(entry.get("assetTargets") or [])
    frame["matchedSkills"] = list(entry.get("skills") or [])
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
        self._session_turns: OrderedDict[str, Dict[str, str]] = OrderedDict()

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

        # The deterministic and advisory paths must stay independent from cold
        # filesystem, history and provider initialization.  In particular this
        # keeps review-style prompts such as ``这段写得怎么样`` in the sub-100ms
        # path even when the project registry lives on slow storage.
        if not normalized_prompt or normalized_prompt.startswith("/"):
            frame = heuristic_intent_frame(prompt=normalized_prompt, active_file=active_file)
            frame["method"] = "deterministic"
            catalog = build_intent_catalog()
            _enrich_frame(frame, catalog)
            self._remember(
                session_key=session_key,
                prompt=normalized_prompt,
                primary=str(frame.get("primary") or ""),
            )
            return frame
        if is_advisory_request(normalized_prompt):
            frame = {
                "primary": "general",
                "confidence": "high",
                "signals": ["local_advisory_rule"],
                "method": "advisory_fast",
                "reason": "Local rule identified a consultation, suggestion, or evaluation request.",
            }
            catalog = build_intent_catalog()
            _enrich_frame(frame, catalog)
            self._remember(
                session_key=session_key,
                prompt=normalized_prompt,
                primary="general",
            )
            return frame

        # Registry and trace-history reads are filesystem work.  Keep them off
        # the request loop so the outer hard deadline also covers cold storage.
        catalog = await asyncio.to_thread(self._catalog, workspace_root)
        previous_turn = self._session_turns.get(session_key) if session_key else None
        is_follow_up = bool(_FOLLOW_UP_RE.match(normalized_prompt))
        if session_id and (is_follow_up or previous_turn is None):
            persisted_turn = await asyncio.to_thread(
                self._load_persisted_turn,
                session_id=session_id,
                workspace_root=workspace_root,
            )
            if persisted_turn:
                previous_turn = {**persisted_turn, **(previous_turn or {})}
        if is_follow_up and previous_turn:
            frame = self._follow_up_frame(previous_turn)
        else:
            heuristic = heuristic_intent_frame(prompt=normalized_prompt, active_file=active_file)
            explicit_signals = [
                signal for signal in heuristic.get("signals", []) if signal != "active_chapter_file"
            ]
            if explicit_signals:
                frame = heuristic
                frame["method"] = "heuristic_fast"
            else:
                frame = await self._llm_intent_frame(
                    prompt=normalized_prompt,
                    active_file=active_file,
                    catalog=catalog,
                    previous_turn=previous_turn,
                )
                if frame is None:
                    frame = heuristic
                    frame["method"] = "heuristic_fallback"
        _enrich_frame(frame, catalog)
        self._remember(
            session_key=session_key,
            prompt=normalized_prompt,
            primary=str(frame.get("primary") or ""),
            previous_turn=previous_turn,
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
        previous_turn: Dict[str, str] | None = None,
    ) -> None:
        if not session_key or not prompt or not primary:
            return
        remembered = {
            "prompt": prompt[:200],
            "intent": primary,
        }
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
    def _follow_up_frame(previous_turn: Dict[str, str]) -> Dict[str, Any]:
        assistant_reply = str(previous_turn.get("assistantReply") or "")
        pending_action = str(previous_turn.get("pendingAction") or "")
        primary = str(previous_turn.get("intent") or "general")
        if _VARIABLE_ACTION_RE.search(f"{pending_action}\n{assistant_reply}"):
            primary = "general"
        return {
            "primary": primary,
            "confidence": "high",
            "signals": ["persistent_previous_turn", "elliptical_follow_up"],
            "method": "deterministic_context",
            "reason": "Resolved from the previous assistant proposal in this session.",
        }

    @staticmethod
    def _load_persisted_turn(*, session_id: str, workspace_root: Path | None) -> Dict[str, str] | None:
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
            audit = record.get("audit") if isinstance(record.get("audit"), list) else []
            for item in reversed(audit):
                if isinstance(item, dict) and item.get("action") == "storydex_turn_contract":
                    intent = str(item.get("intent") or "").strip()
                    break
            if not intent:
                events = record.get("events") if isinstance(record.get("events"), list) else []
                for event in reversed(events):
                    if not isinstance(event, dict) or event.get("event") != "TurnContract":
                        continue
                    data = event.get("data") if isinstance(event.get("data"), dict) else {}
                    intent_frame = data.get("intentFrame") if isinstance(data.get("intentFrame"), dict) else {}
                    intent = str(intent_frame.get("primary") or "").strip()
                    if intent:
                        break
            if not prompt and not reply:
                continue
            pending_action = reply[-800:] if reply and ("?" in reply or "？" in reply or "是否" in reply) else ""
            return {
                "prompt": prompt[:200],
                "intent": intent or "general",
                "assistantReply": reply[-1200:],
                "pendingAction": pending_action,
            }
        return None

    async def _llm_intent_frame(
        self,
        *,
        prompt: str,
        active_file: str,
        catalog: Dict[str, Dict[str, Any]],
        previous_turn: Dict[str, str] | None,
    ) -> Dict[str, Any] | None:
        try:
            from services.coomi_agent_service import _call_provider_chat, _storydex_coomi_home

            with _storydex_coomi_home():
                from services.llm_replay import get_replayable_llm_provider, llm_purpose

                def create_provider() -> Any:
                    from coomi.services import get_llm_provider

                    main_provider = get_llm_provider()
                    try:
                        from coomi.services.llm.factory import create_fast_provider

                        fast_provider = create_fast_provider(main_provider)
                    except Exception:
                        fast_provider = None
                    bounded_provider = _BoundedIntentProvider(fast_provider or main_provider)
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
        )


_SERVICE = StorydexIntentService()


def get_storydex_intent_service() -> StorydexIntentService:
    return _SERVICE
