from __future__ import annotations

import re
from pathlib import Path

from services.story_prose_quality import extract_story_prose


STORY_WORD_COUNT_ALGORITHM = "storydex_visible_characters_v1"
STORY_PARAGRAPH_COUNT_ALGORITHM = "storydex_blank_line_paragraphs_v1"
# 计数规则的唯一描述来源：正文之外的包装块不计入，模型据此理解自己被怎么计数。
STORY_WORD_COUNT_RULE = (
    "count every non-whitespace Unicode character in the prose itself; "
    "summary/details/thinking wrapper blocks are excluded"
)
STORY_OVER_BUDGET_KEEP_MESSAGE = "正文超过所选篇幅档位的期望范围，已按原文保留。"
STORY_UNDER_BUDGET_KEEP_MESSAGE = "正文低于所选篇幅档位的期望范围，已保留结构完整首稿。"
_PARAGRAPH_SEPARATOR_RE = re.compile(r"\n\s*\n")

CHAPTER_LENGTH_TIERS = ("short", "medium", "long")
DEFAULT_CHAPTER_LENGTH_TIER = "medium"
STORY_LENGTH_TIER_PROMPT_VERSION = "story_length_tier_v1"
STORY_LENGTH_TIER_SCOPE = "candidate"
STORY_LENGTH_TIER_POLICIES: dict[str, dict[str, int]] = {
    "short": {
        "preferredMinimum": 1000,
        "preferredMaximum": 3000,
        "hardMinimum": 700,
        "runtimeSafetyMaximum": 4000,
    },
    "medium": {
        "preferredMinimum": 2200,
        "preferredMaximum": 5000,
        "hardMinimum": 1800,
        "runtimeSafetyMaximum": 7200,
    },
    "long": {
        "preferredMinimum": 3000,
        "preferredMaximum": 6000,
        "hardMinimum": 2500,
        "runtimeSafetyMaximum": 9000,
    },
}
STORY_LENGTH_TIER_PROMPTS = {
    "short": "篇幅档位为短。用短章规模完成本章核心推进，保持剧情完整并自然收束；既定事件、人物事实和叙事节奏优先。",
    "medium": "篇幅档位为中。用常规章节规模完整展开本章主要推进，按剧情需要自然组织场景；既定事件、人物事实和叙事节奏优先。",
    "long": "篇幅档位为长。用长章规模充分展开本章重要推进，按剧情需要容纳多个场景或线索；既定事件、人物事实和叙事节奏优先。",
}

# Chapter length has asymmetric product gates. The lower bound is hard because
# an incomplete chapter must not be reported as success. The upper product
# bound is only an observable signal; a separate, much higher runtime ceiling
# protects context, latency and cost from genuinely runaway output.
WORD_COUNT_POLICY_VERSION = 5
PRECISION_REVISION_STRATEGY = "structured_patch_v1"
ASYMMETRIC_SELECTION_STRATEGY = "asymmetric_length_loss_v1"
MAXIMUM_CONDITIONAL_SECOND_DRAFTS = 1
HARD_MINIMUM_RATIO = 0.85
SOFT_MAXIMUM_RATIO = 1.30
RUNTIME_SAFETY_MAXIMUM_RATIO = 2.0
PRECISION_BAND_RATIO = 0.10
# The precision path may spend at most one extra logical prose call. This bound
# is part of the contract rather than a tunable: a retry loop chasing an exact
# count is what the earlier correction flow did, and it burned calls without
# converging.
MAX_PRECISION_REVISION_CALLS = 1
# Guard against inverted bands for very small targets rather than applying a
# large absolute floor, which would reject legitimately short chapters.
_MINIMUM_BAND_FLOOR = 50

def strip_non_story_wrappers(content: str) -> str:
    """Return the same publishable prose used by quality gates and writes."""

    return extract_story_prose(content).prose


def count_story_text_words(content: str) -> int:
    """Return the same objective count shown by the Storydex editor.

    Storydex treats every non-whitespace Unicode character as one displayed
    "word" for fiction targets.  Keeping this in one backend helper prevents
    Agent prompts, file APIs and post-write validation from drifting apart.
    Non-prose wrapper blocks are excluded so that the count always describes
    the chapter itself.
    """

    return sum(1 for char in strip_non_story_wrappers(content) if not char.isspace())


def count_story_text_paragraphs(content: str) -> int:
    """Return the blank-line paragraph count used for length control.

    Chapter length is controlled by paragraph count rather than by a character
    target, because the model reliably follows a paragraph quota while it has no
    representation of character counts.  This counting rule must stay identical
    to the evaluation harness (``local/wc-live-eval``) so that calibration
    samples and offline reports describe the same quantity.  Wrapper blocks are
    excluded for the same reason they are excluded from the character count.
    """

    return sum(
        1
        for item in _PARAGRAPH_SEPARATOR_RE.split(strip_non_story_wrappers(content))
        if item.strip()
    )


