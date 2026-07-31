from __future__ import annotations

from services import coomi_version_service as version_service


def test_repository_manifest_matches_rust_bridge_binary() -> None:
    status = version_service.check_coomi_version()
    assert status["ok"] is True
    assert status["runtime"] == "storydex-coomi-rs"
    assert status["expected"] == status["binaryVersion"]
    assert status["executable"]


def test_reads_workspace_version_from_vendored_manifest() -> None:
    assert version_service.read_expected_coomi_version() == "2.0.0-storydex.1"


def test_missing_manifest_and_binary_are_reported(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(version_service, "VENDORED_RUNTIME_ROOT", tmp_path / "missing")
    monkeypatch.setattr(
        version_service,
        "_installed_bridge_version",
        lambda: (_ for _ in ()).throw(RuntimeError("missing binary")),
    )
    status = version_service.check_coomi_version()
    assert status["ok"] is False
    assert status["expected"] == version_service.STORYDEX_COOMI_RUNTIME_VERSION
    assert len(status["warnings"]) == 1


def test_packaged_runtime_uses_embedded_expected_version(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(version_service, "VENDORED_RUNTIME_ROOT", tmp_path / "missing")
    monkeypatch.setattr(
        version_service,
        "_installed_bridge_version",
        lambda: (version_service.STORYDEX_COOMI_RUNTIME_VERSION, "bridge"),
    )
    status = version_service.check_coomi_version()
    assert status["ok"] is True
    assert status["expected"] == version_service.STORYDEX_COOMI_RUNTIME_VERSION


def test_binary_version_mismatch_is_reported(monkeypatch) -> None:
    monkeypatch.setattr(version_service, "_installed_bridge_version", lambda: ("0.0.0", "bridge"))
    status = version_service.check_coomi_version()
    assert status["ok"] is False
    assert "0.0.0" in status["warnings"][0]
