from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from threading import Lock
from typing import Any, Dict
from uuid import uuid4

from services.story_preset_length_policy_service import (
    DEFAULT_PARAGRAPH_DENSITY_BAND,
    DEFAULT_CHARS_PER_PARAGRAPH,
    PARAGRAPH_DENSITY_BANDS,
)
from services.story_word_count_service import (
    chapter_normal_band,
    classify_chapter_word_count,
)


LENGTH_CALIBRATION_RELATIVE_PATH = Path(".storydex") / "memory" / "length_calibration.json"
LENGTH_GRADE_SIZE = 500
LENGTH_CALIBRATION_VERSION = 3
MIN_CALIBRATION_SAMPLES = 3
MIN_VALID_RESPONSE_RATIO = 0.25
MAX_VALID_RESPONSE_RATIO = 4.0
MAX_CALIBRATION_SAMPLE_AGE_DAYS = 90
MAX_RECENT_CALIBRATION_SAMPLES = 20
MIN_NEARBY_GRADE_SAMPLES = 5
MAX_NEARBY_GRADE_DISTANCE_RATIO = 0.20
NEARBY_GRADE_CORRECTION_STRENGTH = 0.50

# Calibration must describe what the model produces in one unassisted call, so
# every sample records which attempt it came from. A draft that was later
# expanded by a precision revision would otherwise be observed at its corrected
# length and teach calibration that the model writes to target on its own.
INITIAL_ATTEMPT_KIND = "initial"
PRECISION_REVISION_ATTEMPT_KIND = "precision_revision"
CALIBRATION_ATTEMPT_KINDS = (INITIAL_ATTEMPT_KIND, PRECISION_REVISION_ATTEMPT_KIND)

# Three or four samples are enough to notice a bias but not to size it, so they
# move the reference only half way. Five or more apply the full median.
FULL_STRENGTH_CALIBRATION_SAMPLES = 5
PARTIAL_CALIBRATION_CORRECTION_STRENGTH = 0.50
# One adjustment may not move the model-facing reference further than this from
# the product target, whatever the median says. It coincides with the normal
# band today; it is stated separately because it bounds a different thing —
# how far one calibration step may travel, not which results are acceptable.
MAX_REFERENCE_CORRECTION_RATIO = 0.30

# Paragraph-quota calibration. Chapter length is delivered as a paragraph count
# because the model has no representation of character counts but follows a
# paragraph quota reliably. Only characters-per-paragraph needs learning; the
# quota itself is arithmetic.
MIN_PARAGRAPH_CALIBRATION_SAMPLES = 3
MIN_VALID_CHARS_PER_PARAGRAPH = 8.0
MAX_VALID_CHARS_PER_PARAGRAPH = 400.0
PARAGRAPH_QUOTA_MINIMUM = 3
PARAGRAPH_QUOTA_MAXIMUM = 400
PARAGRAPH_QUOTA_TOLERANCE_RATIO = 0.10
MIN_PARAGRAPH_QUOTA_SPAN = 2


