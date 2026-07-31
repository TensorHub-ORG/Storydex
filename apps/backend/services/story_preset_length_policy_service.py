"""Separate preset-owned paragraph shape from program-owned chapter length.

Chapter length is the product of two independent factors::

    chapter characters = paragraph count x characters per paragraph

Presets legitimately own the second factor: modules such as ``长自然段`` or
``严禁频繁分段`` are style decisions the user selected.  The program owns the
first factor, because paragraph count is the only length quantity the model
reliably follows.

Two things are needed to keep those factors from colliding:

1. Classify which paragraph-shape band the active presets ask for, so the
   calibration layer can look up the right characters-per-paragraph value.
2. Strip *quantitative* length directives from preset text.  A preset that says
   "不少于2000字" or "分5-8段" competes with the turn contract for the same
   factor; qualitative density wording ("内容要详实") is deliberately kept,
   because it shapes prose rather than dictating chapter size.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple


PARAGRAPH_DENSITY_BANDS = ("short", "medium", "long")
DEFAULT_PARAGRAPH_DENSITY_BAND = "medium"

# ``medium`` is measured: 7 free whole-chapter samples under presets carrying no
# global paragraph-shape directive produced 43.6 chars/paragraph (CV 6%).  The
# ``short`` and ``long`` values are cold-start estimates only; the calibration
# layer replaces them once that band has enough samples.
DEFAULT_CHARS_PER_PARAGRAPH: Dict[str, int] = {
    "short": 30,
    "medium": 44,
    "long": 110,
}

_LONG_PARAGRAPH_SIGNAL_RE = re.compile(
    r"严禁频繁分段|禁止频繁分段|不要频繁分段|少分段|"
    r"长自然段|完整的?长段|大段为主|段落以长句为主|长句为主|"
    r"(?:不要|不得|避免|禁止|严禁|勿|少)[^。！？；\n]{0,10}(?:单句成段|短段|碎片化)"
)
_SHORT_PARAGRAPH_SIGNAL_RE = re.compile(
    r"单句成段|短段落|多分段|频繁分段|碎片化分段|一句一段"
)
_NEGATION_PREFIX_RE = re.compile(r"(?:不要|不得|避免|禁止|严禁|勿|少|无需|不必)[^。！？；\n]{0,10}$")

# Quantitative directives that compete with the turn contract for chapter size.
# Summary/outline lengths are excluded: they size a different artefact.
_EXCLUDED_LENGTH_SUBJECT_RE = re.compile(r"总结|摘要|概括|大纲|summary|outline", re.IGNORECASE)
# Clauses, not sentences: a comma bounds the match so that trailing style
# wording ("……1500字左右，注意节奏") survives the strip.
_SENTENCE_BODY = r"[^。！？；，,、\n]"
_CLAUSE_END = r"[。！？；，,、]?"
_QUANTITATIVE_LENGTH_PATTERNS: Tuple[re.Pattern[str], ...] = (
    # 本章目标3000字 / 正文目标长度为3000-5000字 / 写约3000字
    re.compile(
        rf"{_SENTENCE_BODY}*"
        rf"(?:(?:本章|章节|正文|全文|本次)[^。！？；，,、\n]{{0,8}}?"
        rf"(?:目标(?:字数|长度)?|长度目标)|"
        rf"(?:写|生成|输出|续写|扩写|创作)[^。！？；，,、\n]{{0,8}}?)"
        rf"[^。！？；，,、\n]{{0,6}}?\d{{2,6}}"
        rf"(?:\s*[-~—至到]\s*\d{{2,6}})?\s*(?:字|个字|字符)"
        rf"{_SENTENCE_BODY}*{_CLAUSE_END}"
    ),
    # 字数：2000字以上 / 输出长度：3000-5000字
    re.compile(
        rf"{_SENTENCE_BODY}*"
        rf"(?:字数|篇幅|输出长度|回复长度|正文长度|全文长度|单次长度)"
        rf"{_SENTENCE_BODY}{{0,20}}?\d{{2,6}}{_SENTENCE_BODY}{{0,8}}?(?:字|个字|字符)"
        rf"{_SENTENCE_BODY}*{_CLAUSE_END}"
    ),
    # 每次回复不少于1500字 / 不超过3000字 / 控制在2000字左右
    re.compile(
        rf"{_SENTENCE_BODY}*"
        rf"(?:不少于|不低于|至少|不超过|不多于|不得少于|不得超过|控制在|保持在|大约|约莫|约)"
        rf"{_SENTENCE_BODY}{{0,6}}?\d{{2,6}}{_SENTENCE_BODY}{{0,8}}?(?:字|个字|字符)"
        rf"{_SENTENCE_BODY}*{_CLAUSE_END}"
    ),
    # 2000字以上 / 1500字左右 / 3000字以内
    re.compile(
        rf"{_SENTENCE_BODY}*\d{{2,6}}\s*(?:字|个字|字符)\s*(?:以上|以内|以下|左右|上下|起步)"
        rf"{_SENTENCE_BODY}*{_CLAUSE_END}"
    ),
    # 分5-8个自然段 / 写10段以上
    re.compile(
        rf"{_SENTENCE_BODY}*(?:分|写|输出|产出|保持)"
        rf"{_SENTENCE_BODY}{{0,6}}?\d{{1,3}}\s*(?:[-~—]\s*\d{{1,3}}\s*)?"
        rf"(?:个)?(?:自然)?段{_SENTENCE_BODY}*{_CLAUSE_END}"
    ),
)


def _signal_is_negated(source: str, start: int) -> bool:
    return bool(_NEGATION_PREFIX_RE.search(source[max(0, start - 24) : start]))


def paragraph_density_signals(text: str) -> Tuple[List[str], List[str]]:
    """Return (long_signals, short_signals) found in one preset module."""

    source = str(text or "")
    long_signals = [match.group(0).strip() for match in _LONG_PARAGRAPH_SIGNAL_RE.finditer(source)]
    short_signals = [
        match.group(0).strip()
        for match in _SHORT_PARAGRAPH_SIGNAL_RE.finditer(source)
        if not _signal_is_negated(source, match.start())
    ]
    return long_signals, short_signals


def classify_paragraph_density_text(chunks: List[str]) -> Dict[str, Any]:
    """Classify paragraph shape from already-collected enabled module texts."""

    long_signals: List[str] = []
    short_signals: List[str] = []
    for chunk in chunks:
        found_long, found_short = paragraph_density_signals(chunk)
        long_signals.extend(found_long)
        short_signals.extend(found_short)
    long_signals = list(dict.fromkeys(long_signals))
    short_signals = list(dict.fromkeys(short_signals))
    if long_signals and not short_signals:
        band, reason = "long", "preset_requires_long_paragraphs"
    elif short_signals and not long_signals:
        band, reason = "short", "preset_requires_short_paragraphs"
    elif long_signals and short_signals:
        band, reason = DEFAULT_PARAGRAPH_DENSITY_BAND, "conflicting_preset_signals"
    else:
        band, reason = DEFAULT_PARAGRAPH_DENSITY_BAND, "no_preset_signal"
    return {
        "band": band,
        "reason": reason,
        "longSignals": long_signals,
        "shortSignals": short_signals,
    }


def classify_paragraph_density(workspace_root: Path) -> Dict[str, Any]:
    """Classify the paragraph shape requested by active project presets.

    Only modules the preset itself leaves enabled are considered: a disabled
    ``长自然段`` module must not move the band.  Any read failure degrades to the
    default band rather than blocking generation.
    """

    root = Path(workspace_root).resolve()
    active_root = root / ".storydex" / "presets" / "active"
    chunks: List[str] = []
    preset_paths: List[str] = []
    for sidecar in sorted(active_root.glob("*.preset.json")):
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        modules = payload.get("modules") if isinstance(payload, dict) else None
        if not isinstance(modules, list):
            continue
        recorded = False
        for module in modules:
            if not isinstance(module, dict) or module.get("enabledByDefault") is False:
                continue
            content = str(module.get("content") or "").strip()
            if not content:
                continue
            chunks.append(content)
            if not recorded:
                preset_paths.append(sidecar.relative_to(root).as_posix())
                recorded = True
    classification = classify_paragraph_density_text(chunks)
    classification["presetPaths"] = preset_paths
    return classification


def strip_quantitative_length_directives(text: str) -> Tuple[str, List[str]]:
    """Remove quantitative chapter-size directives, keep qualitative density.

    Returns the cleaned text plus an audit list of what was removed.  Stripping
    is deliberately conservative: a missed directive only adds noise to length
    control, while an over-eager match would silently delete style rules.
    """

    source = str(text or "")
    if not source:
        return "", []
    removed: List[str] = []

    def _replace(match: re.Match[str]) -> str:
        fragment = match.group(0)
        if not fragment.strip() or _EXCLUDED_LENGTH_SUBJECT_RE.search(fragment):
            return fragment
        removed.append(fragment.strip())
        return ""

    cleaned = source
    for pattern in _QUANTITATIVE_LENGTH_PATTERNS:
        cleaned = pattern.sub(_replace, cleaned)
    if not removed:
        return source, []
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip(), list(dict.fromkeys(removed))
