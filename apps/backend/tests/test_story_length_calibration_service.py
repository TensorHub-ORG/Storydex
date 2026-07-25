from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.story_length_calibration_service import StoryLengthCalibrationService


def test_samples_are_bucketed_by_provider_model_and_length_grade(tmp_path: Path) -> None:
    service = StoryLengthCalibrationService()

    assert service.append_sample(
        tmp_path,
        reference_word_count=2400,
        actual_word_count=1920,
        provider="openai",
        model="gpt-test",
        timestamp="2026-07-25T01:00:00+00:00",
    )
    assert service.append_sample(
        tmp_path,
        reference_word_count=2500,
        actual_word_count=2500,
        provider="openai",
        model="gpt-test",
        timestamp="2026-07-25T02:00:00+00:00",
    )
    assert service.append_sample(
        tmp_path,
        reference_word_count=2600,
        actual_word_count=3120,
        provider="openai",
        model="gpt-test",
        timestamp="2026-07-25T03:00:00+00:00",
    )

    payload = json.loads(service.calibration_path(tmp_path).read_text(encoding="utf-8"))
    assert payload["lengthGradeSize"] == 500
    assert len(payload["buckets"]) == 1
    bucket = payload["buckets"][0]
    assert bucket["provider"] == "openai"
    assert bucket["model"] == "gpt-test"
    assert bucket["lengthGrade"] == 2500
    assert [sample["referenceWordCount"] for sample in bucket["samples"]] == [2400, 2500, 2600]
    assert bucket["samples"][0] == {
        "referenceWordCount": 2400,
        "actualWordCount": 1920,
        "provider": "openai",
        "model": "gpt-test",
        "timestamp": "2026-07-25T01:00:00+00:00",
    }
    assert service.median_ratio(
        tmp_path,
        reference_word_count=2500,
        provider="openai",
        model="gpt-test",
    ) == pytest.approx(1.0)


def test_provider_model_and_length_grade_create_distinct_buckets(tmp_path: Path) -> None:
    service = StoryLengthCalibrationService()
    samples = [
        ("provider-a", "model-a", 2500),
        ("provider-b", "model-a", 2500),
        ("provider-a", "model-b", 2500),
        ("provider-a", "model-a", 3200),
    ]
    for provider, model, reference in samples:
        assert service.append_sample(
            tmp_path,
            reference_word_count=reference,
            actual_word_count=reference,
            provider=provider,
            model=model,
        )

    payload = json.loads(service.calibration_path(tmp_path).read_text(encoding="utf-8"))
    assert len(payload["buckets"]) == 4


def test_missing_or_corrupt_file_has_no_statistics_and_can_be_rebuilt(tmp_path: Path) -> None:
    service = StoryLengthCalibrationService()
    assert service.median_ratio(
        tmp_path,
        reference_word_count=2500,
        provider="provider",
        model="model",
    ) is None

    path = service.calibration_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    assert service.median_ratio(
        tmp_path,
        reference_word_count=2500,
        provider="provider",
        model="model",
    ) is None
    assert service.append_sample(
        tmp_path,
        reference_word_count=2500,
        actual_word_count=2000,
        provider="provider",
        model="model",
    )
    assert service.median_ratio(
        tmp_path,
        reference_word_count=2500,
        provider="provider",
        model="model",
    ) == pytest.approx(0.8)


def test_write_failure_is_non_fatal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    service = StoryLengthCalibrationService()

    def fail_write(_path: Path, _payload: dict) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(service, "_write_payload", fail_write)
    assert service.append_sample(
        tmp_path,
        reference_word_count=2500,
        actual_word_count=2500,
        provider="provider",
        model="model",
    ) is False


def test_generation_result_records_only_accepted_chapter_scope(tmp_path: Path) -> None:
    service = StoryLengthCalibrationService()
    contract = {
        "turnPlan": {
            "chapterWordCountTarget": 2500,
            "wordCountPolicy": {"scope": "chapter", "target": 2500},
        }
    }
    validation = {
        "applicable": True,
        "passed": True,
        "chapterWordCountTarget": 2500,
        "generatedWordCount": 2800,
    }
    assert service.record_generation_result(
        tmp_path,
        turn_contract=contract,
        validation=validation,
        provider="provider",
        model="model",
    )
    assert service.median_ratio(
        tmp_path,
        reference_word_count=2500,
        provider="provider",
        model="model",
    ) == pytest.approx(1.12)

    validation["passed"] = False
    assert service.record_generation_result(
        tmp_path,
        turn_contract=contract,
        validation=validation,
        provider="provider",
        model="model",
    ) is False
    contract["turnPlan"]["wordCountPolicy"]["scope"] = "fragment"
    validation["passed"] = True
    assert service.record_generation_result(
        tmp_path,
        turn_contract=contract,
        validation=validation,
        provider="provider",
        model="model",
    ) is False
