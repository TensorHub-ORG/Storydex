from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.story_length_calibration_service import StoryLengthCalibrationService


def test_generation_guidance_falls_back_to_product_target_without_samples(tmp_path: Path) -> None:
    service = StoryLengthCalibrationService()

    guidance = service.resolve_generation_guidance(
        tmp_path,
        product_target_word_count=3000,
        provider="chy",
        model="deepseek-v4-flash",
        now="2026-07-26T00:00:00+00:00",
    )

    assert guidance["productTargetWordCount"] == 3000
    assert guidance["acceptanceMinimum"] == 2100
    assert guidance["acceptanceMaximum"] == 3900
    assert guidance["modelReferenceWordCount"] == 3000
    assert guidance["calibration"]["status"] == "fallback"
    assert guidance["calibration"]["reason"] == "insufficient_samples"
    assert guidance["calibration"]["sampleCount"] == 0


@pytest.mark.parametrize(
    ("target", "minimum", "maximum"),
    [
        (1500, 1050, 1950),
        (3000, 2100, 3900),
        (5000, 3500, 6500),
    ],
)
def test_product_acceptance_band_scales_with_target(
    tmp_path: Path,
    target: int,
    minimum: int,
    maximum: int,
) -> None:
    guidance = StoryLengthCalibrationService().resolve_generation_guidance(
        tmp_path,
        product_target_word_count=target,
        provider="chy",
        model="deepseek-v4-flash",
    )

    assert guidance["acceptanceMinimum"] == minimum
    assert guidance["acceptanceMaximum"] == maximum
    assert guidance["modelReferenceWordCount"] == target


def test_three_same_grade_samples_adjust_only_the_model_reference(tmp_path: Path) -> None:
    service = StoryLengthCalibrationService()
    contract = {
        "turnPlan": {
            "chapterWordCountTarget": 3000,
            "wordCountPolicy": {
                "scope": "chapter",
                "target": 3000,
                "modelReferenceWordCount": 3000,
            },
        }
    }
    for actual_word_count in (3600, 3750, 3900):
        assert service.record_generation_result(
            tmp_path,
            turn_contract=contract,
            validation={
                "applicable": True,
                "passed": True,
                "chapterWordCountTarget": 3000,
                "generatedWordCount": actual_word_count,
            },
            provider="chy",
            model="deepseek-v4-flash",
        )

    guidance = service.resolve_generation_guidance(
        tmp_path,
        product_target_word_count=3000,
        provider="chy",
        model="deepseek-v4-flash",
    )

    assert guidance["productTargetWordCount"] == 3000
    assert guidance["acceptanceMinimum"] == 2100
    assert guidance["acceptanceMaximum"] == 3900
    assert guidance["modelReferenceWordCount"] == 2400
    assert guidance["calibration"]["status"] == "applied"
    assert guidance["calibration"]["reason"] == "same_target_grade"
    assert guidance["calibration"]["sampleCount"] == 3
    assert guidance["calibration"]["medianRatio"] == pytest.approx(1.25)
    assert guidance["calibration"]["appliedRatio"] == pytest.approx(1.25)


def test_product_target_selects_the_bucket_while_model_reference_measures_response(
    tmp_path: Path,
) -> None:
    service = StoryLengthCalibrationService()
    contract = {
        "turnPlan": {
            "chapterWordCountTarget": 3000,
            "wordCountPolicy": {
                "scope": "chapter",
                "target": 3000,
                "modelReferenceWordCount": 2400,
            },
        }
    }
    for _ in range(3):
        assert service.record_generation_result(
            tmp_path,
            turn_contract=contract,
            validation={
                "applicable": True,
                "passed": True,
                "chapterWordCountTarget": 3000,
                "generatedWordCount": 3000,
            },
            provider="chy",
            model="deepseek-v4-flash",
        )

    guidance = service.resolve_generation_guidance(
        tmp_path,
        product_target_word_count=3000,
        provider="chy",
        model="deepseek-v4-flash",
    )

    assert guidance["modelReferenceWordCount"] == 2400
    assert guidance["calibration"]["medianRatio"] == pytest.approx(1.25)


