from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from threading import Lock
from typing import Any, Dict
from uuid import uuid4


LENGTH_CALIBRATION_RELATIVE_PATH = Path(".storydex") / "memory" / "length_calibration.json"
LENGTH_GRADE_SIZE = 500


class StoryLengthCalibrationService:
    """Persist passive chapter-length observations without affecting generation."""

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

    def append_sample(
        self,
        workspace_root: Path,
        *,
        reference_word_count: int,
        actual_word_count: int,
        provider: str,
        model: str,
        timestamp: str = "",
    ) -> bool:
        """Append one observation; any storage failure is intentionally non-fatal."""

        try:
            reference = int(reference_word_count)
            actual = int(actual_word_count)
        except (TypeError, ValueError):
            return False
        if reference <= 0 or actual < 0:
            return False

        normalized_provider = self._normalize_dimension(provider)
        normalized_model = self._normalize_dimension(model)
        grade = self.length_grade(reference)
        sample = {
            "referenceWordCount": reference,
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
                        and item.get("lengthGrade") == grade
                    ),
                    None,
                )
                if bucket is None:
                    bucket = {
                        "provider": normalized_provider,
                        "model": normalized_model,
                        "lengthGrade": grade,
                        "samples": [],
                    }
                    buckets.append(bucket)
                samples = bucket.get("samples")
                if not isinstance(samples, list):
                    samples = []
                    bucket["samples"] = samples
                samples.append(sample)
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

        path = self.calibration_path(workspace_root)
        payload = self._read_payload(path)
        normalized_provider = self._normalize_dimension(provider)
        normalized_model = self._normalize_dimension(model)
        grade = self.length_grade(reference)
        ratios: list[float] = []
        for bucket in payload["buckets"]:
            if not isinstance(bucket, dict):
                continue
            if (
                bucket.get("provider") != normalized_provider
                or bucket.get("model") != normalized_model
                or bucket.get("lengthGrade") != grade
            ):
                continue
            samples = bucket.get("samples") if isinstance(bucket.get("samples"), list) else []
            for sample in samples:
                if not isinstance(sample, dict):
                    continue
                try:
                    sample_reference = int(sample.get("referenceWordCount"))
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
            contract = turn_contract if isinstance(turn_contract, dict) else {}
            turn_plan = contract.get("turnPlan") if isinstance(contract.get("turnPlan"), dict) else {}
            policy = turn_plan.get("wordCountPolicy") if isinstance(turn_plan.get("wordCountPolicy"), dict) else {}
            result = validation if isinstance(validation, dict) else {}
            if str(policy.get("scope") or "").strip().lower() != "chapter":
                return False
            if not bool(result.get("applicable")) or not bool(result.get("passed")):
                return False
            reference = int(
                result.get("chapterWordCountTarget")
                or turn_plan.get("chapterWordCountTarget")
                or policy.get("target")
                or 0
            )
            actual = int(result.get("generatedWordCount") or 0)
            return self.append_sample(
                workspace_root,
                reference_word_count=reference,
                actual_word_count=actual,
                provider=provider,
                model=model,
            )
        except Exception:
            return False

    @staticmethod
    def _normalize_dimension(value: Any) -> str:
        return str(value or "").strip() or "unknown"

    @staticmethod
    def _empty_payload() -> Dict[str, Any]:
        return {
            "_type": "StoryLengthCalibration",
            "_version": 1,
            "lengthGradeSize": LENGTH_GRADE_SIZE,
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
        return {
            "_type": "StoryLengthCalibration",
            "_version": 1,
            "lengthGradeSize": LENGTH_GRADE_SIZE,
            "buckets": payload["buckets"],
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
