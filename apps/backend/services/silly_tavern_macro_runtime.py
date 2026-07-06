from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional


_COMMENT_RE = re.compile(r"\{\{//.*?\}\}", re.DOTALL)
# ST 语义：{{trim}} 只吞掉自身周围的换行（Trims newlines surrounding this macro）。
_TRIM_RE = re.compile(r"(?:\r?\n)*\{\{\s*trim\s*\}\}(?:\r?\n)*", re.IGNORECASE)
_SUPPORTED_MACRO_RE = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_]*)\b([\s\S]*?)\}\}")
_INSTRUCT_MACRO_ALIASES = {
    "instructstorystringprefix": ("storyStringPrefix", "story_string_prefix"),
    "instructstorystringsuffix": ("storyStringSuffix", "story_string_suffix"),
    "instructuserprefix": ("inputSequence", "input_sequence"),
    "instructinput": ("inputSequence", "input_sequence"),
    "instructusersuffix": ("inputSuffix", "input_suffix"),
    "instructassistantprefix": ("outputSequence", "output_sequence"),
    "instructoutput": ("outputSequence", "output_sequence"),
    "instructassistantsuffix": ("outputSuffix", "output_suffix"),
    "instructseparator": ("outputSuffix", "output_suffix"),
    "instructsystemprefix": ("systemSequence", "system_sequence"),
    "instructsystemsuffix": ("systemSuffix", "system_suffix"),
    "instructfirstassistantprefix": ("firstOutputSequence", "first_output_sequence", "outputSequence", "output_sequence"),
    "instructfirstoutputprefix": ("firstOutputSequence", "first_output_sequence", "outputSequence", "output_sequence"),
    "instructlastassistantprefix": ("lastOutputSequence", "last_output_sequence", "outputSequence", "output_sequence"),
    "instructlastoutputprefix": ("lastOutputSequence", "last_output_sequence", "outputSequence", "output_sequence"),
    "instructstop": ("stopSequence", "stop_sequence"),
    "instructuserfiller": ("userAlignmentMessage", "user_alignment_message"),
    "instructsysteminstructionprefix": ("lastSystemSequence", "last_system_sequence"),
    "instructfirstuserprefix": ("firstInputSequence", "first_input_sequence", "inputSequence", "input_sequence"),
    "instructfirstinput": ("firstInputSequence", "first_input_sequence", "inputSequence", "input_sequence"),
    "instructlastuserprefix": ("lastInputSequence", "last_input_sequence", "inputSequence", "input_sequence"),
    "instructlastinput": ("lastInputSequence", "last_input_sequence", "inputSequence", "input_sequence"),
}
_SYSTEM_PROMPT_MACROS = {"defaultsystemprompt", "instructsystem", "instructsystemprompt", "systemprompt"}