def test_extreme_response_ratios_are_excluded_from_generation_guidance(tmp_path: Path) -> None:
    service = StoryLengthCalibrationService()
    contract = {
        "turnPlan": {
            "chapterWordCountTarget": 2500,
            "wordCountPolicy": {
                "scope": "chapter",
                "target": 2500,
                "modelReferenceWordCount": 2500,
            },
        }
    }
    for _ in range(3):
        assert service.record_generation_result(
            tmp_path,
            turn_contract=contract,
            validation={
                "applicable": True,
                "passed": True,
                "chapterWordCountTarget": 2500,
                "generatedWordCount": 20000,
            },
            provider="chy",
            model="deepseek-v4-flash",
        )

    guidance = service.resolve_generation_guidance(
        tmp_path,
        product_target_word_count=2500,
        provider="chy",
        model="deepseek-v4-flash",
    )

    assert guidance["modelReferenceWordCount"] == 2500
    assert guidance["calibration"]["status"] == "fallback"
    assert guidance["calibration"]["reason"] == "insufficient_samples"
    assert guidance["calibration"]["sampleCount"] == 0


@pytest.mark.parametrize(
    ("actual_word_count", "expected_reference"),
    [
        (750, 3900),
        (12000, 2100),
    ],
)
def test_calibrated_reference_stays_inside_the_product_acceptance_band(
    tmp_path: Path,
    actual_word_count: int,
    expected_reference: int,
) -> None:
    service = StoryLengthCalibrationService()
    for _ in range(3):
        assert service.append_sample(
            tmp_path,
            product_target_word_count=3000,
            model_reference_word_count=3000,
            actual_word_count=actual_word_count,
            provider="chy",
            model="deepseek-v4-flash",
        )

    guidance = service.resolve_generation_guidance(
        tmp_path,
        product_target_word_count=3000,
        provider="chy",
        model="deepseek-v4-flash",
    )

    assert guidance["calibration"]["status"] == "applied"
    assert guidance["modelReferenceWordCount"] == expected_reference
    assert 2100 <= guidance["modelReferenceWordCount"] <= 3900


def test_generation_guidance_ignores_samples_older_than_ninety_days(tmp_path: Path) -> None:
    service = StoryLengthCalibrationService()
    for timestamp in (
        "2026-01-01T00:00:00+00:00",
        "2026-01-02T00:00:00+00:00",
        "2026-01-03T00:00:00+00:00",
    ):
        assert service.append_sample(
            tmp_path,
            product_target_word_count=3000,
            model_reference_word_count=3000,
            actual_word_count=2400,
            provider="chy",
            model="deepseek-v4-flash",
            timestamp=timestamp,
        )
    for timestamp in (
        "2026-05-01T00:00:00+00:00",
        "2026-06-01T00:00:00+00:00",
        "2026-07-01T00:00:00+00:00",
    ):
        assert service.append_sample(
            tmp_path,
            product_target_word_count=3000,
            model_reference_word_count=3000,
            actual_word_count=3600,
            provider="chy",
            model="deepseek-v4-flash",
            timestamp=timestamp,
        )

    guidance = service.resolve_generation_guidance(
        tmp_path,
        product_target_word_count=3000,
        provider="chy",
        model="deepseek-v4-flash",
        now="2026-07-26T00:00:00+00:00",
    )

    assert guidance["modelReferenceWordCount"] == 2500
    assert guidance["calibration"]["sampleCount"] == 3
    assert guidance["calibration"]["medianRatio"] == pytest.approx(1.2)


def test_generation_guidance_uses_only_the_twenty_most_recent_samples(tmp_path: Path) -> None:
    service = StoryLengthCalibrationService()
    for day in range(1, 21):
        assert service.append_sample(
            tmp_path,
            product_target_word_count=3000,
            model_reference_word_count=3000,
            actual_word_count=2400,
            provider="chy",
            model="deepseek-v4-flash",
            timestamp=f"2026-06-{day:02d}T00:00:00+00:00",
        )
    for day in range(1, 21):
        assert service.append_sample(
            tmp_path,
            product_target_word_count=3000,
            model_reference_word_count=3000,
            actual_word_count=3600,
            provider="chy",
            model="deepseek-v4-flash",
            timestamp=f"2026-07-{day:02d}T00:00:00+00:00",
        )

    guidance = service.resolve_generation_guidance(
        tmp_path,
        product_target_word_count=3000,
        provider="chy",
        model="deepseek-v4-flash",
        now="2026-07-26T00:00:00+00:00",
    )

    assert guidance["modelReferenceWordCount"] == 2500
    assert guidance["calibration"]["sampleCount"] == 20
    assert guidance["calibration"]["medianRatio"] == pytest.approx(1.2)