class StoryLengthCalibrationService:
    """Persist observations and resolve bounded model-facing length guidance."""

    def __init__(self) -> None:
        self._write_lock = Lock()

    @staticmethod
    def length_grade(reference_word_count: int) -> int:
        reference = max(1, int(reference_word_count))
        return max(
            LENGTH_GRADE_SIZE,
            ((reference + LENGTH_GRADE_SIZE // 2) // LENGTH_GRADE_SIZE) * LENGTH_GRADE_SIZE,
        )

    @staticmethod
    def calibration_path(workspace_root: Path) -> Path:
        return Path(workspace_root).resolve() / LENGTH_CALIBRATION_RELATIVE_PATH

    def resolve_generation_guidance(
        self,
        workspace_root: Path,
        *,
        product_target_word_count: int,
        provider: str,
        model: str,
        now: str = "",
    ) -> Dict[str, Any]:
        """Resolve product acceptance and the model-facing length reference."""

        target = max(1, int(product_target_word_count))
        acceptance_minimum, acceptance_maximum = chapter_normal_band(target)
        normalized_provider = self._normalize_dimension(provider)
        normalized_model = self._normalize_dimension(model)
        model_identity_available = self._has_model_identity(
            normalized_provider,
            normalized_model,
        )
        target_grade = self.length_grade(target)
        effective_now = self._parse_timestamp(now) or datetime.now(timezone.utc)
        oldest_allowed = effective_now - timedelta(days=MAX_CALIBRATION_SAMPLE_AGE_DAYS)
        # Tagged first drafts and untagged legacy samples are collected apart.
        # Only a draft describes what the model does unassisted, but discarding a
        # v2 project's history outright would cold-start every existing project,
        # so legacy samples stay usable as a low-weight reference.
        timed_ratios_by_grade: dict[int, list[tuple[datetime, float]]] = {}
        legacy_timed_ratios_by_grade: dict[int, list[tuple[datetime, float]]] = {}
        payload = (
            self._read_payload(self.calibration_path(workspace_root))
            if model_identity_available
            else self._empty_payload()
        )
        for bucket in payload["buckets"]:
            if not isinstance(bucket, dict):
                continue
            if bucket.get("provider") != normalized_provider or bucket.get("model") != normalized_model:
                continue
            try:
                source_grade = int(bucket.get("targetGrade"))
            except (TypeError, ValueError):
                continue
            timed_ratios = timed_ratios_by_grade.setdefault(source_grade, [])
            legacy_timed_ratios = legacy_timed_ratios_by_grade.setdefault(source_grade, [])
            samples = bucket.get("samples") if isinstance(bucket.get("samples"), list) else []
            for sample in samples:
                if not isinstance(sample, dict):
                    continue
                attempt_kind = self._normalize_attempt_kind(sample.get("attemptKind"))
                # A precision revision measures the model plus a program-driven
                # correction, so it never enters the draft reference.
                if attempt_kind == PRECISION_REVISION_ATTEMPT_KIND:
                    continue
                sample_time = self._parse_timestamp(sample.get("timestamp"))
                if sample_time is None or sample_time < oldest_allowed or sample_time > effective_now:
                    continue
                try:
                    reference = int(sample.get("modelReferenceWordCount"))
                    actual = int(sample.get("actualWordCount"))
                except (TypeError, ValueError):
                    continue
                if reference > 0 and actual > 0:
                    ratio = actual / reference
                    if MIN_VALID_RESPONSE_RATIO <= ratio <= MAX_VALID_RESPONSE_RATIO:
                        if attempt_kind == INITIAL_ATTEMPT_KIND:
                            timed_ratios.append((sample_time, ratio))
                        else:
                            legacy_timed_ratios.append((sample_time, ratio))

        def recent_ratios(
            source: dict[int, list[tuple[datetime, float]]],
        ) -> dict[int, list[float]]:
            resolved: dict[int, list[float]] = {}
            for grade, timed in source.items():
                timed.sort(key=lambda item: item[0], reverse=True)
                resolved[grade] = [
                    ratio for _, ratio in timed[:MAX_RECENT_CALIBRATION_SAMPLES]
                ]
            return resolved

        ratios_by_grade = recent_ratios(timed_ratios_by_grade)
        legacy_ratios_by_grade = recent_ratios(legacy_timed_ratios_by_grade)

        source_grade: int | None = None
        calibration_reason = (
            "insufficient_samples"
            if model_identity_available
            else "model_identity_unavailable"
        )
        correction_strength = 1.0
        attempt_kind_source = INITIAL_ATTEMPT_KIND
        ratios = ratios_by_grade.get(target_grade, [])
        if len(ratios) >= MIN_CALIBRATION_SAMPLES:
            source_grade = target_grade
            calibration_reason = "same_target_grade"
        else:
            nearby_candidates = [
                (abs(grade - target_grade), grade, candidate_ratios)
                for grade, candidate_ratios in ratios_by_grade.items()
                if grade != target_grade
                and abs(grade - target_grade) / max(1, target) <= MAX_NEARBY_GRADE_DISTANCE_RATIO
                and len(candidate_ratios) >= MIN_NEARBY_GRADE_SAMPLES
            ]
            if nearby_candidates:
                _, source_grade, ratios = min(nearby_candidates, key=lambda item: item[0])
                calibration_reason = "nearby_target_grade"
                correction_strength = NEARBY_GRADE_CORRECTION_STRENGTH
            else:
                # No tagged drafts yet. A v2 project's untagged history is the
                # only evidence available, so it is used at half strength until
                # enough drafts accumulate to replace it.
                legacy_ratios = legacy_ratios_by_grade.get(target_grade, [])
                if len(legacy_ratios) >= MIN_CALIBRATION_SAMPLES:
                    source_grade = target_grade
                    ratios = legacy_ratios
                    calibration_reason = "legacy_untagged_samples"
                    correction_strength = PARTIAL_CALIBRATION_CORRECTION_STRENGTH
                    attempt_kind_source = ""

        sample_count = len(ratios)
        calibration_applied = source_grade is not None
        # Three or four samples can show a bias but cannot size it, so they move
        # the reference half way. Both strengths bound the same step, so the
        # stricter one wins rather than compounding into a near-zero correction.
        if calibration_applied and sample_count < FULL_STRENGTH_CALIBRATION_SAMPLES:
            correction_strength = min(
                correction_strength,
                PARTIAL_CALIBRATION_CORRECTION_STRENGTH,
            )
        raw_ratio = float(median(ratios)) if calibration_applied else None
        bounded_ratio = max(0.75, min(1.50, raw_ratio)) if raw_ratio is not None else 1.0
        applied_ratio = 1.0 + (bounded_ratio - 1.0) * correction_strength
        model_reference = int(round(target / applied_ratio))
        # Cap how far one adjustment may move the model-facing reference. This is
        # deliberately not the acceptance band: the two happen to be ±30% today,
        # but one bounds a calibration step and the other classifies a result.
        correction_floor = max(1, int(round(target * (1.0 - MAX_REFERENCE_CORRECTION_RATIO))))
        correction_ceiling = int(round(target * (1.0 + MAX_REFERENCE_CORRECTION_RATIO)))
        model_reference = max(correction_floor, min(correction_ceiling, model_reference))
        return {
            "productTargetWordCount": target,
            "acceptanceMinimum": acceptance_minimum,
            "acceptanceMaximum": acceptance_maximum,
            "modelReferenceWordCount": model_reference,
            "calibration": {
                "status": "applied" if calibration_applied else "fallback",
                "reason": calibration_reason,
                "provider": normalized_provider,
                "model": normalized_model,
                "targetGrade": target_grade,
                "sourceTargetGrade": source_grade,
                "sampleCount": sample_count,
                "medianRatio": raw_ratio,
                "appliedRatio": applied_ratio,
                "correctionStrength": correction_strength,
                # Which kind of sample steered this reference. Empty means the
                # only evidence was untagged legacy history.
                "attemptKind": attempt_kind_source,
            },
        }

    def resolve_paragraph_quota(
        self,
        workspace_root: Path,
        *,
        product_target_word_count: int,
        provider: str,
        model: str,
        density_band: str = DEFAULT_PARAGRAPH_DENSITY_BAND,
        now: str = "",
    ) -> Dict[str, Any]:
        """Translate a chapter character target into a paragraph quota.

        The model is never given the character target: it has no representation
        of character counts, but follows a paragraph quota.  Characters per
        paragraph is owned by preset style (see
        ``story_preset_length_policy_service``) and is the only quantity learned
        here, bucketed by provider/model/density band rather than by length
        grade — paragraph shape barely varies with chapter size, so sharing one
        bucket across targets reaches the sample floor far sooner.
        """

        target = max(1, int(product_target_word_count))
        acceptance_minimum, acceptance_maximum = chapter_normal_band(target)
        band = self._normalize_density_band(density_band)
        normalized_provider = self._normalize_dimension(provider)
        normalized_model = self._normalize_dimension(model)
        model_identity_available = self._has_model_identity(
            normalized_provider,
            normalized_model,
        )
        effective_now = self._parse_timestamp(now) or datetime.now(timezone.utc)
        oldest_allowed = effective_now - timedelta(days=MAX_CALIBRATION_SAMPLE_AGE_DAYS)

        timed_values: list[tuple[datetime, float]] = []
        if model_identity_available:
            payload = self._read_payload(self.calibration_path(workspace_root))
            for bucket in payload["paragraphBuckets"]:
                if (
                    bucket.get("provider") != normalized_provider
                    or bucket.get("model") != normalized_model
                    or bucket.get("densityBand") != band
                ):
                    continue
                for sample in bucket.get("samples") or []:
                    sample_time = self._parse_timestamp(sample.get("timestamp"))
                    if sample_time is None or sample_time < oldest_allowed or sample_time > effective_now:
                        continue
                    try:
                        actual = int(sample.get("actualWordCount"))
                        paragraphs = int(sample.get("actualParagraphCount"))
                    except (TypeError, ValueError):
                        continue
                    if actual <= 0 or paragraphs <= 0:
                        continue
                    value = actual / paragraphs
                    if MIN_VALID_CHARS_PER_PARAGRAPH <= value <= MAX_VALID_CHARS_PER_PARAGRAPH:
                        timed_values.append((sample_time, value))
                break

        timed_values.sort(key=lambda item: item[0], reverse=True)
        values = [value for _, value in timed_values[:MAX_RECENT_CALIBRATION_SAMPLES]]
        calibration_applied = len(values) >= MIN_PARAGRAPH_CALIBRATION_SAMPLES
        if calibration_applied:
            chars_per_paragraph = float(median(values))
            reason = "calibrated_density_band"
        else:
            chars_per_paragraph = float(
                DEFAULT_CHARS_PER_PARAGRAPH.get(
                    band,
                    DEFAULT_CHARS_PER_PARAGRAPH[DEFAULT_PARAGRAPH_DENSITY_BAND],
                )
            )
            reason = (
                "insufficient_samples"
                if model_identity_available
                else "model_identity_unavailable"
            )
        chars_per_paragraph = max(
            MIN_VALID_CHARS_PER_PARAGRAPH,
            min(MAX_VALID_CHARS_PER_PARAGRAPH, chars_per_paragraph),
        )

        quota = int(round(target / chars_per_paragraph))
        quota = max(PARAGRAPH_QUOTA_MINIMUM, min(PARAGRAPH_QUOTA_MAXIMUM, quota))
        span = max(
            MIN_PARAGRAPH_QUOTA_SPAN,
            int(round(quota * PARAGRAPH_QUOTA_TOLERANCE_RATIO)),
        )
        quota_minimum = max(PARAGRAPH_QUOTA_MINIMUM, quota - span)
        quota_maximum = min(PARAGRAPH_QUOTA_MAXIMUM, quota + span)
        return {
            "productTargetWordCount": target,
            "acceptanceMinimum": acceptance_minimum,
            "acceptanceMaximum": acceptance_maximum,
            "paragraphQuota": quota,
            "paragraphQuotaMinimum": quota_minimum,
            "paragraphQuotaMaximum": quota_maximum,
            "charsPerParagraph": round(chars_per_paragraph, 2),
            "densityBand": band,
            "calibration": {
                "status": "applied" if calibration_applied else "fallback",
                "reason": reason,
                "provider": normalized_provider,
                "model": normalized_model,
                "densityBand": band,
                "sampleCount": len(values),
                "medianCharsPerParagraph": (
                    round(float(median(values)), 2) if values else None
                ),
            },
        }

    def append_paragraph_sample(
        self,
        workspace_root: Path,
        *,
        product_target_word_count: int,
        actual_word_count: int,
        actual_paragraph_count: int,
        provider: str,
        model: str,
        density_band: str = DEFAULT_PARAGRAPH_DENSITY_BAND,
        timestamp: str = "",
    ) -> bool:
        """Append one paragraph-shape observation; storage failure is non-fatal."""

        try:
            product_target = int(product_target_word_count)
            actual = int(actual_word_count)
            paragraphs = int(actual_paragraph_count)
        except (TypeError, ValueError):
            return False
        if product_target <= 0 or actual <= 0 or paragraphs <= 0:
            return False
        value = actual / paragraphs
        if not MIN_VALID_CHARS_PER_PARAGRAPH <= value <= MAX_VALID_CHARS_PER_PARAGRAPH:
            return False

        normalized_provider = self._normalize_dimension(provider)
        normalized_model = self._normalize_dimension(model)
        if not self._has_model_identity(normalized_provider, normalized_model):
            return False
        band = self._normalize_density_band(density_band)
        sample = {
            "productTargetWordCount": product_target,
            "actualWordCount": actual,
            "actualParagraphCount": paragraphs,
            "provider": normalized_provider,
            "model": normalized_model,
            "densityBand": band,
            "timestamp": str(timestamp or datetime.now(timezone.utc).isoformat()),
        }
        path = self.calibration_path(workspace_root)
        try:
            with self._write_lock:
                payload = self._read_payload(path)
                buckets = payload["paragraphBuckets"]
                bucket = next(
                    (
                        item
                        for item in buckets
                        if item.get("provider") == normalized_provider
                        and item.get("model") == normalized_model
                        and item.get("densityBand") == band
                    ),
                    None,
                )
                if bucket is None:
                    bucket = {
                        "provider": normalized_provider,
                        "model": normalized_model,
                        "densityBand": band,
                        "samples": [],
                    }
                    buckets.append(bucket)
                samples = bucket.get("samples")
                if not isinstance(samples, list):
                    samples = []
                    bucket["samples"] = samples
                samples.append(sample)
                minimum_time = datetime.min.replace(tzinfo=timezone.utc)
                samples.sort(
                    key=lambda item: self._parse_timestamp(
                        item.get("timestamp") if isinstance(item, dict) else None
                    )
                    or minimum_time
                )
                del samples[:-MAX_RECENT_CALIBRATION_SAMPLES]
                self._write_payload(path, payload)
        except Exception:
            return False
        return True

    def record_paragraph_generation_result(
        self,
        workspace_root: Path,
        *,
        turn_contract: Dict[str, Any] | None,
        validation: Dict[str, Any] | None,
        provider: str,
        model: str,
    ) -> bool:
        """Extract one paragraph-shape sample from an accepted generation result.

        Unlike the character-ratio sample this is recorded even when the chapter
        landed outside the acceptance band: characters-per-paragraph is a style
        observation, and discarding off-target runs would keep a mis-calibrated
        band from ever correcting itself.
        """

        try:
            if not self._has_model_identity(provider, model):
                return False
            contract = turn_contract if isinstance(turn_contract, dict) else {}
            turn_plan = contract.get("turnPlan") if isinstance(contract.get("turnPlan"), dict) else {}
            policy = turn_plan.get("wordCountPolicy") if isinstance(turn_plan.get("wordCountPolicy"), dict) else {}
            result = validation if isinstance(validation, dict) else {}
            if str(policy.get("scope") or "").strip().lower() != "chapter":
                return False
            if not bool(result.get("applicable")) or not bool(result.get("structurePassed", True)):
                return False
            product_target = int(
                result.get("chapterWordCountTarget")
                or turn_plan.get("chapterWordCountTarget")
                or policy.get("target")
                or 0
            )
            return self.append_paragraph_sample(
                workspace_root,
                product_target_word_count=product_target,
                actual_word_count=int(result.get("generatedWordCount") or 0),
                actual_paragraph_count=int(result.get("generatedParagraphCount") or 0),
                provider=provider,
                model=model,
                density_band=str(policy.get("paragraphDensityBand") or DEFAULT_PARAGRAPH_DENSITY_BAND),
            )
        except Exception:
            return False

    @staticmethod
    def _normalize_density_band(value: Any) -> str:
        band = str(value or "").strip().lower()
        return band if band in PARAGRAPH_DENSITY_BANDS else DEFAULT_PARAGRAPH_DENSITY_BAND

    @staticmethod
    def _normalize_attempt_kind(value: Any) -> str:
        """Return a known attempt kind, or "" for an untagged legacy sample.

        Empty is a real state rather than a default: it marks a v2 sample whose
        provenance cannot be recovered, and only tagged ``initial`` samples are
        allowed to steer the model-facing reference.
        """

        kind = str(value or "").strip().lower()
        return kind if kind in CALIBRATION_ATTEMPT_KINDS else ""

    def append_sample(
        self,
        workspace_root: Path,
        *,
        reference_word_count: int | None = None,
        product_target_word_count: int | None = None,
        model_reference_word_count: int | None = None,
        actual_word_count: int,
        provider: str,
        model: str,
        attempt_kind: str = INITIAL_ATTEMPT_KIND,
        normal_band_passed: bool | None = None,
        precision_band_passed: bool | None = None,
        timestamp: str = "",
    ) -> bool:
        """Append one observation; any storage failure is intentionally non-fatal."""

        try:
            legacy_reference = int(reference_word_count) if reference_word_count is not None else None
            product_target = int(
                product_target_word_count
                if product_target_word_count is not None
                else legacy_reference
            )
            model_reference = int(
                model_reference_word_count
                if model_reference_word_count is not None
                else legacy_reference
                if legacy_reference is not None
                else product_target
            )
            actual = int(actual_word_count)
        except (TypeError, ValueError):
            return False
        if product_target <= 0 or model_reference <= 0 or actual < 0:
            return False

        normalized_provider = self._normalize_dimension(provider)
        normalized_model = self._normalize_dimension(model)
        if not self._has_model_identity(normalized_provider, normalized_model):
            return False
        grade = self.length_grade(product_target)
        sample = {
            "productTargetWordCount": product_target,
            "modelReferenceWordCount": model_reference,
            "actualWordCount": actual,
            "provider": normalized_provider,
            "model": normalized_model,
            # Which attempt produced this length decides whether it may steer the
            # reference. A precision revision is stored for reporting only.
            "attemptKind": self._normalize_attempt_kind(attempt_kind) or INITIAL_ATTEMPT_KIND,
            "timestamp": str(timestamp or datetime.now(timezone.utc).isoformat()),
        }
        if normal_band_passed is not None:
            sample["normalBandPassed"] = bool(normal_band_passed)
        if precision_band_passed is not None:
            sample["precisionBandPassed"] = bool(precision_band_passed)
        path = self.calibration_path(workspace_root)

        try:
            with self._write_lock:
                payload = self._read_payload(path)
                buckets = payload["buckets"]
                bucket = next(
                    (
                        item
                        for item in buckets
                        if isinstance(item, dict)
                        and item.get("provider") == normalized_provider
                        and item.get("model") == normalized_model
                        and item.get("targetGrade") == grade
                    ),
                    None,
                )
                if bucket is None:
                    bucket = {
                        "provider": normalized_provider,
                        "model": normalized_model,
                        "targetGrade": grade,
                        "samples": [],
                    }
                    buckets.append(bucket)
                samples = bucket.get("samples")
                if not isinstance(samples, list):
                    samples = []
                    bucket["samples"] = samples
                samples.append(sample)
                minimum_time = datetime.min.replace(tzinfo=timezone.utc)
                samples.sort(
                    key=lambda item: self._parse_timestamp(
                        item.get("timestamp") if isinstance(item, dict) else None
                    )
                    or minimum_time
                )
                del samples[:-MAX_RECENT_CALIBRATION_SAMPLES]
                self._write_payload(path, payload)
        except Exception:
            return False
        return True

    def median_ratio(
        self,
        workspace_root: Path,
        *,
        reference_word_count: int,
        provider: str,
        model: str,
    ) -> float | None:
        """Read the median actual/reference ratio for one provider/model/length bucket."""

        try:
            reference = int(reference_word_count)
        except (TypeError, ValueError):
            return None
        if reference <= 0:
            return None

        normalized_provider = self._normalize_dimension(provider)
        normalized_model = self._normalize_dimension(model)
        if not self._has_model_identity(normalized_provider, normalized_model):
            return None
        path = self.calibration_path(workspace_root)
        payload = self._read_payload(path)
        grade = self.length_grade(reference)
        ratios: list[float] = []
        for bucket in payload["buckets"]:
            if not isinstance(bucket, dict):
                continue
            if (
                bucket.get("provider") != normalized_provider
                or bucket.get("model") != normalized_model
                or bucket.get("targetGrade") != grade
            ):
                continue
            samples = bucket.get("samples") if isinstance(bucket.get("samples"), list) else []
            for sample in samples:
                if not isinstance(sample, dict):
                    continue
                try:
                    sample_reference = int(sample.get("modelReferenceWordCount"))
                    sample_actual = int(sample.get("actualWordCount"))
                except (TypeError, ValueError):
                    continue
                if sample_reference > 0 and sample_actual >= 0:
                    ratios.append(sample_actual / sample_reference)
            break
        return float(median(ratios)) if ratios else None

    def record_generation_result(
        self,
        workspace_root: Path,
        *,
        turn_contract: Dict[str, Any] | None,
        validation: Dict[str, Any] | None,
        provider: str,
        model: str,
    ) -> bool:
        """Extract one chapter-level sample from a generation result.

        Sampling is gated on *structure*, not on the word count. A draft that
        came out short is exactly the observation calibration needs; dropping it
        because it missed the band is survivor bias, and it made the reference
        drift towards "the model already writes to target". The word-count
        outcome is recorded alongside the sample instead of deciding whether the
        sample exists.
        """

        try:
            if not self._has_model_identity(provider, model):
                return False
            contract = turn_contract if isinstance(turn_contract, dict) else {}
            turn_plan = contract.get("turnPlan") if isinstance(contract.get("turnPlan"), dict) else {}
            policy = turn_plan.get("wordCountPolicy") if isinstance(turn_plan.get("wordCountPolicy"), dict) else {}
            result = validation if isinstance(validation, dict) else {}
            if str(policy.get("scope") or "").strip().lower() != "chapter":
                return False
            if not bool(result.get("applicable")):
                return False
            # Structure is the hard gate: a wrong path or fragment count means the
            # length describes something other than the planned chapter.
            if not bool(result.get("structurePassed", result.get("passed"))):
                return False
            product_target = int(
                result.get("chapterWordCountTarget")
                or turn_plan.get("chapterWordCountTarget")
                or policy.get("target")
                or 0
            )
            model_reference = int(policy.get("modelReferenceWordCount") or product_target)
            actual = int(result.get("generatedWordCount") or 0)
            if actual <= 0:
                return False
            status = classify_chapter_word_count(actual, target=product_target)
            return self.append_sample(
                workspace_root,
                product_target_word_count=product_target,
                model_reference_word_count=model_reference,
                actual_word_count=actual,
                provider=provider,
                model=model,
                attempt_kind=self._normalize_attempt_kind(result.get("attemptKind"))
                or INITIAL_ATTEMPT_KIND,
                normal_band_passed=bool(status["normalBandPassed"]),
                precision_band_passed=bool(status["precisionBandPassed"]),
            )
        except Exception:
            return False

    @staticmethod
    def _normalize_dimension(value: Any) -> str:
        return str(value or "").strip() or "unknown"

    @classmethod
    def _has_model_identity(cls, provider: Any, model: Any) -> bool:
        return (
            cls._normalize_dimension(provider) != "unknown"
            and cls._normalize_dimension(model) != "unknown"
        )

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _empty_payload() -> Dict[str, Any]:
        return {
            "_type": "StoryLengthCalibration",
            "_version": LENGTH_CALIBRATION_VERSION,
            "targetGradeSize": LENGTH_GRADE_SIZE,
            "buckets": [],
            "paragraphBuckets": [],
        }

    @classmethod
    def _read_paragraph_buckets(cls, payload: Any) -> list[Dict[str, Any]]:
        raw_buckets = (
            payload.get("paragraphBuckets") if isinstance(payload, dict) else None
        )
        if not isinstance(raw_buckets, list):
            return []
        buckets: list[Dict[str, Any]] = []
        for raw_bucket in raw_buckets:
            if not isinstance(raw_bucket, dict):
                continue
            provider = cls._normalize_dimension(raw_bucket.get("provider"))
            model = cls._normalize_dimension(raw_bucket.get("model"))
            band = cls._normalize_density_band(raw_bucket.get("densityBand"))
            samples: list[Dict[str, Any]] = []
            raw_samples = (
                raw_bucket.get("samples") if isinstance(raw_bucket.get("samples"), list) else []
            )
            for raw_sample in raw_samples:
                if not isinstance(raw_sample, dict):
                    continue
                try:
                    product_target = int(raw_sample.get("productTargetWordCount"))
                    actual = int(raw_sample.get("actualWordCount"))
                    paragraphs = int(raw_sample.get("actualParagraphCount"))
                except (TypeError, ValueError):
                    continue
                samples.append(
                    {
                        "productTargetWordCount": product_target,
                        "actualWordCount": actual,
                        "actualParagraphCount": paragraphs,
                        "provider": cls._normalize_dimension(
                            raw_sample.get("provider", provider)
                        ),
                        "model": cls._normalize_dimension(raw_sample.get("model", model)),
                        "densityBand": cls._normalize_density_band(
                            raw_sample.get("densityBand", band)
                        ),
                        "timestamp": str(raw_sample.get("timestamp") or ""),
                    }
                )
            buckets.append(
                {
                    "provider": provider,
                    "model": model,
                    "densityBand": band,
                    "samples": samples,
                }
            )
        return buckets

    def _read_payload(self, path: Path) -> Dict[str, Any]:
        if not path.is_file():
            return self._empty_payload()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return self._empty_payload()
        if not isinstance(payload, dict) or not isinstance(payload.get("buckets"), list):
            return self._empty_payload()
        buckets: list[Dict[str, Any]] = []
        for raw_bucket in payload["buckets"]:
            if not isinstance(raw_bucket, dict):
                continue
            try:
                target_grade = int(
                    raw_bucket.get("targetGrade", raw_bucket.get("lengthGrade"))
                )
            except (TypeError, ValueError):
                continue
            provider = self._normalize_dimension(raw_bucket.get("provider"))
            model = self._normalize_dimension(raw_bucket.get("model"))
            samples: list[Dict[str, Any]] = []
            raw_samples = (
                raw_bucket.get("samples")
                if isinstance(raw_bucket.get("samples"), list)
                else []
            )
            for raw_sample in raw_samples:
                if not isinstance(raw_sample, dict):
                    continue
                try:
                    legacy_reference = raw_sample.get("referenceWordCount")
                    product_target = int(
                        raw_sample.get("productTargetWordCount", legacy_reference)
                    )
                    model_reference = int(
                        raw_sample.get("modelReferenceWordCount", legacy_reference)
                    )
                    actual = int(raw_sample.get("actualWordCount"))
                except (TypeError, ValueError):
                    continue
                normalized_sample = {
                    "productTargetWordCount": product_target,
                    "modelReferenceWordCount": model_reference,
                    "actualWordCount": actual,
                    "provider": self._normalize_dimension(
                        raw_sample.get("provider", provider)
                    ),
                    "model": self._normalize_dimension(raw_sample.get("model", model)),
                    "timestamp": str(raw_sample.get("timestamp") or ""),
                }
                # v2 samples carry no attemptKind, and the key stays absent rather
                # than being back-filled: a v2 sample that passed validation may
                # have been a draft plus a correction, so claiming "initial" would
                # import exactly the pollution v3 removes.
                attempt_kind = self._normalize_attempt_kind(raw_sample.get("attemptKind"))
                if attempt_kind:
                    normalized_sample["attemptKind"] = attempt_kind
                samples.append(normalized_sample)
            buckets.append(
                {
                    "provider": provider,
                    "model": model,
                    "targetGrade": target_grade,
                    "samples": samples,
                }
            )
        return {
            "_type": "StoryLengthCalibration",
            "_version": LENGTH_CALIBRATION_VERSION,
            "targetGradeSize": LENGTH_GRADE_SIZE,
            "buckets": buckets,
            "paragraphBuckets": self._read_paragraph_buckets(payload),
        }

    @staticmethod
    def _write_payload(path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


_SERVICE = StoryLengthCalibrationService()


def get_story_length_calibration_service() -> StoryLengthCalibrationService:
    return _SERVICE