@dataclass
class SillyTavernMacroRuntime:
    runtime_context: Dict[str, Any] = field(default_factory=dict)
    local_variables: Dict[str, str] = field(default_factory=dict)
    global_variables: Dict[str, str] = field(default_factory=dict)
    rng: random.Random = field(default_factory=random.Random)

    def expand(self, text: str) -> str:
        value = str(text or "")
        if not value:
            return value

        value = _COMMENT_RE.sub("", value)
        value = _TRIM_RE.sub("", value)
        value = self._expand_document(value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()

    def _expand_document(self, text: str) -> str:
        output: list[str] = []
        index = 0
        while index < len(text):
            start = text.find("{{", index)
            if start < 0:
                output.append(text[index:])
                break
            output.append(text[index:start])
            macro_end = _find_balanced_macro_end(text, start)
            if macro_end < 0:
                output.append(text[start:])
                break

            macro_inner = text[start + 2 : macro_end]
            name, body = _split_macro_inner(macro_inner)
            if name.lower() == "if":
                expanded, next_index = self._expand_if_macro(text, start, macro_end, body)
                output.append(expanded)
                index = next_index
                continue

            output.append(
                self._expand_macro_parts(
                    name=name,
                    body=body,
                    original=text[start : macro_end + 2],
                    global_offset=start,
                )
            )
            index = macro_end + 2
        return "".join(output)

    def _expand_if_macro(self, text: str, start: int, macro_end: int, body: str) -> tuple[str, int]:
        inline_args = _split_inline_if_body(body)
        if inline_args is not None:
            condition, inline_content = inline_args
            return (self.expand(inline_content) if self._truthy_condition(condition) else "", macro_end + 2)

        close_info = _find_if_block(text, macro_end + 2)
        if close_info is None:
            return text[start : macro_end + 2], macro_end + 2
        else_start, else_end, close_start, close_end = close_info
        if else_start is None or else_end is None:
            then_branch = text[macro_end + 2 : close_start]
            else_branch = ""
        else:
            then_branch = text[macro_end + 2 : else_start]
            else_branch = text[else_end + 2 : close_start]
        selected = then_branch if self._truthy_condition(body) else else_branch
        return self.expand(selected), close_end + 2

    def _truthy_condition(self, raw_condition: str) -> bool:
        condition = str(raw_condition or "").strip()
        inverted = False
        if condition.startswith("!"):
            inverted = True
            condition = condition[1:].strip()

        if condition.startswith(".") and len(condition) > 1:
            value = self.local_variables.get(condition[1:].strip(), "")
        elif condition.startswith("$") and len(condition) > 1:
            value = self.global_variables.get(condition[1:].strip(), "")
        else:
            value = self.expand(condition) if "{{" in condition else condition

        result = bool(str(value).strip()) and not _is_false_boolean(str(value))
        return not result if inverted else result

    def _expand_macro(self, match: re.Match[str]) -> str:
        return self._expand_macro_parts(
            name=match.group(1),
            body=match.group(2),
            original=match.group(0),
            global_offset=match.start(),
        )

    def _expand_macro_parts(self, *, name: str, body: str, original: str, global_offset: int = 0) -> str:
        normalized_name = name.lower()
        args = _parse_macro_args(body)

        if normalized_name == "setvar":
            return self._set_var(self.local_variables, args)
        if normalized_name == "addvar":
            return self._add_var(self.local_variables, args)
        if normalized_name == "incvar":
            return self._inc_var(self.local_variables, args)
        if normalized_name == "decvar":
            return self._dec_var(self.local_variables, args)
        if normalized_name in {"hasvar", "varexists"}:
            return self._has_var(self.local_variables, args)
        if normalized_name in {"deletevar", "flushvar"}:
            return self._delete_var(self.local_variables, args)

        if normalized_name == "setglobalvar":
            return self._set_var(self.global_variables, args)
        if normalized_name == "addglobalvar":
            return self._add_var(self.global_variables, args)
        if normalized_name == "incglobalvar":
            return self._inc_var(self.global_variables, args)
        if normalized_name == "decglobalvar":
            return self._dec_var(self.global_variables, args)
        if normalized_name in {"hasglobalvar", "globalvarexists"}:
            return self._has_var(self.global_variables, args)
        if normalized_name in {"deleteglobalvar", "flushglobalvar"}:
            return self._delete_var(self.global_variables, args)
        if normalized_name == "getvar":
            return self.local_variables.get(_first_arg(args), "")
        if normalized_name == "getglobalvar":
            return self.global_variables.get(_first_arg(args), "")

        if normalized_name == "random":
            return self._random_choice(args)
        if normalized_name == "pick":
            return self._stable_pick(args, original=original, global_offset=global_offset)
        if normalized_name == "roll":
            return self._roll_dice(_first_arg(args))
        if normalized_name == "trim":
            return ""
        if normalized_name == "space":
            return " " * _positive_int(_first_arg(args), default=1)
        if normalized_name == "newline":
            return "\n" * _positive_int(_first_arg(args), default=1)
        if normalized_name == "noop":
            return ""
        if normalized_name == "reverse":
            return "".join(reversed(_first_arg(args)))
        if normalized_name == "banned":
            return ""
        if normalized_name in {
            "time",
            "date",
            "weekday",
            "isotime",
            "isodate",
            "datetimeformat",
            "idleduration",
            "idle_duration",
            "timediff",
        }:
            return self._time_macro(normalized_name, args)
        if normalized_name in {
            "lastmessage",
            "lastmessageid",
            "lastusermessage",
            "lastcharmessage",
            "firstincludedmessageid",
            "firstdisplayedmessageid",
            "lastswipeid",
            "currentswipeid",
            "allchatrange",
        }:
            return self._chat_macro(normalized_name)
        if normalized_name in {"greeting", "charfirstmessage"}:
            return self._greeting_macro(args)
        if normalized_name == "hasextension":
            return self._has_extension(args)
        if normalized_name == "outlet":
            return self._outlet_macro(args)
        if normalized_name in _INSTRUCT_MACRO_ALIASES:
            return self._instruct_macro(normalized_name)
        if normalized_name in _SYSTEM_PROMPT_MACROS:
            return self._system_prompt_macro(normalized_name)
        if normalized_name in {"exampleseparator", "chatseparator", "chatstart"}:
            return self._context_template_macro(normalized_name)

        if args:
            return original
        value = _context_value(self.runtime_context, name)
        return original if value is None else value

    def _greeting_macro(self, args: list[str]) -> str:
        index_text = _first_arg(args)
        try:
            index = int(index_text) if index_text else 0
        except ValueError:
            index = 0
        if index <= 0:
            return _context_value(self.runtime_context, "charFirstMessage") or ""
        alternates = self.runtime_context.get("alternateGreetings")
        if isinstance(alternates, list) and index - 1 < len(alternates):
            return _stringify_macro_value(alternates[index - 1])
        return ""

    def _has_extension(self, args: list[str]) -> str:
        name = _first_arg(args)
        extensions = self.runtime_context.get("extensions")
        if isinstance(extensions, dict):
            exists = name in extensions
        elif isinstance(extensions, (list, tuple, set)):
            exists = name in {str(item) for item in extensions}
        else:
            exists = False
        return "true" if exists else "false"

    def _time_macro(self, normalized_name: str, args: list[str]) -> str:
        if normalized_name in {"idleduration", "idle_duration"}:
            return str(
                self.runtime_context.get("idleDuration")
                or self.runtime_context.get("idle_duration")
                or "just now"
            )

        now = _runtime_now(self.runtime_context)
        if normalized_name == "time":
            offset = _parse_utc_offset(_first_arg(args))
            if offset is not None:
                now = now.astimezone(timezone(offset))
            return now.strftime("%H:%M")
        if normalized_name == "isotime":
            return now.strftime("%H:%M")
        if normalized_name == "isodate":
            return now.strftime("%Y-%m-%d")
        if normalized_name == "date":
            return f"{now.strftime('%B')} {now.day}, {now.year}"
        if normalized_name == "weekday":
            return now.strftime("%A")
        if normalized_name == "datetimeformat":
            return _format_moment_like(now, _first_arg(args))
        if normalized_name == "timediff":
            return _time_diff_macro(args)
        return ""

    def _chat_macro(self, normalized_name: str) -> str:
        direct_aliases = {
            "lastmessage": ("lastMessage",),
            "lastmessageid": ("lastMessageId",),
            "lastusermessage": ("lastUserMessage", "prompt"),
            "lastcharmessage": ("lastCharMessage",),
            "firstincludedmessageid": ("firstIncludedMessageId",),
            "firstdisplayedmessageid": ("firstDisplayedMessageId",),
            "lastswipeid": ("lastSwipeId",),
            "currentswipeid": ("currentSwipeId",),
            "allchatrange": ("allChatRange",),
        }
        for key in direct_aliases.get(normalized_name, ()):
            value = self.runtime_context.get(key)
            if value is not None:
                return str(value)

        chat = self.runtime_context.get("chat")
        if not isinstance(chat, list) or not chat:
            return ""
        last_index = _last_chat_index(chat)
        last_message = chat[last_index] if last_index is not None else None
        if normalized_name == "lastmessageid":
            return "" if last_index is None else str(last_index)
        if normalized_name == "lastmessage":
            return _chat_message_text(last_message)
        if normalized_name == "lastusermessage":
            return _chat_message_text(_last_chat_message(chat, user=True))
        if normalized_name == "lastcharmessage":
            return _chat_message_text(_last_chat_message(chat, user=False))
        if normalized_name == "allchatrange":
            return f"0-{len(chat) - 1}"
        if normalized_name in {"lastswipeid", "currentswipeid"} and isinstance(last_message, dict):
            swipes = last_message.get("swipes")
            if normalized_name == "lastswipeid" and isinstance(swipes, list):
                return str(len(swipes))
            swipe_id = last_message.get("swipe_id")
            if normalized_name == "currentswipeid" and isinstance(swipe_id, int):
                return str(swipe_id + 1)

        metadata = self.runtime_context.get("chatMetadata")
        if not isinstance(metadata, dict):
            metadata = {}
        if normalized_name == "firstincludedmessageid":
            value = metadata.get("lastInContextMessageId")
            return "" if value is None else str(value)
        if normalized_name == "firstdisplayedmessageid":
            value = metadata.get("firstDisplayedMessageId")
            return "" if value is None else str(value)
        return ""

    @staticmethod
    def _set_var(store: Dict[str, str], args: list[str]) -> str:
        if len(args) < 2:
            return ""
        store[args[0].strip()] = args[1]
        return ""

    @staticmethod
    def _add_var(store: Dict[str, str], args: list[str]) -> str:
        if len(args) < 2:
            return ""
        name = args[0].strip()
        value = args[1]
        existing = store.get(name, "")
        store[name] = _add_values(existing, value)
        return ""

    @staticmethod
    def _inc_var(store: Dict[str, str], args: list[str]) -> str:
        name = _first_arg(args)
        value = _coerce_number(store.get(name, "0")) + 1
        store[name] = _format_number(value)
        return store[name]

    @staticmethod
    def _dec_var(store: Dict[str, str], args: list[str]) -> str:
        name = _first_arg(args)
        value = _coerce_number(store.get(name, "0")) - 1
        store[name] = _format_number(value)
        return store[name]

    @staticmethod
    def _has_var(store: Dict[str, str], args: list[str]) -> str:
        return "true" if _first_arg(args) in store else "false"

    @staticmethod
    def _delete_var(store: Dict[str, str], args: list[str]) -> str:
        store.pop(_first_arg(args), None)
        return ""

    def _random_choice(self, args: list[str]) -> str:
        options = args if len(args) > 1 else _split_random_options(_first_arg(args))
        if not options:
            return ""
        return options[self.rng.randrange(len(options))]

    def _stable_pick(self, args: list[str], *, original: str, global_offset: int) -> str:
        options = args if len(args) > 1 else _split_random_options(_first_arg(args))
        if not options:
            return ""
        seed_parts = [
            _stringify_macro_value(
                self.runtime_context.get("chatId")
                or self.runtime_context.get("currentChatId")
                or self.runtime_context.get("mainChat")
                or ""
            ),
            _stringify_macro_value(self.runtime_context.get("randomSeed") or ""),
            _stringify_macro_value(self.runtime_context.get("pickRerollSeed") or ""),
            original,
            str(global_offset),
        ]
        digest = hashlib.sha256("|".join(seed_parts).encode("utf-8")).hexdigest()
        rng = random.Random(int(digest[:16], 16))
        return options[rng.randrange(len(options))]

    def _outlet_macro(self, args: list[str]) -> str:
        key = _first_arg(args)
        if not key:
            return ""
        outlets = self.runtime_context.get("outlets")
        if isinstance(outlets, dict):
            value = outlets.get(key)
            if value is not None:
                return _stringify_macro_value(value)

        extension_prompts = self.runtime_context.get("extensionPrompts") or self.runtime_context.get("extension_prompts")
        if isinstance(extension_prompts, dict):
            for candidate_key in (key, f"CUSTOM_WI_OUTLET:{key}", f"custom_wi_outlet:{key}"):
                value = extension_prompts.get(candidate_key)
                if isinstance(value, dict):
                    return _stringify_macro_value(value.get("value") or "")
                if value is not None:
                    return _stringify_macro_value(value)
        return ""

    def _instruct_macro(self, normalized_name: str) -> str:
        instruct = self.runtime_context.get("instruct")
        if not isinstance(instruct, dict):
            instruct = {}
        if _explicitly_disabled(instruct.get("enabled") if "enabled" in instruct else self.runtime_context.get("instructEnabled")):
            return ""
        keys = _INSTRUCT_MACRO_ALIASES[normalized_name]
        return _lookup_many(instruct, keys) or _lookup_many(self.runtime_context, tuple(f"instruct{key[:1].upper()}{key[1:]}" for key in keys)) or ""

    def _system_prompt_macro(self, normalized_name: str) -> str:
        system_prompt = _lookup_many(self.runtime_context, ("systemPrompt", "defaultSystemPrompt", "sysprompt"))
        if normalized_name == "systemprompt" and _truthy_value(self.runtime_context.get("preferCharacterPrompt")):
            char_prompt = _context_value(self.runtime_context, "charPrompt")
            if char_prompt:
                return char_prompt
        return system_prompt or ""

    def _context_template_macro(self, normalized_name: str) -> str:
        context_template = self.runtime_context.get("contextTemplate") or self.runtime_context.get("context")
        if not isinstance(context_template, dict):
            context_template = {}
        if normalized_name in {"exampleseparator", "chatseparator"}:
            return _lookup_many(context_template, ("exampleSeparator", "example_separator", "chatSeparator", "chat_separator")) or ""
        if normalized_name == "chatstart":
            return _lookup_many(context_template, ("chatStart", "chat_start")) or ""
        return ""

    def _roll_dice(self, formula: str) -> str:
        formula = formula.strip()
        if formula.isdigit():
            formula = f"1d{formula}"
        dice_match = re.fullmatch(r"(\d*)d(\d+)([+-]\d+)?", formula, flags=re.IGNORECASE)
        if not dice_match:
            return ""
        count = int(dice_match.group(1) or "1")
        sides = int(dice_match.group(2))
        modifier = int(dice_match.group(3) or "0")
        if count <= 0 or sides <= 0 or count > 1000:
            return ""
        total = sum(self.rng.randint(1, sides) for _ in range(count)) + modifier
        return str(total)


def create_silly_tavern_macro_runtime(runtime_context: Optional[Dict[str, Any]] = None) -> SillyTavernMacroRuntime:
    context = dict(runtime_context or {})
    seed = context.get("randomSeed")
    rng = random.Random(seed) if seed is not None else random.Random()
    local_variables = _coerce_string_map(context.get("variables") or context.get("localVariables"))
    global_variables = _coerce_string_map(context.get("globalVariables"))
    return SillyTavernMacroRuntime(
        runtime_context=context,
        local_variables=local_variables,
        global_variables=global_variables,
        rng=rng,
    )


def _lookup_many(context: Dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = context.get(key)
        if value is not None:
            return _stringify_macro_value(value)
    return ""


def _explicitly_disabled(value: Any) -> bool:
    return value is False or _is_false_boolean(str(value)) if value is not None else False


def _truthy_value(value: Any) -> bool:
    if value is None:
        return False
    return bool(str(value).strip()) and not _is_false_boolean(str(value))


def _context_value(context: Dict[str, Any], name: str) -> Optional[str]:
    aliases = {
        "user": ("user", "personaName"),
        "char": ("char", "character", "characterName"),
        "group": ("group", "charIfNotGroup"),
        "charIfNotGroup": ("group", "charIfNotGroup"),
        "groupNotMuted": ("groupNotMuted",),
        "notChar": ("notChar",),
        "charPrompt": ("charPrompt",),
        "charInstruction": ("charInstruction",),
        "personality": ("personality", "charPersonality"),
        "scenario": ("scenario", "charScenario"),
        "description": ("description", "charDescription"),
        "charDescription": ("charDescription", "description"),
        "charPersonality": ("charPersonality", "personality"),
        "charScenario": ("charScenario", "scenario"),
        "mesExamplesRaw": ("mesExamplesRaw",),
        "mesExamples": ("mesExamples",),
        "charDepthPrompt": ("charDepthPrompt",),
        "creatorNotes": ("creatorNotes", "charCreatorNotes"),
        "charCreatorNotes": ("charCreatorNotes", "creatorNotes"),
        "charFirstMessage": ("charFirstMessage", "greeting"),
        "charVersion": ("charVersion", "version", "char_version"),
        "version": ("charVersion", "version", "char_version"),
        "char_version": ("charVersion", "version", "char_version"),
        "model": ("model",),
        "original": ("original",),
        "isMobile": ("isMobile",),
        "maxPrompt": ("maxPrompt", "maxPromptTokens"),
        "maxPromptTokens": ("maxPrompt", "maxPromptTokens"),
        "maxContext": ("maxContext", "maxContextTokens"),
        "maxContextTokens": ("maxContext", "maxContextTokens"),
        "maxResponse": ("maxResponse", "maxResponseTokens"),
        "maxResponseTokens": ("maxResponse", "maxResponseTokens"),
        "lastGenerationType": ("lastGenerationType",),
        "lastusermessage": ("lastUserMessage", "prompt"),
        "lastUserMessage": ("lastUserMessage", "prompt"),
        "lastcharmessage": ("lastCharMessage",),
        "lastCharMessage": ("lastCharMessage",),
        "persona": ("persona", "personaDescription"),
    }
    keys = aliases.get(name, (name,))
    for key in keys:
        value = context.get(key)
        if value is not None:
            return _stringify_macro_value(value)
    return None


def _stringify_macro_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _find_balanced_macro_end(text: str, start: int) -> int:
    depth = 1
    index = start + 2
    while index < len(text):
        next_open = text.find("{{", index)
        next_close = text.find("}}", index)
        if next_close < 0:
            return -1
        if next_open >= 0 and next_open < next_close:
            depth += 1
            index = next_open + 2
            continue
        depth -= 1
        if depth == 0:
            return next_close
        index = next_close + 2
    return -1


def _split_macro_inner(inner: str) -> tuple[str, str]:
    value = str(inner or "").strip()
    if not value:
        return "", ""
    for index, char in enumerate(value):
        if char.isspace() or char == ":":
            return value[:index].strip(), value[index:]
    return value, ""


def _find_if_block(text: str, index: int) -> Optional[tuple[Optional[int], Optional[int], int, int]]:
    depth = 1
    else_start: Optional[int] = None
    else_end: Optional[int] = None
    while index < len(text):
        start = text.find("{{", index)
        if start < 0:
            return None
        macro_end = _find_balanced_macro_end(text, start)
        if macro_end < 0:
            return None
        name, _body = _split_macro_inner(text[start + 2 : macro_end])
        normalized_name = name.lower()
        if normalized_name == "if":
            depth += 1
        elif normalized_name == "/if":
            depth -= 1
            if depth == 0:
                return else_start, else_end, start, macro_end
        elif normalized_name == "else" and depth == 1 and else_start is None:
            else_start = start
            else_end = macro_end
        index = macro_end + 2
    return None


def _split_inline_if_body(body: str) -> Optional[tuple[str, str]]:
    value = str(body or "").strip()
    depth = 0
    index = 0
    while index < len(value):
        if value.startswith("{{", index):
            depth += 1
            index += 2
            continue
        if value.startswith("}}", index) and depth > 0:
            depth -= 1
            index += 2
            continue
        if value.startswith("::", index) and depth == 0:
            return value[:index].strip(), value[index + 2 :]
        index += 1
    return None


def _parse_macro_args(body: str) -> list[str]:
    raw = str(body or "")
    if not raw.strip():
        return []
    if raw.startswith("::"):
        return raw[2:].split("::")
    if raw.startswith(":"):
        return raw[1:].split("::")
    stripped = raw.strip()
    if not stripped:
        return []
    if "::" in stripped:
        return stripped.split("::")
    return [stripped]


def _first_arg(args: list[str]) -> str:
    return str(args[0]).strip() if args else ""


def _positive_int(value: str, *, default: int) -> int:
    try:
        parsed = int(str(value or "").strip())
    except ValueError:
        return default
    return max(0, parsed)


def _is_false_boolean(value: str) -> bool:
    return str(value or "").strip().lower() in {"false", "off", "0", "no", "disabled"}


def _runtime_now(context: Dict[str, Any]) -> datetime:
    value = context.get("now") or context.get("currentTime")
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.now().astimezone()
    else:
        parsed = datetime.now().astimezone()
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)


def _time_diff_macro(args: list[str]) -> str:
    if len(args) < 2:
        return ""
    left = _parse_datetime_value(args[0])
    right = _parse_datetime_value(args[1])
    if left is None or right is None:
        return ""
    seconds = (left - right).total_seconds()
    return _humanize_time_delta(seconds)


def _parse_datetime_value(value: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in (text, text.replace(" ", "T", 1)):
        try:
            return datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            continue
    return None


def _humanize_time_delta(seconds: float) -> str:
    is_past = seconds < 0
    value = abs(seconds)
    units = [
        (365 * 24 * 60 * 60, "year"),
        (30 * 24 * 60 * 60, "month"),
        (24 * 60 * 60, "day"),
        (60 * 60, "hour"),
        (60, "minute"),
    ]
    for unit_seconds, unit_name in units:
        if value >= unit_seconds:
            amount = max(1, round(value / unit_seconds))
            label = unit_name if amount == 1 else f"{unit_name}s"
            phrase = f"{amount} {label}"
            return f"{phrase} ago" if is_past else f"in {phrase}"
    return "a few seconds ago" if is_past else "in a few seconds"


def _parse_utc_offset(value: str) -> Optional[timedelta]:
    match = re.fullmatch(r"UTC([+-]\d{1,2})", str(value or "").strip(), flags=re.IGNORECASE)
    if not match:
        return None
    hours = int(match.group(1))
    if hours < -23 or hours > 23:
        return None
    return timedelta(hours=hours)


def _format_moment_like(value: datetime, pattern: str) -> str:
    fmt = str(pattern or "")
    replacements = {
        "YYYY": f"{value.year:04d}",
        "MM": f"{value.month:02d}",
        "DD": f"{value.day:02d}",
        "HH": f"{value.hour:02d}",
        "mm": f"{value.minute:02d}",
        "ss": f"{value.second:02d}",
    }
    for token, replacement in replacements.items():
        fmt = fmt.replace(token, replacement)
    return fmt


def _last_chat_index(chat: list[Any]) -> Optional[int]:
    for index in range(len(chat) - 1, -1, -1):
        item = chat[index]
        if not isinstance(item, dict):
            continue
        if item.get("is_system") is True:
            continue
        swipes = item.get("swipes")
        swipe_id = item.get("swipe_id")
        if isinstance(swipes, list) and isinstance(swipe_id, int) and swipe_id >= len(swipes):
            continue
        return index
    return None


def _last_chat_message(chat: list[Any], *, user: bool) -> Optional[Dict[str, Any]]:
    for index in range(len(chat) - 1, -1, -1):
        item = chat[index]
        if not isinstance(item, dict) or item.get("is_system") is True:
            continue
        if bool(item.get("is_user")) is user:
            return item
    return None


def _chat_message_text(item: Optional[Dict[str, Any]]) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("mes") or item.get("content") or item.get("text") or "")


def _split_random_options(value: str) -> list[str]:
    if "::" in value:
        return [item.strip() for item in value.split("::") if item.strip()]
    placeholder = "\u0000COMMA\u0000"
    return [
        item.strip().replace(placeholder, ",")
        for item in value.replace(r"\,", placeholder).split(",")
        if item.strip()
    ]


def _add_values(left: str, right: str) -> str:
    if _is_number(left) and _is_number(right):
        return _format_number(_coerce_number(left) + _coerce_number(right))
    return f"{left}{right}"


def _is_number(value: str) -> bool:
    try:
        float(str(value).strip())
    except ValueError:
        return False
    return True


def _coerce_number(value: str) -> float:
    try:
        return float(str(value).strip())
    except ValueError:
        return 0.0


def _format_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)


def _coerce_string_map(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}
