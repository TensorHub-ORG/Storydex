"""W2-2: resolve and validate the chapter a story turn writes into.

Which chapter a turn targets is decided locally, before any prose call, and it is
a hard gate. The bug this closes wrote "chapter 2" prose into
``chapters/第1章 .../002.md``: the planner only recognised an explicit rewrite and
otherwise appended into whichever chapter happened to be active, so a request
naming a later chapter silently extended the current one.

Structure is never repaired by a second model call. A turn whose target path is
wrong is stopped here rather than handed to the length-revision path, which can
only change how much prose exists, not where it lives.

The resolver is deliberately pure: prompt text plus the chapter numbers that
already exist, in and a decision out. That keeps it testable without a project on
disk, and keeps the filesystem checks in ``validate_chapter_plan`` where they can
report a named issue.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, Sequence

CHAPTER_ACTION_CONTINUE_FRAGMENT = "continue_current_fragment"
CHAPTER_ACTION_CONTINUE_CHAPTER = "continue_current_chapter"
CHAPTER_ACTION_CREATE_NEXT_CHAPTER = "create_next_chapter"
CHAPTER_ACTION_CREATE_SPECIFIC_CHAPTER = "create_specific_chapter"
CHAPTER_ACTION_REWRITE_EXISTING = "rewrite_existing"

# The write path branches on these names, so the vocabulary is closed and
# ordered. A new action must be added here and handled explicitly rather than
# arriving as an unrecognised string that falls through to "append to active".
CHAPTER_ACTIONS = (
    CHAPTER_ACTION_CONTINUE_FRAGMENT,
    CHAPTER_ACTION_CONTINUE_CHAPTER,
    CHAPTER_ACTION_CREATE_NEXT_CHAPTER,
    CHAPTER_ACTION_CREATE_SPECIFIC_CHAPTER,
    CHAPTER_ACTION_REWRITE_EXISTING,
)

_SINGLE_FILE_CONTENT_MODE = "single_file"

_CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CHINESE_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}

# "重写第3章" must not read as "create chapter 3": the number is the target of a
# rewrite, and creating would build a duplicate directory beside the real one.
_REWRITE_RE = re.compile(
    r"(?:重写|重新写|改写|rewrite)[^\d一二三四五六七八九十百千万两零〇]{0,12}"
    r"(?:第)?([0-9一二三四五六七八九十百千万两零〇]+)\s*章",
    re.IGNORECASE,
)
_CHAPTER_NUMBER_RE = re.compile(r"第\s*([0-9一二三四五六七八九十百千万两零〇]+)\s*章")
# "下一章"/"新的一章" name no number, so they resolve against the current maximum.
_NEXT_CHAPTER_RE = re.compile(r"(?:下一章|下1章|新的一章|新一章|开新章|新开一章|开一章|次章)")
_NEW_CHAPTER_HINT_RE = re.compile(r"(?:新|另)(?:起|开|建|写)?(?:一)?章")
_PATH_CHAPTER_RE = re.compile(r"第\s*([0-9一二三四五六七八九十百千万两零〇]+)\s*章")


def parse_chapter_number(raw: Any) -> int:
    """Parse an Arabic or Chinese chapter number; 0 means unparseable."""

    text = str(raw or "").strip()
    if not text:
        return 0
    if text.isdigit():
        try:
            return int(text)
        except ValueError:
            return 0
    total = 0
    section = 0
    current = 0
    for char in text:
        if char in _CHINESE_DIGITS:
            current = _CHINESE_DIGITS[char]
            continue
        unit = _CHINESE_UNITS.get(char)
        if unit is None:
            return 0
        if unit == 10000:
            section = (section + max(current, 1)) * unit
            total += section
            section = 0
        else:
            section += max(current, 1) * unit
        current = 0
    return total + section + current


def chapter_number_from_path(value: Any) -> int:
    """Read the chapter number out of a project-relative path."""

    normalized = str(value or "").strip().replace("\\", "/").lstrip("/")
    if not normalized:
        return 0
    for part in Path(normalized).parts:
        match = _PATH_CHAPTER_RE.search(part)
        if match:
            return parse_chapter_number(match.group(1))
    return 0


def _requested_chapter_number(prompt: str) -> int:
    match = _CHAPTER_NUMBER_RE.search(str(prompt or ""))
    return parse_chapter_number(match.group(1)) if match else 0


def _rewrite_chapter_number(prompt: str) -> int:
    match = _REWRITE_RE.search(str(prompt or ""))
    return parse_chapter_number(match.group(1)) if match else 0


def _wants_next_chapter(prompt: str) -> bool:
    text = str(prompt or "")
    return bool(_NEXT_CHAPTER_RE.search(text) or _NEW_CHAPTER_HINT_RE.search(text))


def resolve_chapter_action(
    *,
    prompt: str,
    active_file: str,
    chapter_numbers: Sequence[int] | Iterable[int],
    content_mode: str,
    is_new_story: bool,
) -> Dict[str, Any]:
    """Decide which chapter this turn writes into.

    Precedence is ordered by how explicit the request is. An explicit rewrite
    beats a bare chapter number, a bare chapter number beats "next chapter", and
    only a request naming nothing at all falls back to the active chapter. The
    old planner effectively had that order inverted, which is what sent
    chapter-2 prose into chapter 1.
    """

    numbers = sorted({int(item) for item in chapter_numbers if int(item) > 0})
    maximum = numbers[-1] if numbers else 0
    active_number = chapter_number_from_path(active_file)
    single_file = str(content_mode or "").strip() == _SINGLE_FILE_CONTENT_MODE

    def decision(action: str, number: int, reason: str) -> Dict[str, Any]:
        return {
            "action": action,
            "targetChapterNumber": max(1, int(number)),
            "reason": reason,
            "isNewChapter": action
            in (CHAPTER_ACTION_CREATE_NEXT_CHAPTER, CHAPTER_ACTION_CREATE_SPECIFIC_CHAPTER),
        }

    if is_new_story or not numbers:
        return decision(CHAPTER_ACTION_CREATE_NEXT_CHAPTER, 1, "new_story")

    rewrite_number = _rewrite_chapter_number(prompt)
    if rewrite_number:
        return decision(CHAPTER_ACTION_REWRITE_EXISTING, rewrite_number, "explicit_rewrite")

    requested = _requested_chapter_number(prompt)
    if requested:
        if requested in numbers:
            # The chapter already exists, so naming it means "work on it", not
            # "build a second directory with the same number".
            action = (
                CHAPTER_ACTION_CONTINUE_FRAGMENT
                if single_file and requested == active_number
                else CHAPTER_ACTION_CONTINUE_CHAPTER
            )
            return decision(action, requested, "existing_chapter_named")
        return decision(
            CHAPTER_ACTION_CREATE_SPECIFIC_CHAPTER,
            requested,
            "explicit_chapter_number",
        )

    if _wants_next_chapter(prompt):
        return decision(CHAPTER_ACTION_CREATE_NEXT_CHAPTER, maximum + 1, "next_chapter_requested")

    if active_number:
        action = (
            CHAPTER_ACTION_CONTINUE_FRAGMENT if single_file else CHAPTER_ACTION_CONTINUE_CHAPTER
        )
        return decision(action, active_number, "active_chapter")

    return decision(CHAPTER_ACTION_CONTINUE_CHAPTER, maximum, "latest_chapter")


def validate_chapter_plan(
    workspace_root: Path,
    *,
    action: str,
    target_chapter_number: int,
    authoritative_chapter_path: str,
    fragment_paths: Sequence[str],
    chapter_numbers: Sequence[int] | Iterable[int],
) -> Dict[str, Any]:
    """Check a planned chapter target before any prose call runs.

    Every issue is a named string so a failure report says what was wrong rather
    than only that validation failed. All checks run, so one call reports every
    problem instead of making the caller fix them one at a time.
    """

    root = Path(workspace_root).resolve()
    issues: list[str] = []
    numbers = sorted({int(item) for item in chapter_numbers if int(item) > 0})
    normalized_action = str(action or "").strip()
    target_number = int(target_chapter_number or 0)
    chapter_path = str(authoritative_chapter_path or "").strip().replace("\\", "/").strip("/")
    creates_chapter = normalized_action in (
        CHAPTER_ACTION_CREATE_NEXT_CHAPTER,
        CHAPTER_ACTION_CREATE_SPECIFIC_CHAPTER,
    )

    if normalized_action not in CHAPTER_ACTIONS:
        issues.append("unknown_chapter_action")
    if target_number <= 0:
        issues.append("missing_target_chapter_number")
    if not chapter_path:
        issues.append("missing_authoritative_chapter_path")
    elif not chapter_path.startswith("chapters/"):
        issues.append("chapter_path_outside_chapters")

    # The directory name carries the chapter number the reader sees. If it
    # disagrees with the planned number, one of the two is wrong and prose would
    # land under a misleading heading.
    path_number = chapter_number_from_path(chapter_path)
    if chapter_path and target_number > 0 and path_number and path_number != target_number:
        issues.append("chapter_number_path_mismatch")

    if creates_chapter and numbers and target_number <= numbers[-1]:
        issues.append("chapter_number_not_advancing")

    normalized_fragments: list[str] = []
    for raw in fragment_paths:
        candidate = str(raw or "").strip().replace("\\", "/").lstrip("/")
        if not candidate:
            issues.append("empty_fragment_path")
            continue
        normalized_fragments.append(candidate)

    if not normalized_fragments:
        issues.append("missing_fragment_paths")
    if len(normalized_fragments) != len(set(normalized_fragments)):
        issues.append("duplicate_fragment_paths")

    for candidate in normalized_fragments:
        # Traversal is checked by resolving against the project root rather than
        # by string matching, so "a/../../b" cannot slip through.
        try:
            resolved = (root / candidate).resolve()
            resolved.relative_to(root)
        except (ValueError, OSError):
            issues.append("fragments_outside_authoritative_chapter")
            break
        if chapter_path and Path(candidate).parent.as_posix() != chapter_path:
            issues.append("fragments_outside_authoritative_chapter")
            break

    if chapter_path:
        absolute = root / chapter_path
        if creates_chapter and absolute.exists():
            issues.append("chapter_directory_already_exists")
        if normalized_action == CHAPTER_ACTION_REWRITE_EXISTING:
            if target_number and target_number not in numbers:
                issues.append("rewrite_target_missing")
            elif not absolute.exists():
                issues.append("rewrite_target_missing")
        if (
            normalized_action
            in (CHAPTER_ACTION_CONTINUE_CHAPTER, CHAPTER_ACTION_CONTINUE_FRAGMENT)
            and target_number
            and target_number not in numbers
        ):
            issues.append("continue_target_missing")

    ordered = [issue for index, issue in enumerate(issues) if issue not in issues[:index]]
    return {
        "_type": "ChapterPlanValidation",
        "_version": 1,
        "passed": not ordered,
        "action": normalized_action,
        "targetChapterNumber": target_number,
        "authoritativeChapterPath": chapter_path,
        "authoritativeFragmentPaths": normalized_fragments,
        "issues": ordered,
    }