def test_nearby_target_grade_is_a_half_strength_fallback_after_five_samples(
    tmp_path: Path,
) -> None:
    service = StoryLengthCalibrationService()
    for _ in range(5):
        assert service.append_sample(
            tmp_path,
            product_target_word_count=3000,
            model_reference_word_count=3000,
            actual_word_count=3600,
            provider="chy",
            model="deepseek-v4-flash",
        )

    guidance = service.resolve_generation_guidance(
        tmp_path,
        product_target_word_count=3500,
        provider="chy",
        model="deepseek-v4-flash",
    )

    assert guidance["productTargetWordCount"] == 3500
    assert guidance["modelReferenceWordCount"] == 3182
    assert guidance["calibration"]["status"] == "applied"
    assert guidance["calibration"]["reason"] == "nearby_target_grade"
    assert guidance["calibration"]["sourceTargetGrade"] == 3000
    assert guidance["calibration"]["sampleCount"] == 5
    assert guidance["calibration"]["medianRatio"] == pytest.approx(1.2)
    assert guidance["calibration"]["appliedRatio"] == pytest.approx(1.1)


def test_missing_provider_or_model_identity_never_uses_a_shared_unknown_bucket(
    tmp_path: Path,
) -> None:
    service = StoryLengthCalibrationService()
    contract = {
        "turnPlan": {
            "chapterWordCountTarget": 3000,
            "wordCountPolicy": {
                "scope": "chapter",
                "target": 3000,
                "modelReferenceWordCount": 3000,
            },
        }
    }

    assert service.record_generation_result(
        tmp_path,
        turn_contract=contract,
        validation={
            "applicable": True,
            "passed": True,
            "chapterWordCountTarget": 3000,
            "generatedWordCount": 3600,
        },
        provider="",
        model="",
    ) is False

    guidance = service.resolve_generation_guidance(
        tmp_path,
        product_target_word_count=3000,
        provider="",
        model="",
    )
    assert guidance["modelReferenceWordCount"] == 3000
    assert guidance["calibration"]["status"] == "fallback"
    assert guidance["calibration"]["reason"] == "model_identity_unavailable"
    assert guidance["calibration"]["sampleCount"] == 0
    assert service.append_sample(
        tmp_path,
        product_target_word_count=3000,
        model_reference_word_count=3000,
        actual_word_count=3000,
        provider="",
        model="",
    ) is False
    assert service.calibration_path(tmp_path).exists() is False


