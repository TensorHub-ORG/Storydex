from __future__ import annotations

from services import coomi_version_service as version_service


def test_reads_workspace_version_from_vendored_manifest() -> None:
    assert version_service.read_expected_coomi_version() == "2.1.0-storydex-desktop.1"


def test_missing_manifest_and_binary_are_reported(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(version_service, "DESKTOP_RUNTIME_ROOT", tmp_path / "missing")
    monkeypatch.setattr(
        version_service,
        "_installed_bridge_identity",
        lambda: (_ for _ in ()).throw(RuntimeError("missing binary")),
    )
    status = version_service.check_coomi_version()
    assert status["ok"] is False
    assert status["expected"] == version_service.STORYDEX_COOMI_RUNTIME_VERSION
    assert len(status["warnings"]) == 2


def test_packaged_runtime_uses_embedded_expected_version(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(version_service, "DESKTOP_RUNTIME_ROOT", tmp_path / "missing")
    monkeypatch.setattr(
        version_service,
        "_expected_build_identity",
        lambda: {"gitSha": "abc123", "sourceFingerprint": "source123"},
    )
    monkeypatch.setattr(
        version_service,
        "_installed_bridge_identity",
        lambda: (
            {
                "version": version_service.STORYDEX_COOMI_RUNTIME_VERSION,
                "gitSha": "abc123",
                "sourceFingerprint": "source123",
            },
            "bridge",
        ),
    )
    status = version_service.check_coomi_version()
    assert status["ok"] is True
    assert status["expected"] == version_service.STORYDEX_COOMI_RUNTIME_VERSION


def test_binary_version_mismatch_is_reported(monkeypatch) -> None:
    expected = version_service._expected_build_identity()
    monkeypatch.setattr(
        version_service,
        "_installed_bridge_identity",
        lambda: (
            {
                "version": "0.0.0",
                "gitSha": expected["gitSha"],
                "sourceFingerprint": expected["sourceFingerprint"],
            },
            "bridge",
        ),
    )
    status = version_service.check_coomi_version()
    assert status["ok"] is False
    assert "0.0.0" in status["warnings"][0]


def test_binary_fingerprint_mismatch_is_reported(monkeypatch) -> None:
    expected = version_service._expected_build_identity()
    monkeypatch.setattr(
        version_service,
        "_installed_bridge_identity",
        lambda: (
            {
                "version": version_service.STORYDEX_COOMI_RUNTIME_VERSION,
                "gitSha": expected["gitSha"],
                "sourceFingerprint": "stale-source",
            },
            "bridge",
        ),
    )
    status = version_service.check_coomi_version()
    assert status["ok"] is False
    assert "source fingerprint" in status["warnings"][0]


def test_binary_git_sha_mismatch_is_reported(monkeypatch) -> None:
    expected = version_service._expected_build_identity()
    monkeypatch.setattr(
        version_service,
        "_installed_bridge_identity",
        lambda: (
            {
                "version": version_service.STORYDEX_COOMI_RUNTIME_VERSION,
                "gitSha": "stale-commit",
                "sourceFingerprint": expected["sourceFingerprint"],
            },
            "bridge",
        ),
    )
    status = version_service.check_coomi_version()
    assert status["ok"] is False
    assert "Git SHA" in status["warnings"][0]
