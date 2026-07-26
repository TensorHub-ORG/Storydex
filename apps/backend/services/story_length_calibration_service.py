from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from threading import Lock
from typing import Any, Dict
from uuid import uuid4


LENGTH_CALIBRATION_RELATIVE_PATH = Path(".storydex") / "memory" / "length_calibration.json"
LENGTH_GRADE_SIZE = 500
LENGTH_CALIBRATION_VERSION = 2
MIN_CALIBRATION_SAMPLES = 3
MIN_VALID_RESPONSE_RATIO = 0.25
MAX_VALID_RESPONSE_RATIO = 4.0
MAX_CALIBRATION_SAMPLE_AGE_DAYS = 90
MAX_RECENT_CALIBRATION_SAMPLES = 20
MIN_NEARBY_GRADE_SAMPLES = 5
MAX_NEARBY_GRADE_DISTANCE_RATIO = 0.20
NEARBY_GRADE_CORRECTION_STRENGTH = 0.50


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
        acceptance_minimum = max(50, int(round(target * 0.70)))
        acceptance_maximum = int(round(target * 1.30))
        normalized_provider = self._normalize_dimension(provider)
        normalized_model = self._normalize_dimension(model)
        model_identity_available = self._has_model_identity(
            normalized_provider,
            normalized_model,
        )
        target_grade = self.length_grade(target)
        effective_now = self._parse_timestamp(now) or datetime.now(timezone.utc)
        oldest_allowed = effective_now - timedelta(days=MAX_CALIBRATION_SAMPLE_AGE_DAYS)
        timed_ratios_by_grade: dict[int, list[tuple[datetime, float]]] = {}
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
            samples = bucket.get("samples") if isinstance(bucket.get("samples"), list) else []
            for sample in samples:
                if not isinstance(sample, dict):
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
                        timed_ratios.append((sample_time, ratio))

        ratios_by_grade: dict[int, list[float]] = {}
        for source_grade, timed_ratios in timed_ratios_by_grade.items():
            timed_ratios.sort(key=lambda item: item[0], reverse=True)
            ratios_by_grade[source_grade] = [
                ratio for _, ratio in timed_ratios[:MAX_RECENT_CALIBRATION_SAMPLES]
            ]

        source_grade: int | None = None
        calibration_reason = (
            "insufficient_samples"
            if model_identity_available
            else "model_identity_unavailable"
        )
        correction_strength = 1.0
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

        sample_count = len(ratios)
        calibration_applied = source_grade is not None
        raw_ratio = float(median(ratios)) if calibration_applied else None
        bounded_ratio = max(0.75, min(1.50, raw_ratio)) if raw_ratio is not None else 1.0
        applied_ratio = 1.0 + (bounded_ratio - 1.0) * correction_strength
        model_reference = int(round(target / applied_ratio))
        model_reference = max(acceptance_minimum, min(acceptance_maximum, model_reference))
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
            },
        }

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
            "timestamp": str(timestamp or datetime.now(timezone.utc).isoformat()),
        }
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
        """Extract one chapter-level sample from an accepted generation result."""

        try:
            if not self._has_model_identity(provider, model):
                return False
            contract = turn_contract if isinstance(turn_contract, dict) else {}
            turn_plan = contract.get("turnPlan") if isinstance(contract.get("turnPlan"), dict) else {}
            policy = turn_plan.get("wordCountPolicy") if isinstance(turn_plan.get("wordCountPolicy"), dict) else {}
            result = validation if isinstance(validation, dict) else {}
            if str(policy.get("scope") or "").strip().lower() != "chapter":
                return False
            if not bool(result.get("applicable")) or not bool(result.get("passed")):
                return False
            product_target = int(
                result.get("chapterWordCountTarget")
                or turn_plan.get("chapterWordCountTarget")
                or policy.get("target")
                or 0
            )
            model_reference = int(policy.get("modelReferenceWordCount") or product_target)
            actual = int(result.get("generatedWordCount") or 0)
            return self.append_sample(
                workspace_root,
                product_target_word_count=product_target,
                model_reference_word_count=model_reference,
                actual_word_count=actual,
                provider=provider,
                model=model,
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
        }

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
                samples.append(
                    {
                        "productTargetWordCount": product_target,
                        "modelReferenceWordCount": model_reference,
                        "actualWordCount": actual,
                        "provider": self._normalize_dimension(
                            raw_sample.get("provider", provider)
                        ),
                        "model": self._normalize_dimension(raw_sample.get("model", model)),
                        "timestamp": str(raw_sample.get("timestamp") or ""),
                    }
                )
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
