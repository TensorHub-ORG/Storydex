"""Bounded story generation pipeline: stage candidates, commit once.

The Agent flow this replaces wrote the draft to the chapter file first and only
then measured it. A short draft therefore left prose on disk that the next call
had to append to, which is how one 3000-character target ended up holding two
chapters. It also made the logical call count unknowable: nothing distinguished
"one prose call" from "a draft plus two corrections".

This module fixes both by separating measuring from writing:

1. the draft is staged as JSON under the agent temp root, not the chapter;
2. the authoritative counter classifies it against both bands;
3. precision may add at most one revision candidate, also staged;
4. exactly one candidate reaches ``apply_story_generation_increment()``.

Structure is the hard gate and word count is candidate status (plan §8.1). A
draft with a wrong path or fragment count is not written at all, because its
length describes something other than the planned chapter. A draft that is
merely short is still written: it is the only structurally valid prose the turn
produced, and discarding it would trade a length miss for lost work.

Staging lives under ``.storydex/.agent/temp/`` rather than ``.storydex/temp/``:
the latter is the user's own scratch space, and agent intermediates must never
be mistaken for it or injected back as context.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

from services.story_call_accounting import (
    STORY_INITIAL_GENERATION_PURPOSE,
    STORY_LENGTH_REVISION_PURPOSE,
    STORY_SECOND_DRAFT_PURPOSE,
    StoryCallAccounting,
)
from services.story_word_count_service import (
    asymmetric_length_loss,
    chapter_precision_band,
    classify_chapter_length_tier,
    classify_chapter_word_count,
    count_story_text_words,
)

STAGING_DIRECTORY_NAME = "story-generation"
INITIAL_CANDIDATE_NAME = "initial"
REVISION_CANDIDATE_NAME = "revision"
SECOND_DRAFT_CANDIDATE_NAME = "second-draft"
CANDIDATE_FILE_VERSION = 1

# Which candidate the turn committed. ``draft`` covers both the normal path and
# a precision turn that kept its draft; the two revision outcomes are named
# separately so a report never claims precision for a partial recovery.
CANDIDATE_SOURCE_DRAFT = "draft"
CANDIDATE_SOURCE_REVISION = "revision"

SELECTION_PRECISION_ACHIEVED = "precision_achieved"
SELECTION_WIDE_RECOVERED = "wide_recovered"
SELECTION_DRAFT_KEPT = "draft_kept"
SELECTION_DRAFT_IN_BAND = "draft_in_precision_band"
SELECTION_PRECISION_DISABLED = "precision_disabled"
SELECTION_REVISION_UNAVAILABLE = "revision_unavailable"
SELECTION_REVISION_REJECTED = "revision_rejected"
SELECTION_FIRST_DRAFT_ACCEPTED = "first_draft_accepted"
SELECTION_SECOND_DRAFT_SELECTED = "second_draft_selected"
SELECTION_NO_ELIGIBLE_CANDIDATE = "no_eligible_candidate"

# A candidate only wins on a wide recovery if it closes at least half the gap.
# Anything less is noise, and accepting it would spend a call to move the number
# without earning the band.
WIDE_RECOVERY_MINIMUM_IMPROVEMENT = 0.50


class StoryGenerationPipelineError(RuntimeError):
    """Raised when the pipeline cannot produce any committable candidate."""


def _staging_root(project_service: Any, workspace_root: Path, trace_id: str) -> Path:
    safe_trace = "".join(
        char if char.isalnum() or char in {"-", "_"} else "_" for char in str(trace_id or "")
    ).strip("_") or "turn"
    return project_service.agent_temp_root(workspace_root) / STAGING_DIRECTORY_NAME / safe_trace


def _fragment_texts(fragments: List[Any]) -> List[str]:
    texts: List[str] = []
    for item in fragments:
        if isinstance(item, dict):
            texts.append(str(item.get("text") or item.get("content") or ""))
        else:
            texts.append(str(item or ""))
    return texts


def _candidate_word_count(fragments: List[Any]) -> int:
    return sum(count_story_text_words(text) for text in _fragment_texts(fragments))


def _band_distance(count: int, band: tuple[int, int]) -> int:
    actual = max(0, int(count))
    low, high = int(band[0]), int(band[1])
    if actual < low:
        return low - actual
    if actual > high:
        return actual - high
    return 0


class StoryGenerationPipeline:
    """Run one bounded story turn: one draft, at most one revision, one write."""

    def __init__(self, project_service: Any) -> None:
        self.project_service = project_service

    # -- staging ---------------------------------------------------------

    def stage_candidate(
        self,
        workspace_root: Path,
        *,
        trace_id: str,
        name: str,
        payload: Dict[str, Any],
        word_count: int,
        status: Dict[str, Any] | None = None,
    ) -> str:
        """Persist one candidate and return its workspace-relative path.

        The staged file holds the prose because that is the point: a rejected
        draft must survive long enough to be revised or recovered instead of
        being regenerated from scratch.
        """

        root = Path(workspace_root).resolve()
        directory = _staging_root(self.project_service, root, trace_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.json"
        path.write_text(
            json.dumps(
                {
                    "_type": "StoryGenerationCandidate",
                    "_version": CANDIDATE_FILE_VERSION,
                    "candidate": str(name),
                    "traceId": str(trace_id or ""),
                    "wordCount": int(word_count),
                    "stagedAt": int(time.time()),
                    "wordCountStatus": dict(status or {}),
                    "payload": payload,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return path.relative_to(root).as_posix()

    def read_staged_candidate(self, workspace_root: Path, relative_path: str) -> Dict[str, Any]:
        path = Path(workspace_root).resolve() / str(relative_path or "")
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def cleanup_staging(self, workspace_root: Path, *, trace_id: str) -> None:
        """Drop staged prose once the chapter is committed.

        Only the prose is removed; metrics that never contained prose are kept
        by the caller. Failure to clean up is not worth failing a written
        chapter over, so errors are swallowed here and left to temp expiry.
        """

        directory = _staging_root(self.project_service, Path(workspace_root).resolve(), trace_id)
        if not directory.is_dir():
            return
        for item in sorted(directory.glob("*.json")):
            try:
                item.unlink()
            except OSError:
                continue
        try:
            directory.rmdir()
        except OSError:
            pass

    # -- candidate selection --------------------------------------------

    def select_asymmetric_candidate(
        self,
        *,
        target: int,
        candidates: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Hard-filter candidates, then minimize asymmetric length loss."""

        eligible = [
            dict(candidate)
            for candidate in candidates
            if candidate.get("validationPassed") is True
            and candidate.get("qualityPassed") is True
        ]
        if not eligible:
            return {
                "source": "",
                "wordCount": 0,
                "eligibleCandidateCount": 0,
                "asymmetricLengthLoss": None,
            }
        selected = min(
            eligible,
            key=lambda candidate: asymmetric_length_loss(
                int(candidate.get("wordCount") or 0),
                target=target,
            ),
        )
        return {
            **selected,
            "eligibleCandidateCount": len(eligible),
            "asymmetricLengthLoss": asymmetric_length_loss(
                int(selected.get("wordCount") or 0),
                target=target,
            ),
        }

    def select_candidate(
        self,
        *,
        target: int,
        draft_word_count: int,
        revision_word_count: int | None,
        revision_quality_passed: bool,
        precision_enabled: bool,
        retained_word_count: int = 0,
        draft_generated_word_count: int | None = None,
        revision_generated_word_count: int | None = None,
        use_band_distance: bool = False,
    ) -> Dict[str, Any]:
        """Choose between draft and revision candidate (plan §7.5).

        Having spent the second call is never a reason to accept a worse
        candidate; keeping the draft is the quality insurance of the whole
        mechanism.
        """

        draft_generated = (
            int(draft_word_count)
            if draft_generated_word_count is None
            else int(draft_generated_word_count)
        )
        revision_generated = (
            int(revision_word_count)
            if revision_word_count is not None and revision_generated_word_count is None
            else int(revision_generated_word_count)
            if revision_generated_word_count is not None
            else None
        )
        draft_status = classify_chapter_word_count(draft_word_count, target=target)
        keep_draft = {
            "source": CANDIDATE_SOURCE_DRAFT,
            "wordCount": int(draft_word_count),
            "retainedWordCount": int(retained_word_count),
            "draftGeneratedWordCount": draft_generated,
            "finalGeneratedWordCount": draft_generated,
            "draftWordCount": int(draft_word_count),
            "finalWordCount": int(draft_word_count),
            "precisionAchieved": bool(draft_status["precisionBandPassed"]),
            "normalBandPassed": bool(draft_status["normalBandPassed"]),
            "draftStatus": draft_status,
        }
        if not precision_enabled:
            return {**keep_draft, "reason": SELECTION_PRECISION_DISABLED}
        if bool(draft_status["precisionBandPassed"]):
            return {**keep_draft, "reason": SELECTION_DRAFT_IN_BAND}
        if revision_word_count is None:
            return {**keep_draft, "reason": SELECTION_REVISION_UNAVAILABLE}
        if not revision_quality_passed:
            return {**keep_draft, "reason": SELECTION_REVISION_REJECTED}

        revision_status = classify_chapter_word_count(revision_word_count, target=target)
        take_revision = {
            "source": CANDIDATE_SOURCE_REVISION,
            "wordCount": int(revision_word_count),
            "retainedWordCount": int(retained_word_count),
            "draftGeneratedWordCount": draft_generated,
            "revisionGeneratedWordCount": revision_generated,
            "finalGeneratedWordCount": revision_generated,
            "draftWordCount": int(draft_word_count),
            "finalWordCount": int(revision_word_count),
            "normalBandPassed": bool(revision_status["normalBandPassed"]),
            "draftStatus": draft_status,
            "revisionStatus": revision_status,
        }
        if bool(revision_status["precisionBandPassed"]):
            return {
                **take_revision,
                "precisionAchieved": True,
                "reason": SELECTION_PRECISION_ACHIEVED,
            }
        # A candidate that missed precision can still rescue a draft that was
        # outside the release band, but it must not be reported as precise.
        if use_band_distance:
            precision_band = chapter_precision_band(target)
            draft_deviation = _band_distance(draft_word_count, precision_band)
            revision_deviation = _band_distance(revision_word_count, precision_band)
        else:
            draft_deviation = abs(int(draft_word_count) - max(1, int(target)))
            revision_deviation = abs(int(revision_word_count) - max(1, int(target)))
        improved_enough = revision_deviation <= draft_deviation * (
            1.0 - WIDE_RECOVERY_MINIMUM_IMPROVEMENT
        )
        if (
            (use_band_distance or not bool(draft_status["normalBandPassed"]))
            and bool(revision_status["normalBandPassed"])
            and improved_enough
        ):
            return {
                **take_revision,
                "precisionAchieved": False,
                "reason": SELECTION_WIDE_RECOVERED,
            }
        return {**keep_draft, "reason": SELECTION_DRAFT_KEPT}

    # -- one bounded turn ------------------------------------------------

    async def run(
        self,
        workspace_root: Path,
        *,
        trace_id: str,
        turn_contract: Dict[str, Any],
        generate_draft: Callable[[], Any],
        generate_second_draft: Callable[[], Any] | None = None,
        revise: Callable[[Dict[str, Any]], Any] | None = None,
        accounting: StoryCallAccounting | None = None,
        on_commit_started: Callable[[], Any] | None = None,
        on_commit_finished: Callable[[], Any] | None = None,
    ) -> Dict[str, Any]:
        """Produce one chapter with one draft call and at most one revision.

        ``generate_draft`` and ``revise`` return an increment payload shaped for
        ``apply_story_generation_increment``. Provider transport lives in the
        adapter they close over, so this pipeline stays testable without a
        network and the call contract stays enforceable in one place.
        """

        root = Path(workspace_root).resolve()
        ledger = accounting if accounting is not None else StoryCallAccounting()
        turn_plan = (
            turn_contract.get("turnPlan") if isinstance(turn_contract.get("turnPlan"), dict) else {}
        )
        policy = (
            turn_plan.get("wordCountPolicy")
            if isinstance(turn_plan.get("wordCountPolicy"), dict)
            else {}
        )
        tier_mode = str(policy.get("mode") or "").strip().lower() == "tier"
        precision = policy.get("precision") if isinstance(policy.get("precision"), dict) else {}
        precision_enabled = bool(precision.get("enabled")) and not tier_mode
        asymmetric = (
            policy.get("asymmetric")
            if isinstance(policy.get("asymmetric"), dict)
            else {}
        )
        asymmetric_enabled = bool(asymmetric.get("enabled")) and not tier_mode
        target = max(
            1,
            int(
                policy.get("target")
                or turn_plan.get("chapterWordCountTarget")
                or (
                    int(policy.get("preferredMinimum") or 1)
                    + int(policy.get("preferredMaximum") or 1)
                )
                // 2
            )
            or 1,
        )
        retained_word_count = max(0, int(policy.get("retainedWordCount") or 0))
        word_count_scope = str(policy.get("scope") or "").strip().lower()
        candidate_scoped_tier = tier_mode and word_count_scope == "candidate"

        # One logical call and the request that carries it. Transport retries
        # happen inside the adapter the callable closes over; that adapter shares
        # this ledger, so a retried call reports one logical call and several
        # attempts rather than several calls.
        ledger.record_logical_call(STORY_INITIAL_GENERATION_PURPOSE)
        ledger.record_provider_attempt(STORY_INITIAL_GENERATION_PURPOSE)
        draft_payload = await _maybe_await(generate_draft())
        if not isinstance(draft_payload, dict):
            raise StoryGenerationPipelineError("draft generation returned no increment payload")
        draft_length_control = (
            dict(draft_payload.get("lengthControl"))
            if isinstance(draft_payload.get("lengthControl"), dict)
            else {}
        )
        draft_fragments = list(draft_payload.get("fragments") or [])
        draft_generated_count = _candidate_word_count(draft_fragments)
        draft_resulting_count = retained_word_count + draft_generated_count
        draft_count = (
            draft_generated_count if candidate_scoped_tier else draft_resulting_count
        )
        draft_status = (
            classify_chapter_length_tier(
                draft_count,
                tier=policy.get("tier"),
                policy=policy,
            )
            if tier_mode
            else classify_chapter_word_count(draft_count, target=target)
        )
        staged: Dict[str, str] = {
            INITIAL_CANDIDATE_NAME: self.stage_candidate(
                root,
                trace_id=trace_id,
                name=INITIAL_CANDIDATE_NAME,
                payload=draft_payload,
                word_count=draft_count,
                status=draft_status,
            )
        }

        if tier_mode:
            draft_validation = self.project_service.validate_story_generation_candidate(
                root,
                draft_payload,
                generation_contract=turn_contract,
            )
            draft_quality_passed = draft_payload.get("qualityPassed") is True
            draft_quality_issues = [
                str(item) for item in list(draft_payload.get("qualityIssues") or [])
            ]
            committable = bool(
                draft_validation.get("passed") and draft_quality_passed
            )
            selection = {
                "source": CANDIDATE_SOURCE_DRAFT if committable else "",
                "reason": (
                    SELECTION_FIRST_DRAFT_ACCEPTED
                    if committable
                    else SELECTION_NO_ELIGIBLE_CANDIDATE
                ),
                "wordCount": draft_count,
                "retainedWordCount": retained_word_count,
                "resultingWordCount": draft_resulting_count,
                "draftGeneratedWordCount": draft_generated_count,
                "finalGeneratedWordCount": (
                    draft_generated_count if committable else 0
                ),
                "draftWordCount": draft_count,
                "finalWordCount": draft_count if committable else 0,
                "chapterLengthTier": str(draft_status.get("tier") or ""),
                "tierHit": bool(draft_status.get("tierHit")),
                "tierDeviation": str(draft_status.get("tierDeviation") or ""),
                "committable": committable,
                "draftStatus": draft_status,
                "draftValidation": draft_validation,
                "draftQualityPassed": draft_quality_passed,
                "draftQualityIssues": draft_quality_issues,
                "eligibleCandidateCount": 1 if committable else 0,
            }
            if committable:
                if on_commit_started is not None:
                    on_commit_started()
                try:
                    applied = self.project_service.apply_story_generation_increment(
                        root,
                        draft_payload,
                        generation_contract=turn_contract,
                    )
                finally:
                    if on_commit_finished is not None:
                        on_commit_finished()
                committed = bool(applied.get("ok"))
                if committed:
                    self.cleanup_staging(root, trace_id=trace_id)
            else:
                applied = {
                    "ok": False,
                    "accepted": False,
                    "code": "story_tier_candidate_not_committable",
                    "message": str(
                        draft_validation.get("message")
                        or "正文未通过结构、质量或安全写入门禁。"
                    ),
                    "wordCountValidation": draft_validation,
                    "qualityIssues": draft_quality_issues,
                    "writtenPaths": [],
                    "writtenPathCount": 0,
                }
                committed = False
            violations = ledger.contract_violations(
                precision_enabled=False,
                asymmetric_enabled=False,
            )
            return {
                "_type": "StoryGenerationPipelineResult",
                "_version": 1,
                "traceId": str(trace_id or ""),
                "committed": committed,
                "applied": applied,
                "chapterLengthTier": str(draft_status.get("tier") or ""),
                "tierHit": bool(draft_status.get("tierHit")),
                "tierDeviation": str(draft_status.get("tierDeviation") or ""),
                "precisionEnabled": False,
                "asymmetricLengthEnabled": False,
                "selection": selection,
                "retainedWordCount": retained_word_count,
                "resultingWordCount": draft_resulting_count,
                "draftGeneratedWordCount": draft_generated_count,
                "draftWordCount": draft_count,
                "normalBandPassed": bool(draft_status.get("tierHit")),
                "precisionAchieved": None,
                "stagedCandidates": dict(staged) if not committed else {},
                "callAccounting": ledger.payload(),
                "contractViolations": violations,
            }

        if asymmetric_enabled:
            draft_validation = self.project_service.validate_story_generation_candidate(
                root,
                draft_payload,
                generation_contract=turn_contract,
            )
            draft_quality_passed = bool(draft_payload.get("qualityPassed", True))
            draft_eligible = bool(
                draft_validation.get("passed") and draft_quality_passed
            )
            second_payload: Dict[str, Any] | None = None
            second_generated_count: int | None = None
            second_count: int | None = None
            second_status: Dict[str, Any] = {}
            second_validation: Dict[str, Any] = {}
            second_quality_passed = False
            second_quality_issues: List[str] = []
            second_error = ""
            if not draft_eligible and generate_second_draft is not None:
                ledger.record_logical_call(STORY_SECOND_DRAFT_PURPOSE)
                ledger.record_provider_attempt(STORY_SECOND_DRAFT_PURPOSE)
                try:
                    candidate = await _maybe_await(generate_second_draft())
                except Exception as exc:  # noqa: BLE001 - one failed second draft ends the turn
                    candidate = None
                    second_error = type(exc).__name__
                if isinstance(candidate, dict):
                    second_payload = candidate
                    second_generated_count = _candidate_word_count(
                        list(candidate.get("fragments") or [])
                    )
                    second_count = retained_word_count + second_generated_count
                    second_status = classify_chapter_word_count(
                        second_count,
                        target=target,
                    )
                    second_validation = (
                        self.project_service.validate_story_generation_candidate(
                            root,
                            second_payload,
                            generation_contract=turn_contract,
                        )
                    )
                    second_quality_passed = bool(
                        second_payload.get("qualityPassed", True)
                    )
                    second_quality_issues = [
                        str(item)
                        for item in list(second_payload.get("qualityIssues") or [])
                    ]
                    staged[SECOND_DRAFT_CANDIDATE_NAME] = self.stage_candidate(
                        root,
                        trace_id=trace_id,
                        name=SECOND_DRAFT_CANDIDATE_NAME,
                        payload=second_payload,
                        word_count=second_count,
                        status=second_status,
                    )
            second_eligible = bool(
                second_payload is not None
                and second_validation.get("passed")
                and second_quality_passed
            )
            ranked = self.select_asymmetric_candidate(
                target=target,
                candidates=[
                    {
                        "source": CANDIDATE_SOURCE_DRAFT,
                        "wordCount": draft_count,
                        "generatedWordCount": draft_generated_count,
                        "validationPassed": bool(draft_validation.get("passed")),
                        "qualityPassed": draft_quality_passed,
                    },
                    *(
                        [
                            {
                                "source": "second_draft",
                                "wordCount": int(second_count or 0),
                                "generatedWordCount": int(
                                    second_generated_count or 0
                                ),
                                "validationPassed": bool(
                                    second_validation.get("passed")
                                ),
                                "qualityPassed": second_quality_passed,
                            }
                        ]
                        if second_payload is not None
                        else []
                    ),
                ],
            )
            selected_source = str(ranked.get("source") or "")
            selected_payload = (
                draft_payload
                if selected_source == CANDIDATE_SOURCE_DRAFT
                else second_payload
                if selected_source == "second_draft" and second_eligible
                else None
            )
            selected_count = int(ranked.get("wordCount") or 0)
            selected_generated_count = int(ranked.get("generatedWordCount") or 0)
            selected_status = (
                draft_status
                if selected_source == CANDIDATE_SOURCE_DRAFT
                else second_status
                if selected_source == "second_draft"
                else {}
            )
            selection = {
                "source": selected_source,
                "reason": (
                    SELECTION_FIRST_DRAFT_ACCEPTED
                    if selected_source == CANDIDATE_SOURCE_DRAFT
                    else SELECTION_SECOND_DRAFT_SELECTED
                    if selected_source == "second_draft"
                    else SELECTION_NO_ELIGIBLE_CANDIDATE
                ),
                "wordCount": selected_count,
                "retainedWordCount": retained_word_count,
                "draftGeneratedWordCount": draft_generated_count,
                "secondDraftGeneratedWordCount": second_generated_count,
                "finalGeneratedWordCount": selected_generated_count,
                "draftWordCount": draft_count,
                "secondDraftWordCount": second_count,
                "finalWordCount": selected_count,
                "normalBandPassed": bool(selected_status.get("normalBandPassed")),
                "draftStatus": draft_status,
                "secondDraftStatus": second_status,
                "draftValidation": draft_validation,
                "secondDraftValidation": second_validation,
                "draftQualityPassed": draft_quality_passed,
                "draftQualityIssues": [
                    str(item)
                    for item in list(draft_payload.get("qualityIssues") or [])
                ],
                "secondDraftQualityPassed": second_quality_passed,
                "secondDraftQualityIssues": second_quality_issues,
                "eligibleCandidateCount": int(
                    ranked.get("eligibleCandidateCount") or 0
                ),
                "asymmetricLengthLoss": ranked.get("asymmetricLengthLoss"),
            }
            if selected_payload is not None:
                if on_commit_started is not None:
                    on_commit_started()
                try:
                    applied = self.project_service.apply_story_generation_increment(
                        root,
                        selected_payload,
                        generation_contract=turn_contract,
                    )
                finally:
                    if on_commit_finished is not None:
                        on_commit_finished()
                committed = bool(applied.get("ok"))
                if committed:
                    self.cleanup_staging(root, trace_id=trace_id)
            else:
                applied = {
                    "ok": False,
                    "accepted": False,
                    "code": "no_eligible_story_candidate",
                    "writtenPaths": [],
                    "writtenPathCount": 0,
                }
                committed = False
            return {
                "_type": "StoryGenerationPipelineResult",
                "_version": 1,
                "traceId": str(trace_id or ""),
                "committed": committed,
                "applied": applied,
                "target": target,
                "precisionEnabled": False,
                "asymmetricLengthEnabled": True,
                "selection": selection,
                "retainedWordCount": retained_word_count,
                "draftGeneratedWordCount": draft_generated_count,
                "draftWordCount": draft_count,
                "secondDraftGeneratedWordCount": second_generated_count,
                "secondDraftWordCount": second_count,
                "secondDraftError": second_error,
                "normalBandPassed": bool(selection.get("normalBandPassed")),
                "precisionAchieved": None,
                "stagedCandidates": dict(staged) if not committed else {},
                "callAccounting": ledger.payload(),
                "contractViolations": ledger.contract_violations(
                    precision_enabled=False,
                    asymmetric_enabled=True,
                ),
            }

        revision_payload: Dict[str, Any] | None = None
        revision_count: int | None = None
        revision_quality_passed = False
        revision_error = ""
        revision_strategy = ""
        revision_length_control: Dict[str, Any] = {}
        if precision_enabled and not bool(draft_status["precisionBandPassed"]) and revise is not None:
            try:
                candidate = await _maybe_await(
                    revise(
                        {
                            "draftPayload": draft_payload,
                            "draftWordCount": draft_count,
                            "draftGeneratedWordCount": draft_generated_count,
                            "retainedWordCount": retained_word_count,
                            "wordCountStatus": draft_status,
                            "target": target,
                            "direction": str(draft_status["direction"]),
                        }
                    )
                )
            except Exception as exc:  # noqa: BLE001 - a failed revision keeps the draft
                # Plan §8.2: a revision timeout, transport error or illegal
                # patch cancels the revision and never retries. The draft is
                # still a committable chapter.
                candidate = None
                revision_error = type(exc).__name__
            provider_call_made = not (
                isinstance(candidate, dict)
                and candidate.get("providerCallMade") is False
            )
            if isinstance(candidate, dict):
                revision_strategy = str(candidate.get("strategy") or "")
                if isinstance(candidate.get("lengthControl"), dict):
                    revision_length_control = dict(candidate["lengthControl"])
            if provider_call_made:
                ledger.record_logical_call(STORY_LENGTH_REVISION_PURPOSE)
                ledger.record_provider_attempt(STORY_LENGTH_REVISION_PURPOSE)
            if isinstance(candidate, dict) and candidate.get("fragments"):
                revision_payload = candidate
                revision_generated_count = _candidate_word_count(
                    list(candidate.get("fragments") or [])
                )
                revision_count = retained_word_count + revision_generated_count
                revision_quality_passed = bool(candidate.get("qualityPassed", True))
                staged[REVISION_CANDIDATE_NAME] = self.stage_candidate(
                    root,
                    trace_id=trace_id,
                    name=REVISION_CANDIDATE_NAME,
                    payload=revision_payload,
                    word_count=revision_count,
                    status=classify_chapter_word_count(revision_count, target=target),
                )

        selection = self.select_candidate(
            target=target,
            draft_word_count=draft_count,
            revision_word_count=revision_count,
            revision_quality_passed=revision_quality_passed,
            precision_enabled=precision_enabled,
            retained_word_count=retained_word_count,
            draft_generated_word_count=draft_generated_count,
            revision_generated_word_count=(
                revision_count - retained_word_count
                if revision_count is not None
                else None
            ),
            use_band_distance=bool(draft_length_control),
        )
        chosen = (
            revision_payload
            if selection["source"] == CANDIDATE_SOURCE_REVISION and revision_payload is not None
            else draft_payload
        )

        if draft_length_control:
            chosen_length_control = (
                revision_length_control
                if selection["source"] == CANDIDATE_SOURCE_REVISION
                and revision_length_control
                else draft_length_control
            )
            if (
                selection["source"] == CANDIDATE_SOURCE_DRAFT
                and revision_length_control
                and revision_length_control.get("lengthFallbackReason")
            ):
                chosen_length_control = {
                    **draft_length_control,
                    "lengthFallbackReason": str(
                        revision_length_control.get("lengthFallbackReason") or ""
                    ),
                }
            elif revision_error:
                chosen_length_control = {
                    **draft_length_control,
                    "lengthFallbackReason": "repair_failed",
                }
            final_edit_ids = [
                str(item)
                for item in list(chosen_length_control.get("selectedEditIds") or [])
            ]
            selection = {
                **selection,
                "lengthControlStrategy": str(
                    draft_length_control.get("lengthControlStrategy") or ""
                ),
                "canonicalWordCount": int(
                    draft_length_control.get("canonicalWordCount") or draft_count
                ),
                "normalBandPassed": bool(selection.get("normalBandPassed")),
                "precisionAchieved": (
                    bool(selection.get("precisionAchieved"))
                    if precision_enabled
                    else None
                ),
                "selectedEditIds": final_edit_ids,
                "rejectedEditIds": [
                    str(item)
                    for item in list(
                        chosen_length_control.get("rejectedEditIds") or []
                    )
                ],
                "rejectedEditReasonCounts": {
                    str(key): int(value)
                    for key, value in dict(
                        chosen_length_control.get("rejectedEditReasonCounts") or {}
                    ).items()
                },
                "evaluatedCombinationCount": int(
                    chosen_length_control.get("evaluatedCombinationCount") or 0
                ),
                "lengthFallbackReason": str(
                    chosen_length_control.get("lengthFallbackReason") or ""
                ),
                "generatedOverheadRatio": draft_length_control.get(
                    "generatedOverheadRatio"
                ),
            }

        # The single write. Everything above measured candidates in memory so
        # that the chapter file goes from unwritten to final in one step.
        if on_commit_started is not None:
            on_commit_started()
        try:
            applied = self.project_service.apply_story_generation_increment(
                root,
                chosen,
                generation_contract=turn_contract,
            )
        finally:
            if on_commit_finished is not None:
                on_commit_finished()
        committed = bool(applied.get("ok"))
        if committed:
            self.cleanup_staging(root, trace_id=trace_id)

        return {
            "_type": "StoryGenerationPipelineResult",
            "_version": 1,
            "traceId": str(trace_id or ""),
            "committed": committed,
            "applied": applied,
            "target": target,
            "precisionEnabled": precision_enabled,
            "selection": selection,
            "retainedWordCount": retained_word_count,
            "draftGeneratedWordCount": draft_generated_count,
            "draftWordCount": draft_count,
            "revisionGeneratedWordCount": (
                revision_count - retained_word_count
                if revision_count is not None
                else None
            ),
            "revisionWordCount": revision_count,
            "revisionError": revision_error,
            "revisionStrategy": revision_strategy,
            "lengthControlStrategy": str(
                selection.get("lengthControlStrategy") or ""
            ),
            "canonicalWordCount": int(
                selection.get("canonicalWordCount") or draft_count
            ),
            "normalBandPassed": bool(selection.get("normalBandPassed")),
            "precisionAchieved": selection.get("precisionAchieved"),
            "selectedEditIds": [
                str(item) for item in list(selection.get("selectedEditIds") or [])
            ],
            "rejectedEditIds": [
                str(item) for item in list(selection.get("rejectedEditIds") or [])
            ],
            "rejectedEditReasonCounts": {
                str(key): int(value)
                for key, value in dict(
                    selection.get("rejectedEditReasonCounts") or {}
                ).items()
            },
            "evaluatedCombinationCount": int(
                selection.get("evaluatedCombinationCount") or 0
            ),
            "lengthFallbackReason": str(
                selection.get("lengthFallbackReason") or ""
            ),
            "generatedOverheadRatio": selection.get("generatedOverheadRatio"),
            # Staged paths stay in the result so a failed write can be
            # recovered instead of forcing the model to write the chapter again.
            "stagedCandidates": dict(staged) if not committed else {},
            "callAccounting": ledger.payload(),
            "contractViolations": ledger.contract_violations(
                precision_enabled=precision_enabled
            ),
        }


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


_PIPELINE: StoryGenerationPipeline | None = None


def get_story_generation_pipeline() -> StoryGenerationPipeline:
    global _PIPELINE
    if _PIPELINE is None:
        from services.story_project_service import get_story_project_service

        _PIPELINE = StoryGenerationPipeline(get_story_project_service())
    return _PIPELINE
