from __future__ import annotations

from services.coomi_version_service import (
    check_coomi_version,
    read_expected_coomi_version,
)


def test_repository_pin_matches_installed_metadata_and_module():
    status = check_coomi_version()
    assert status["ok"] is True
    assert status["expected"] == status["metadataVersion"] == status["moduleVersion"]


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


def test_missing_version_source_does_not_fall_back_to_an_independent_constant(tmp_path, monkeypatch):
    monkeypatch.setattr("services.coomi_version_service.repository_root", lambda: tmp_path)
    try:
        read_expected_coomi_version()
    except FileNotFoundError as exc:
        assert str(tmp_path / "requirements.txt") in str(exc)
    else:
        raise AssertionError("missing requirements.txt must not use a fallback version")


def test_version_check_exposes_missing_version_source_as_warning(tmp_path, monkeypatch):
    monkeypatch.setattr("services.coomi_version_service.repository_root", lambda: tmp_path)
    status = check_coomi_version(metadata_version="0.1.12", module_version="0.1.12")
    assert status["ok"] is False
    assert status["expected"] == ""
    assert "requirements.txt" in status["warnings"][0]


def test_packaged_runtime_reads_manifest_copied_from_root(tmp_path, monkeypatch):
    packaged_requirements = tmp_path / "requirements-runtime.txt"
    packaged_requirements.write_text("coomi-agent==0.1.12\n", encoding="utf-8")
    monkeypatch.setattr("services.coomi_version_service.repository_root", lambda: tmp_path / "missing")
    monkeypatch.setattr(
        "services.coomi_version_service.packaged_requirements_path",
        lambda: packaged_requirements,
    )
    assert read_expected_coomi_version() == "0.1.12"


def test_rejects_duplicate_coomi_pins(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("coomi-agent==0.1.12\ncoomi-agent==0.1.13\n", encoding="utf-8")
    try:
        read_expected_coomi_version(requirements)
    except RuntimeError as exc:
        assert "exactly once" in str(exc)
    else:
        raise AssertionError("duplicate Coomi pins must fail")


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