def test_legacy_unknown_bucket_is_not_read_by_diagnostic_ratio(tmp_path: Path) -> None:
    service = StoryLengthCalibrationService()
    path = service.calibration_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "_type": "StoryLengthCalibration",
                "_version": 1,
                "lengthGradeSize": 500,
                "buckets": [
                    {
                        "provider": "unknown",
                        "model": "unknown",
                        "lengthGrade": 3000,
                        "samples": [
                            {
                                "referenceWordCount": 3000,
                                "actualWordCount": 3600,
                                "timestamp": "2026-07-26T00:00:00+00:00",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert service.median_ratio(
        tmp_path,
        reference_word_count=3000,
        provider="",
        model="",
    ) is None


def test_each_calibration_bucket_keeps_only_the_twenty_most_recent_samples(
    tmp_path: Path,
) -> None:
    service = StoryLengthCalibrationService()
    for hour in range(25):
        assert service.append_sample(
            tmp_path,
            product_target_word_count=3000,
            model_reference_word_count=3000,
            actual_word_count=3000 + hour,
            provider="chy",
            model="deepseek-v4-flash",
            timestamp=f"2026-07-25T{hour % 24:02d}:00:00+00:00"
            if hour < 24
            else "2026-07-26T00:00:00+00:00",
        )

    payload = json.loads(service.calibration_path(tmp_path).read_text(encoding="utf-8"))
    samples = payload["buckets"][0]["samples"]
    assert len(samples) == 20
    assert samples[0]["actualWordCount"] == 3005
    assert samples[-1]["actualWordCount"] == 3024


def test_new_calibration_files_persist_product_target_and_model_reference_separately(
    tmp_path: Path,
) -> None:
    service = StoryLengthCalibrationService()
    assert service.append_sample(
        tmp_path,
        product_target_word_count=3000,
        model_reference_word_count=2400,
        actual_word_count=3000,
        provider="chy",
        model="deepseek-v4-flash",
        timestamp="2026-07-26T00:00:00+00:00",
    )

    payload = json.loads(service.calibration_path(tmp_path).read_text(encoding="utf-8"))
    assert payload["_version"] == 2
    assert payload["targetGradeSize"] == 500
    assert payload["buckets"] == [
        {
            "provider": "chy",
            "model": "deepseek-v4-flash",
            "targetGrade": 3000,
            "samples": [
                {
                    "productTargetWordCount": 3000,
                    "modelReferenceWordCount": 2400,
                    "actualWordCount": 3000,
                    "provider": "chy",
                    "model": "deepseek-v4-flash",
                    "timestamp": "2026-07-26T00:00:00+00:00",
                }
            ],
        }
    ]


def test_v1_calibration_is_read_and_upgraded_on_the_next_write(tmp_path: Path) -> None:
    service = StoryLengthCalibrationService()
    path = service.calibration_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy_samples = [
        {
            "referenceWordCount": 3000,
            "actualWordCount": 3600,
            "provider": "chy",
            "model": "deepseek-v4-flash",
            "timestamp": f"2026-07-2{day}T00:00:00+00:00",
        }
        for day in range(3, 6)
    ]
    path.write_text(
        json.dumps(
            {
                "_type": "StoryLengthCalibration",
                "_version": 1,
                "lengthGradeSize": 500,
                "buckets": [
                    {
                        "provider": "chy",
                        "model": "deepseek-v4-flash",
                        "lengthGrade": 3000,
                        "samples": legacy_samples,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    guidance = service.resolve_generation_guidance(
        tmp_path,
        product_target_word_count=3000,
        provider="chy",
        model="deepseek-v4-flash",
        now="2026-07-26T00:00:00+00:00",
    )
    assert guidance["modelReferenceWordCount"] == 2500
    assert guidance["calibration"]["sampleCount"] == 3

    assert service.append_sample(
        tmp_path,
        product_target_word_count=3000,
        model_reference_word_count=2500,
        actual_word_count=3000,
        provider="chy",
        model="deepseek-v4-flash",
        timestamp="2026-07-26T00:00:00+00:00",
    )
    upgraded = json.loads(path.read_text(encoding="utf-8"))
    assert upgraded["_version"] == 2
    assert upgraded["targetGradeSize"] == 500
    assert "lengthGradeSize" not in upgraded
    bucket = upgraded["buckets"][0]
    assert bucket["targetGrade"] == 3000
    assert "lengthGrade" not in bucket
    assert all("referenceWordCount" not in sample for sample in bucket["samples"])
    assert all(
        {"productTargetWordCount", "modelReferenceWordCount"} <= sample.keys()
        for sample in bucket["samples"]
    )


def test_samples_are_bucketed_by_provider_model_and_product_target_grade(tmp_path: Path) -> None:
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
    assert payload["targetGradeSize"] == 500
    assert len(payload["buckets"]) == 1
    bucket = payload["buckets"][0]
    assert bucket["provider"] == "openai"
    assert bucket["model"] == "gpt-test"
    assert bucket["targetGrade"] == 2500
    assert [sample["productTargetWordCount"] for sample in bucket["samples"]] == [2400, 2500, 2600]
    assert bucket["samples"][0] == {
        "productTargetWordCount": 2400,
        "modelReferenceWordCount": 2400,
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


def test_different_model_strings_never_share_generation_guidance(tmp_path: Path) -> None:
    service = StoryLengthCalibrationService()
    for _ in range(3):
        assert service.append_sample(
            tmp_path,
            product_target_word_count=3000,
            model_reference_word_count=3000,
            actual_word_count=3600,
            provider="chy",
            model="deepseek-v4-flash",
        )

    guidance = service.resolve_generation_guidance(
        tmp_path,
        product_target_word_count=3000,
        provider="chy",
        model="deepseek-v4-flash-2",
    )

    assert guidance["modelReferenceWordCount"] == 3000
    assert guidance["calibration"]["status"] == "fallback"
    assert guidance["calibration"]["reason"] == "insufficient_samples"


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