def count_story_file_words(path: Path) -> int:
    return count_story_text_words(Path(path).read_text(encoding="utf-8-sig"))


def migrate_chapter_word_count_target(value: object) -> str:
    """Map one legacy numeric target to its replacement semantic tier."""

    try:
        target = int(value)
    except (TypeError, ValueError):
        return DEFAULT_CHAPTER_LENGTH_TIER
    if target <= 2000:
        return "short"
    if target <= 4000:
        return "medium"
    return "long"


def normalize_chapter_length_tier(
    value: object,
    *,
    legacy_target: object = None,
) -> str:
    """Return a supported tier, falling back through the one-release migration."""

    normalized = str(value or "").strip().lower()
    if normalized in CHAPTER_LENGTH_TIERS:
        return normalized
    if legacy_target is not None:
        return migrate_chapter_word_count_target(legacy_target)
    return DEFAULT_CHAPTER_LENGTH_TIER


def chapter_length_tier_prompt(tier: object) -> str:
    return STORY_LENGTH_TIER_PROMPTS[normalize_chapter_length_tier(tier)]


def chapter_length_tier_policy_payload(
    tier: object,
    *,
    preferred_minimum: int | None = None,
    preferred_maximum: int | None = None,
) -> dict[str, object]:
    """Freeze the v5 single-candidate policy for one turn.

    Preferred bounds may come from observation, but the hard write bounds,
    prompt version and one-call budget are immutable product safeguards.
    """

    normalized = normalize_chapter_length_tier(tier)
    fixed = STORY_LENGTH_TIER_POLICIES[normalized]
    observed_minimum = int(
        fixed["preferredMinimum"]
        if preferred_minimum is None
        else preferred_minimum
    )
    observed_maximum = int(
        fixed["preferredMaximum"]
        if preferred_maximum is None
        else preferred_maximum
    )
    if observed_minimum > observed_maximum:
        observed_minimum, observed_maximum = observed_maximum, observed_minimum
    return {
        "version": WORD_COUNT_POLICY_VERSION,
        "mode": "tier",
        "scope": STORY_LENGTH_TIER_SCOPE,
        "algorithm": STORY_WORD_COUNT_ALGORITHM,
        "countingRule": STORY_WORD_COUNT_RULE,
        "tier": normalized,
        "promptVersion": STORY_LENGTH_TIER_PROMPT_VERSION,
        "preferredMinimum": max(1, observed_minimum),
        "preferredMaximum": max(1, observed_maximum),
        "hardMinimum": fixed["hardMinimum"],
        "runtimeSafetyMaximum": fixed["runtimeSafetyMaximum"],
        "maximumProseCalls": 1,
        "retryOnLengthMiss": False,
    }


def classify_chapter_length_tier(
    count: int,
    *,
    tier: object,
    policy: dict[str, object] | None = None,
) -> dict[str, object]:
    """Classify a count against the inclusive preferred and write bands."""

    snapshot = dict(policy or chapter_length_tier_policy_payload(tier))
    normalized = normalize_chapter_length_tier(snapshot.get("tier") or tier)
    fixed = STORY_LENGTH_TIER_POLICIES[normalized]
    preferred_minimum = int(
        snapshot.get("preferredMinimum") or fixed["preferredMinimum"]
    )
    preferred_maximum = int(
        snapshot.get("preferredMaximum") or fixed["preferredMaximum"]
    )
    hard_minimum = int(snapshot.get("hardMinimum") or fixed["hardMinimum"])
    safety_maximum = int(
        snapshot.get("runtimeSafetyMaximum")
        or fixed["runtimeSafetyMaximum"]
    )
    actual = max(0, int(count))
    below_preferred = actual < preferred_minimum
    above_preferred = actual > preferred_maximum
    tier_hit = not below_preferred and not above_preferred
    hard_minimum_passed = actual >= hard_minimum
    runtime_safety_exceeded = actual > safety_maximum
    committable = hard_minimum_passed and not runtime_safety_exceeded
    deviation = (
        "below_preferred"
        if below_preferred
        else "above_preferred"
        if above_preferred
        else "in_preferred"
    )
    return {
        "tier": normalized,
        "promptVersion": str(
            snapshot.get("promptVersion") or STORY_LENGTH_TIER_PROMPT_VERSION
        ),
        "actualWordCount": actual,
        "preferredMinimum": preferred_minimum,
        "preferredMaximum": preferred_maximum,
        "hardMinimum": hard_minimum,
        "runtimeSafetyMaximum": safety_maximum,
        "tierHit": tier_hit,
        "tierDeviation": deviation,
        "belowPreferred": below_preferred,
        "abovePreferred": above_preferred,
        "hardMinimumPassed": hard_minimum_passed,
        "runtimeSafetyExceeded": runtime_safety_exceeded,
        "productGatePassed": committable,
        "writeGatePassed": committable,
        "committable": committable,
    }


