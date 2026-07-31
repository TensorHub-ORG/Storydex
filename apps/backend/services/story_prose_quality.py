"""Prose quality gates shared by every story generation path.

These checks started inside ``story_semantic_budget_controller``, but they are
not specific to that strategy: "did the model deliver prose, or did it deliver a
placeholder, a word-count apology, a duplicated paragraph or a truncated
sentence" is the same question for a semantic-budget scene and for a precision
length patch (plan §7.4).

They live here so the default prose pipeline can reuse them without importing
the whole semantic-budget controller, which drags in scene planning, hazard
roles and plan coverage that a length patch has no use for. The controller
re-exports these names, so both callers run the same implementation and a fix
to one is a fix to both — the plan is explicit that a second quality
implementation must not be copied into the length path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict

# 正文之外的包装标签。模型常把摘要/思考包在这些标签里一起交付。
_CONTENT_WRAPPER_RE = re.compile(
    r"</?(?:content|summary|details|background|refine)(?:\s[^>]*)?>",
    re.IGNORECASE,
)
_PROVIDER_CONTENT_BLOCK_RE = re.compile(
    r"\A\s*<content(?:\s[^>]*)?>(?P<content>.*?)</content>\s*(?P<tail>.*?)\s*\Z",
    re.IGNORECASE | re.DOTALL,
)
_EXPLICIT_CONTENT_ONLY_RE = re.compile(
    r"\A\s*<content(?:\s[^>]*)?>(?P<content>.*?)</content\s*>\s*\Z",
    re.IGNORECASE | re.DOTALL,
)
_PROVIDER_SIDECAR_BLOCK_RE = re.compile(
    r"\s*<(summary|details)(?:\s[^>]*)?>.*?</\1>\s*",
    re.IGNORECASE | re.DOTALL,
)
_PROVIDER_UNICODE_THINKING_BLOCK_RE = re.compile(
    r"<[|｜]\s*begin(?:▁|_|\s)+of(?:▁|_|\s)+thinking\s*[|｜]>"
    r".*?"
    r"</[|｜]\s*(?:end|begin)(?:▁|_|\s)+of(?:▁|_|\s)+thinking\s*[|｜]>",
    re.IGNORECASE | re.DOTALL,
)
_PROVIDER_UNICODE_THINKING_TAG_RE = re.compile(
    r"<\s*/?\s*[|｜]\s*(?:begin|end)(?:▁|_|\s)+of(?:▁|_|\s)+thinking\s*[|｜]>",
    re.IGNORECASE,
)
_XML_LIKE_TAG_RE = re.compile(
    r"<\s*/?\s*(?P<name>[A-Za-z][A-Za-z0-9:_-]*)(?:\s[^<>]*?)?/?>",
    re.IGNORECASE,
)
_KNOWN_PROSE_WRAPPER_TAGS = {
    "content",
    "summary",
    "details",
    "thinking",
    "think",
    "analysis",
    "plan",
    "reasoning",
}
_PROVIDER_META_BLOCK_RES = tuple(
    re.compile(rf"<{tag}\b[^>]*>.*?</{tag}\s*>", re.IGNORECASE | re.DOTALL)
    for tag in ("details", "thinking", "think", "analysis", "plan", "reasoning", "summary")
)
# 模型在正文里谈论字数、目标和补写，是把程序的计数指令当成了写作内容。
_LENGTH_META_RE = re.compile(
    r"Storydex|TurnContract|程序计数|非空白|字数|目标.{0,8}字|补写|可接受.{0,8}字",
    re.IGNORECASE,
)
_PLACEHOLDER_RE = re.compile(r"全文略|待续|TODO|TBD|此处省略", re.IGNORECASE)
_SCENE_HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:场景|scene)\s*[一二三四五六七八九十\d]+", re.IGNORECASE
)
_DIALOGUE_SPAN_RE = re.compile(r"“[^”]*”|‘[^’]*’|\"[^\"]*\"|'[^']*'", re.DOTALL)
_EXPLICIT_CONTENT_RE = re.compile(
    r"阴茎|阴道|龟头|射精|精液|小穴|肉棒|鸡巴|奶子|口交|性交|性爱|性器官|"
    r"插入.{0,8}(?:阴道|小穴)|(?:阴道|小穴).{0,8}插入",
    re.IGNORECASE,
)
_DUPLICATE_PARAGRAPH_MIN_CHARS = 12
# 24 字符重复是"模型开始复述自己"的可靠信号，短于此的重复在中文里多为正常搭配。
REPETITION_NGRAM_SIZE = 24
_REPEATED_NGRAM_LIMIT = 3
_DEFAULT_NGRAM_SIZE = 2


@dataclass(frozen=True)
class StoryProseExtraction:
    """Auditable result shared by prose counting, quality gates and writes."""

    status: str
    prose: str
    reason_codes: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "accepted": self.accepted,
            "prose": self.prose,
            "reasonCodes": list(self.reason_codes),
        }


def normalized_character_ngrams(text: str, *, size: int = _DEFAULT_NGRAM_SIZE) -> set[str]:
    """Return character n-grams with punctuation and case removed."""

    normalized = re.sub(r"[\W_]+", "", str(text or "").lower(), flags=re.UNICODE)
    return {
        normalized[index : index + size]
        for index in range(max(0, len(normalized) - size + 1))
    }


def _normalize_generated_response(text: str) -> str:
    """Strip code fences and a leading "正文：" label from a model response."""

    value = str(text or "").strip()
    fenced = re.fullmatch(r"```(?:markdown|text)?\s*(.*?)\s*```", value, re.DOTALL | re.IGNORECASE)
    if fenced:
        value = fenced.group(1).strip()
    value = _PROVIDER_UNICODE_THINKING_BLOCK_RE.sub("", value).strip()
    provider_envelope = _PROVIDER_CONTENT_BLOCK_RE.fullmatch(value)
    if provider_envelope:
        tail = provider_envelope.group("tail")
        tail_position = 0
        saw_sidecar = False
        while tail_position < len(tail):
            sidecar = _PROVIDER_SIDECAR_BLOCK_RE.match(tail, tail_position)
            if not sidecar:
                break
            saw_sidecar = True
            tail_position = sidecar.end()
        if saw_sidecar and not tail[tail_position:].strip():
            value = provider_envelope.group("content").strip()
    value = re.sub(r"^(?:正文|续写正文|修改后正文)\s*[：:]\s*", "", value, count=1)
    return value.strip()


def extract_story_prose(text: str) -> StoryProseExtraction:
    """Return publishable prose or an explicit conservative rejection."""

    cleaned = _normalize_generated_response(text)
    if _PROVIDER_UNICODE_THINKING_TAG_RE.search(cleaned):
        return StoryProseExtraction(
            status="rejected",
            prose="",
            reason_codes=("unclosed_thinking_wrapper",),
        )
    explicit_content = _EXPLICIT_CONTENT_ONLY_RE.fullmatch(cleaned)
    prose = explicit_content.group("content").strip() if explicit_content else cleaned
    for pattern in _PROVIDER_META_BLOCK_RES:
        prose = pattern.sub("\n\n", prose)
    explicit_content = _EXPLICIT_CONTENT_ONLY_RE.fullmatch(prose)
    if explicit_content:
        prose = explicit_content.group("content").strip()
    tag_matches = list(_XML_LIKE_TAG_RE.finditer(prose))
    unknown_wrappers: set[str] = set()
    for match in tag_matches:
        name = match.group("name").casefold()
        if name in _KNOWN_PROSE_WRAPPER_TAGS:
            continue
        token = match.group(0)
        boundary_tag = match.start() == 0 or prose[match.start() - 1].isspace()
        paired_tag = bool(
            re.search(rf"<\s*/\s*{re.escape(name)}\s*>", prose, re.IGNORECASE)
        )
        if boundary_tag or paired_tag or token.rstrip().endswith("/>"):
            unknown_wrappers.add(name)
    if unknown_wrappers:
        return StoryProseExtraction(
            status="rejected",
            prose="",
            reason_codes=("unknown_wrapper",),
        )
    remaining_known_wrappers = {
        match.group("name").casefold()
        for match in _XML_LIKE_TAG_RE.finditer(prose)
        if match.group("name").casefold() in _KNOWN_PROSE_WRAPPER_TAGS
        and match.group("name").casefold() != "content"
    }
    if remaining_known_wrappers:
        return StoryProseExtraction(
            status="rejected",
            prose="",
            reason_codes=("unclosed_known_wrapper",),
        )
    prose = prose.strip()
    if not prose:
        return StoryProseExtraction(
            status="rejected",
            prose="",
            reason_codes=("empty_prose",),
        )
    return StoryProseExtraction(status="accepted", prose=prose)


def clean_generated_text(text: str) -> str:
    """Return only publishable prose; rejected envelopes yield no text."""

    return extract_story_prose(text).prose


def mechanical_issues(text: str) -> list[str]:
    """List defects detectable from the prose alone, without any context.

    Every issue here is a reason to reject a candidate outright: none of them
    can be fixed by accepting the text and hoping, and all of them are visible
    to a reader.
    """

    value = str(text or "")
    issues: list[str] = []
    if not value.strip():
        issues.append("empty")
    if _LENGTH_META_RE.search(value):
        issues.append("length_meta_language")
    if _PLACEHOLDER_RE.search(value):
        issues.append("placeholder")
    if _CONTENT_WRAPPER_RE.search(value):
        issues.append("content_wrapper")
    if _SCENE_HEADING_RE.search(value):
        issues.append("scene_heading")
    if not re.search(r"[。！？!?…][\"'”’」』）》】〕〉》)}\]]*$", value.rstrip()):
        issues.append("incomplete_ending")
    paragraphs = [re.sub(r"\s+", "", item) for item in re.split(r"\n\s*\n", value) if item.strip()]
    substantive_paragraphs = [
        item for item in paragraphs if len(item) >= _DUPLICATE_PARAGRAPH_MIN_CHARS
    ]
    if len(substantive_paragraphs) != len(set(substantive_paragraphs)):
        issues.append("duplicate_paragraph")
    normalized = re.sub(r"\s+", "", value)
    if len(normalized) >= REPETITION_NGRAM_SIZE:
        ngrams: Dict[str, int] = {}
        for offset in range(len(normalized) - (REPETITION_NGRAM_SIZE - 1)):
            item = normalized[offset : offset + REPETITION_NGRAM_SIZE]
            ngrams[item] = ngrams.get(item, 0) + 1
        if sum(count - 1 for count in ngrams.values() if count > 1) > _REPEATED_NGRAM_LIMIT:
            issues.append("repeated_ngram")
    return issues


def repeated_ngram_count(text: str) -> int:
    """Count repeated long n-grams so a candidate can be compared to its draft.

    A patch is rejected when it makes repetition *worse* than the draft, which
    needs a magnitude rather than the boolean ``mechanical_issues`` reports.
    """

    normalized = re.sub(r"\s+", "", str(text or ""))
    if len(normalized) < REPETITION_NGRAM_SIZE:
        return 0
    ngrams: Dict[str, int] = {}
    for offset in range(len(normalized) - (REPETITION_NGRAM_SIZE - 1)):
        item = normalized[offset : offset + REPETITION_NGRAM_SIZE]
        ngrams[item] = ngrams.get(item, 0) + 1
    return sum(count - 1 for count in ngrams.values() if count > 1)


def duplicate_paragraph_count(text: str) -> int:
    """Count repeated substantive paragraphs, for the same comparative reason.

    A draft that already repeats a paragraph must still be patchable, so the
    length path asks whether a candidate is *worse* than its draft rather than
    whether it is clean.
    """

    paragraphs = [
        re.sub(r"\s+", "", item)
        for item in re.split(r"\n\s*\n", str(text or ""))
        if item.strip()
    ]
    substantive = [item for item in paragraphs if len(item) >= _DUPLICATE_PARAGRAPH_MIN_CHARS]
    return len(substantive) - len(set(substantive))


def narrates_in_second_person(text: str) -> bool:
    paragraphs = [item.strip() for item in re.split(r"\n+", str(text or "")) if item.strip()]
    if not paragraphs:
        return False
    second_person_starts = sum(item.startswith(("你", "您")) for item in paragraphs)
    unquoted = _DIALOGUE_SPAN_RE.sub("", str(text or ""))
    second_person_mentions = unquoted.count("你") + unquoted.count("您")
    return (
        second_person_starts >= 2
        and second_person_starts / len(paragraphs) >= 0.08
        and second_person_mentions >= 4
    )


def contains_explicit_content(text: str) -> bool:
    return bool(_EXPLICIT_CONTENT_RE.search(str(text or "")))


def contextual_quality_issues(
    text: str,
    *,
    source_context: str,
    user_task: str = "",
) -> list[str]:
    """Add defects that only show up against the surrounding chapter.

    Perspective and explicit-content boundaries are properties of the story, not
    of the sentence: second person is only a defect if the chapter is not written
    that way, and explicit content is only unexpected if nothing around it asked
    for that register.
    """

    issues = mechanical_issues(text)
    if narrates_in_second_person(text) and not narrates_in_second_person(source_context):
        issues.append("narrative_perspective_shift")
    if contains_explicit_content(text) and not contains_explicit_content(
        f"{user_task}\n{source_context}"
    ):
        issues.append("unexpected_explicit_content")
    return issues
