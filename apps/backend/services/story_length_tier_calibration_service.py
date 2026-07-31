from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from threading import Lock
from typing import Any
from uuid import uuid4

from services.story_word_count_service import (
    CHAPTER_LENGTH_TIERS,
    STORY_LENGTH_TIER_POLICIES,
    STORY_LENGTH_TIER_PROMPT_VERSION,
    chapter_length_tier_policy_payload,
    normalize_chapter_length_tier,
)


LENGTH_TIER_CALIBRATION_RELATIVE_PATH = (
    Path(".storydex") / "memory" / "length_tier_calibration.json"
)
LENGTH_TIER_CALIBRATION_VERSION = 2
MIN_TIER_CALIBRATION_SAMPLES = 12
MAX_TIER_CALIBRATION_SAMPLE_AGE_DAYS = 90
MAX_RECENT_TIER_CALIBRATION_SAMPLES = 30
INITIAL_TIER_ATTEMPT_KIND = "initial"
TIER_CALIBRATION_WORD_COUNT_SCOPE = "candidate"


class StoryLengthTierCalibrationService:
    """Observe semantic length tiers without feeding measurements back to prose."""

    def __init__(self) -> None:
        self._write_lock = Lock()

    @staticmethod
    def calibration_path(workspace_root: Path) -> Path:
        return Path(workspace_root).resolve() / LENGTH_TIER_CALIBRATION_RELATIVE_PATH

    def resolve_policy(
        self,
        workspace_root: Path,
        *,
        tier: object,
        provider: str,
        model: str,
        prompt_version: str = STORY_LENGTH_TIER_PROMPT_VERSION,
        now: str = "",
    ) -> dict[str, object]:
        normalized_tier = normalize_chapter_length_tier(tier)
        identity = self._identity(provider, model, prompt_version)
        payload = self._read_payload(self.calibration_path(workspace_root))
        observation = self._compute_observation(
            payload,
            identity=identity,
            now=self._parse_timestamp(now) or datetime.now(timezone.utc),
        )
        selected_band = (
            observation["bands"].get(normalized_tier)
            if observation["status"] == "applied"
            else None
        )
        policy = chapter_length_tier_policy_payload(
            normalized_tier,
            preferred_minimum=(
                int(selected_band[0]) if isinstance(selected_band, list) else None
            ),
            preferred_maximum=(
                int(selected_band[1]) if isinstance(selected_band, list) else None
            ),
        )
        policy["calibration"] = {
            "status": observation["status"],
            "reason": observation["reason"],
            "provider": identity[0],
            "model": identity[1],
            "promptVersion": identity[2],
            "calibrationVersion": self._stored_calibration_version(
                payload,
                identity,
            ),
            "sampleCounts": observation["sampleCounts"],
            "medians": observation["medians"],
            "observedBands": observation["bands"],
        }
        return policy

    def record_sample(
        self,
        workspace_root: Path,
        *,
        provider: str,
        model: str,
        tier: object,
        actual_word_count: int,
        tier_hit: bool,
        structure_passed: bool,
        machine_quality_passed: bool,
        word_count_scope: str = TIER_CALIBRATION_WORD_COUNT_SCOPE,
        attempt_kind: str = INITIAL_TIER_ATTEMPT_KIND,
        logical_prose_calls: int = 1,
        completion_tokens: int | None = None,
        duration_ms: int | None = None,
        trace_id: str = "",
        timestamp: str = "",
        prompt_version: str = STORY_LENGTH_TIER_PROMPT_VERSION,
    ) -> bool:
        """Record one structure-valid initial candidate, including tier misses."""

        normalized_attempt = str(attempt_kind or "").strip().lower()
        normalized_scope = str(word_count_scope or "").strip().lower()
        if (
            normalized_attempt != INITIAL_TIER_ATTEMPT_KIND
            or normalized_scope != TIER_CALIBRATION_WORD_COUNT_SCOPE
            or not structure_passed
        ):
            return False
        actual = max(0, int(actual_word_count))
        if actual <= 0:
            return False
        normalized_tier = normalize_chapter_length_tier(tier)
        identity = self._identity(provider, model, prompt_version)
        effective_timestamp = (
            self._parse_timestamp(timestamp) or datetime.now(timezone.utc)
        ).isoformat()
        sample = {
            "sampleId": str(uuid4()),
            "traceId": str(trace_id or ""),
            "provider": identity[0],
            "model": identity[1],
            "tier": normalized_tier,
            "promptVersion": identity[2],
            "wordCountScope": TIER_CALIBRATION_WORD_COUNT_SCOPE,
            "actualWordCount": actual,
            "tierHit": bool(tier_hit),
            "structurePassed": True,
            "machineQualityPassed": bool(machine_quality_passed),
            "attemptKind": INITIAL_TIER_ATTEMPT_KIND,
            "logicalProseCalls": max(0, int(logical_prose_calls)),
            "completionTokens": (
                max(0, int(completion_tokens))
                if completion_tokens is not None
                else None
            ),
            "durationMs": (
                max(0, int(duration_ms)) if duration_ms is not None else None
            ),
            "timestamp": effective_timestamp,
        }

        path = self.calibration_path(workspace_root)
        with self._write_lock:
            payload = self._read_payload(path)
            trace = str(trace_id or "").strip()
            if trace and any(
                isinstance(item, dict)
                and str(item.get("traceId") or "") == trace
                and str(item.get("tier") or "") == normalized_tier
                and str(item.get("promptVersion") or "") == identity[2]
                for item in payload["samples"]
            ):
                return False
            payload["samples"].append(sample)
            payload["samples"] = self._trim_storage(payload["samples"])
            observation = self._compute_observation(
                payload,
                identity=identity,
                now=self._parse_timestamp(effective_timestamp)
                or datetime.now(timezone.utc),
            )
            self._upsert_observation(payload, identity, observation)
            payload["updatedAt"] = datetime.now(timezone.utc).isoformat()
            self._write_payload(path, payload)
        return True

    def read_summary(
        self,
        workspace_root: Path,
        *,
        provider: str,
        model: str,
        prompt_version: str = STORY_LENGTH_TIER_PROMPT_VERSION,
        now: str = "",
    ) -> dict[str, object]:
        payload = self._read_payload(self.calibration_path(workspace_root))
        identity = self._identity(provider, model, prompt_version)
        observation = self._compute_observation(
            payload,
            identity=identity,
            now=self._parse_timestamp(now) or datetime.now(timezone.utc),
        )
        return {
            **observation,
            "provider": identity[0],
            "model": identity[1],
            "promptVersion": identity[2],
            "calibrationVersion": self._stored_calibration_version(
                payload,
                identity,
            ),
        }

    def _compute_observation(
        self,
        payload: dict[str, Any],
        *,
        identity: tuple[str, str, str],
        now: datetime,
    ) -> dict[str, Any]:
        oldest = now - timedelta(days=MAX_TIER_CALIBRATION_SAMPLE_AGE_DAYS)
        values: dict[str, list[tuple[datetime, int]]] = {
            tier: [] for tier in CHAPTER_LENGTH_TIERS
        }
        for raw in payload.get("samples", []):
            if not isinstance(raw, dict):
                continue
            if self._sample_identity(raw) != identity:
                continue
            if str(raw.get("attemptKind") or "") != INITIAL_TIER_ATTEMPT_KIND:
                continue
            if (
                str(raw.get("wordCountScope") or "").strip().lower()
                != TIER_CALIBRATION_WORD_COUNT_SCOPE
            ):
                continue
            if not bool(raw.get("structurePassed")) or not bool(
                raw.get("machineQualityPassed")
            ):
                continue
            sample_time = self._parse_timestamp(raw.get("timestamp"))
            if sample_time is None or sample_time < oldest or sample_time > now:
                continue
            tier = str(raw.get("tier") or "")
            if tier not in values:
                continue
            try:
                actual = int(raw.get("actualWordCount"))
            except (TypeError, ValueError):
                continue
            if actual > 0:
                values[tier].append((sample_time, actual))

        recent: dict[str, list[int]] = {}
        for tier, timed in values.items():
            timed.sort(key=lambda item: item[0], reverse=True)
            recent[tier] = [
                value
                for _, value in timed[:MAX_RECENT_TIER_CALIBRATION_SAMPLES]
            ]
        counts = {tier: len(recent[tier]) for tier in CHAPTER_LENGTH_TIERS}
        medians = {
            tier: (int(median(recent[tier])) if recent[tier] else None)
            for tier in CHAPTER_LENGTH_TIERS
        }
        enough = all(
            counts[tier] >= MIN_TIER_CALIBRATION_SAMPLES
            for tier in CHAPTER_LENGTH_TIERS
        )
        separated = bool(
            enough
            and int(medians["short"] or 0) < int(medians["medium"] or 0)
            < int(medians["long"] or 0)
        )
        if not enough:
            status = "cold_start"
            reason = "insufficient_samples"
        elif not separated:
            status = "tiers_not_separated"
            reason = "median_order_invalid"
        else:
            status = "applied"
            reason = "p10_p90_observed"
        bands: dict[str, list[int]] = {}
        for tier in CHAPTER_LENGTH_TIERS:
            if status == "applied":
                ordered = sorted(recent[tier])
                lower = self._percentile(ordered, 0.10)
                upper = self._percentile(ordered, 0.90)
                bands[tier] = [
                    int(math.floor(lower / 100.0) * 100),
                    int(math.ceil(upper / 100.0) * 100),
                ]
            else:
                fixed = STORY_LENGTH_TIER_POLICIES[tier]
                bands[tier] = [
                    fixed["preferredMinimum"],
                    fixed["preferredMaximum"],
                ]
        return {
            "status": status,
            "reason": reason,
            "sampleCounts": counts,
            "medians": medians,
            "bands": bands,
        }

    def _upsert_observation(
        self,
        payload: dict[str, Any],
        identity: tuple[str, str, str],
        observation: dict[str, Any],
    ) -> None:
        observations = payload.setdefault("observations", [])
        existing = next(
            (
                item
                for item in observations
                if isinstance(item, dict)
                and self._sample_identity(item) == identity
                and str(item.get("wordCountScope") or "").strip().lower()
                == TIER_CALIBRATION_WORD_COUNT_SCOPE
            ),
            None,
        )
        signature = {
            "status": observation["status"],
            "bands": observation["bands"],
        }
        if existing is None:
            existing = {
                "provider": identity[0],
                "model": identity[1],
                "promptVersion": identity[2],
                "wordCountScope": TIER_CALIBRATION_WORD_COUNT_SCOPE,
                "calibrationVersion": 1,
            }
            observations.append(existing)
        elif {
            "status": existing.get("status"),
            "bands": existing.get("bands"),
        } != signature:
            existing["calibrationVersion"] = int(
                existing.get("calibrationVersion") or 0
            ) + 1
        existing.update(observation)
        existing["updatedAt"] = datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _percentile(values: list[int], ratio: float) -> float:
        if not values:
            return 0.0
        if len(values) == 1:
            return float(values[0])
        position = (len(values) - 1) * max(0.0, min(1.0, ratio))
        lower = int(math.floor(position))
        upper = int(math.ceil(position))
        if lower == upper:
            return float(values[lower])
        fraction = position - lower
        return values[lower] + (values[upper] - values[lower]) * fraction

    @staticmethod
    def _identity(
        provider: object,
        model: object,
        prompt_version: object,
    ) -> tuple[str, str, str]:
        return (
            str(provider or "unknown").strip().upper() or "UNKNOWN",
            str(model or "unknown").strip().lower() or "unknown",
            str(prompt_version or STORY_LENGTH_TIER_PROMPT_VERSION).strip()
            or STORY_LENGTH_TIER_PROMPT_VERSION,
        )

    def _sample_identity(self, value: dict[str, Any]) -> tuple[str, str, str]:
        return self._identity(
            value.get("provider"),
            value.get("model"),
            value.get("promptVersion"),
        )

    def _stored_calibration_version(
        self,
        payload: dict[str, Any],
        identity: tuple[str, str, str],
    ) -> int:
        for item in payload.get("observations", []):
            if (
                isinstance(item, dict)
                and self._sample_identity(item) == identity
                and str(item.get("wordCountScope") or "").strip().lower()
                == TIER_CALIBRATION_WORD_COUNT_SCOPE
            ):
                return max(0, int(item.get("calibrationVersion") or 0))
        return 0

    def _trim_storage(self, samples: list[Any]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
        for raw in samples:
            if not isinstance(raw, dict):
                continue
            key = (
                *self._sample_identity(raw),
                str(raw.get("wordCountScope") or "").strip().lower(),
                str(raw.get("tier") or ""),
            )
            grouped.setdefault(key, []).append(raw)
        kept: list[dict[str, Any]] = []
        for group in grouped.values():
            group.sort(
                key=lambda item: str(item.get("timestamp") or ""),
                reverse=True,
            )
            kept.extend(group[: max(60, MAX_RECENT_TIER_CALIBRATION_SAMPLES * 2)])
        kept.sort(key=lambda item: str(item.get("timestamp") or ""))
        return kept

    @staticmethod
    def _empty_payload() -> dict[str, Any]:
        return {
            "_type": "StoryLengthTierCalibration",
            "_version": LENGTH_TIER_CALIBRATION_VERSION,
            "samples": [],
            "observations": [],
            "updatedAt": "",
        }

    def _read_payload(self, path: Path) -> dict[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return self._empty_payload()
        if not isinstance(raw, dict):
            return self._empty_payload()
        payload = self._empty_payload()
        payload.update(raw)
        payload["_version"] = LENGTH_TIER_CALIBRATION_VERSION
        payload["samples"] = (
            list(raw.get("samples")) if isinstance(raw.get("samples"), list) else []
        )
        payload["observations"] = [
            item
            for item in (
                list(raw.get("observations"))
                if isinstance(raw.get("observations"), list)
                else []
            )
            if isinstance(item, dict)
            and str(item.get("wordCountScope") or "").strip().lower()
            == TIER_CALIBRATION_WORD_COUNT_SCOPE
        ]
        return payload

    @staticmethod
    def _write_payload(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    @staticmethod
    def _parse_timestamp(value: object) -> datetime | None:
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


_SERVICE: StoryLengthTierCalibrationService | None = None


def get_story_length_tier_calibration_service() -> StoryLengthTierCalibrationService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = StoryLengthTierCalibrationService()
    return _SERVICE
