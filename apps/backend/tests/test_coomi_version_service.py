from __future__ import annotations

from services.coomi_version_service import (
    SUPPORTED_COOMI_VERSION,
    check_coomi_version,
    read_expected_coomi_version,
)


def test_repository_pin_matches_supported_version():
    assert read_expected_coomi_version() == SUPPORTED_COOMI_VERSION


def test_reads_single_pinned_coomi_version(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("fastapi==1.0\ncoomi-agent==0.1.12\n", encoding="utf-8")
    assert read_expected_coomi_version(requirements) == "0.1.12"


def test_rejects_unpinned_coomi_requirement(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("coomi-agent\n", encoding="utf-8")
    try:
        read_expected_coomi_version(requirements)
    except RuntimeError as exc:
        assert "must pin" in str(exc)
    else:
        raise AssertionError("expected unpinned requirement to fail")


def test_packaged_runtime_uses_supported_version_when_requirements_are_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("services.coomi_version_service.repository_root", lambda: tmp_path)
    assert read_expected_coomi_version() == SUPPORTED_COOMI_VERSION


def test_version_check_reports_metadata_and_module_mismatch(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("coomi-agent==0.1.12\n", encoding="utf-8")
    status = check_coomi_version(
        requirements_path=requirements,
        metadata_version="0.1.11",
        module_version="0.1.10",
    )
    assert status["ok"] is False
    assert status["metadataVersion"] == "0.1.11"
    assert len(status["warnings"]) == 2


def test_version_check_accepts_exact_match(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("coomi-agent==0.1.12\n", encoding="utf-8")
    status = check_coomi_version(
        requirements_path=requirements,
        metadata_version="0.1.12",
        module_version="0.1.12",
    )
    assert status["ok"] is True
    assert status["warnings"] == []