def _band(target: int, ratio: float) -> tuple[int, int]:
    center = max(1, int(target))
    low = max(_MINIMUM_BAND_FLOOR, int(round(center * (1.0 - ratio))))
    high = int(round(center * (1.0 + ratio)))
    if low > high:
        low, high = high, low
    return low, high


def chapter_normal_band(target: int) -> tuple[int, int]:
    """Return the inclusive hard-minimum/soft-maximum product interval."""

    center = max(1, int(target))
    return (
        max(_MINIMUM_BAND_FLOOR, int(round(center * HARD_MINIMUM_RATIO))),
        int(round(center * SOFT_MAXIMUM_RATIO)),
    )


def chapter_runtime_safety_maximum(target: int) -> int:
    """Return the inclusive infrastructure ceiling for one completed chapter."""

    center = max(1, int(target))
    _, soft_maximum = chapter_normal_band(center)
    return max(soft_maximum, int(round(center * RUNTIME_SAFETY_MAXIMUM_RATIO)))


def asymmetric_length_loss(count: int, *, target: int) -> int:
    """Return the deterministic underlength-biased candidate loss."""

    actual = max(0, int(count))
    center = max(1, int(target))
    return 2 * (center - actual) if actual < center else actual - center


def chapter_precision_band(target: int) -> tuple[int, int]:
    """Return the inclusive precision band for a chapter target (target ±10%)."""

    return _band(target, PRECISION_BAND_RATIO)


def classify_chapter_word_count(count: int, *, target: int) -> dict[str, object]:
    """Describe one measured chapter against both bands.

    The caller gets a single structured status so that the pipeline, calibration
    sampling and events never re-derive the boundaries and drift apart. Both
    bands are inclusive: at the default target 3000 a chapter of exactly 2100
    passes the normal band and exactly 3300 passes the precision band.

    ``direction`` names the revision a precision candidate would need. It is
    empty inside the precision band, where no second call is allowed to run.
    """

    actual = max(0, int(count))
    center = max(1, int(target))
    hard_minimum, soft_maximum = chapter_normal_band(center)
    runtime_safety_maximum = chapter_runtime_safety_maximum(center)
    precision_minimum, precision_maximum = chapter_precision_band(center)
    precision_passed = precision_minimum <= actual <= precision_maximum
    hard_minimum_passed = actual >= hard_minimum
    above_soft_maximum = actual > soft_maximum
    runtime_safety_exceeded = actual > runtime_safety_maximum
    if precision_passed:
        direction = ""
    else:
        direction = "expand" if actual < precision_minimum else "compress"
    return {
        "target": center,
        "actualWordCount": actual,
        "normalMinimum": hard_minimum,
        "normalMaximum": soft_maximum,
        "hardMinimum": hard_minimum,
        "softMaximum": soft_maximum,
        "runtimeSafetyMaximum": runtime_safety_maximum,
        "precisionMinimum": precision_minimum,
        "precisionMaximum": precision_maximum,
        "hardMinimumPassed": hard_minimum_passed,
        "aboveSoftMaximum": above_soft_maximum,
        "runtimeSafetyExceeded": runtime_safety_exceeded,
        "productGatePassed": hard_minimum_passed and not runtime_safety_exceeded,
        "normalBandPassed": hard_minimum_passed and not above_soft_maximum,
        "precisionBandPassed": precision_passed,
        "direction": direction,
    }


def precision_policy_payload(target: int, *, enabled: bool, reason: str) -> dict[str, object]:
    """Describe the optional precision path for one turn contract.

    The band is published even when the switch is off so that observability can
    report how far a normal draft landed from precision without a second call.
    ``reason`` records which input decided the switch, which is what makes an
    unexpected extra call traceable after the fact.
    """

    minimum, maximum = chapter_precision_band(target)
    return {
        "enabled": bool(enabled),
        "minimum": minimum,
        "maximum": maximum,
        "maximumRevisionCalls": MAX_PRECISION_REVISION_CALLS,
        "revisionStrategy": PRECISION_REVISION_STRATEGY,
        "reason": str(reason),
    }


def asymmetric_policy_payload(target: int, *, enabled: bool) -> dict[str, object]:
    """Snapshot the conditional-second-draft policy into a turn contract."""

    hard_minimum, soft_maximum = chapter_normal_band(target)
    return {
        "enabled": bool(enabled),
        "hardMinimum": hard_minimum,
        "softMaximum": soft_maximum,
        "runtimeSafetyMaximum": chapter_runtime_safety_maximum(target),
        "maximumSecondDrafts": MAXIMUM_CONDITIONAL_SECOND_DRAFTS,
        "selectionStrategy": ASYMMETRIC_SELECTION_STRATEGY,
    }
