from __future__ import annotations

import base64
import json
import types

import pytest
from fastapi import HTTPException

from api import routes_presets as routes
from core.exceptions import StorydexError
from services.preset_schema import PresetDocument


class Request:
    headers = {"x-trace-id": "trace"}


def test_path_trace_workspace_deep_merge_decode_unique_and_import_item(monkeypatch, tmp_path):
    monkeypatch.setattr(routes, "project_service", types.SimpleNamespace(current_project=lambda: {"workspaceRoot": str(tmp_path)}))
    assert routes._workspace_root() == tmp_path.resolve()
    assert routes._trace(Request()).trace_id == "trace"
    presets = tmp_path / ".storydex/presets"
    active = presets / "active/a.md"
    library = presets / "library/b.md"
    active.parent.mkdir(parents=True)
    library.parent.mkdir(parents=True)
    active.write_text("a", encoding="utf-8")
    library.write_text("b", encoding="utf-8")
    assert routes._resolve_safe_md_path(tmp_path, "a") == active.resolve()
    assert routes._resolve_safe_md_path(tmp_path, ".storydex/presets/library/b.md") == library.resolve()
    with pytest.raises(HTTPException):
        routes._resolve_safe_md_path(tmp_path, "")
    with pytest.raises(HTTPException):
        routes._resolve_safe_md_path(tmp_path, "../bad")
    with pytest.raises(HTTPException):
        routes._resolve_safe_md_path(tmp_path, "missing")
    with pytest.raises(HTTPException):
        routes._resolve_safe_md_path(tmp_path, "outside.md")
    with pytest.raises(HTTPException):
        routes._resolve_safe_md_path(tmp_path, ".storydex/presets/library/b.json")

    target = {"a": {"b": 1}, "x": 1}
    routes._deep_merge(target, {"a": {"c": 2}, "x": 3})
    assert target == {"a": {"b": 1, "c": 2}, "x": 3}
    assert routes._decode_base64_file(base64.b64encode(b"data").decode()) == b"data"
    assert routes._decode_base64_file("data:text/plain;base64," + base64.b64encode(b"x").decode()) == b"x"
    with pytest.raises(HTTPException):
        routes._decode_base64_file("bad!")
    assert routes._unique_library_stem(library.parent, "b") == "b-1"

    converted = types.SimpleNamespace(
        title="Title", module_count=1, filtered_count=1,
        filtered_blocks=[types.SimpleNamespace(name="n", identifier="i", reason="r")],
        warnings=["w"], import_warnings=["iw"], display_regexes=[], chat_squash_meta={},
        document=PresetDocument(),
    )
    preview = routes._build_import_item("x.json", converted, None, None, None, preview=True)
    assert preview["relativePath"] == "" and preview["filteredBlocks"][0]["reason"] == "r"
    imported = routes._build_import_item("x.json", converted, tmp_path, library, library.with_name("b.preset.json"))
    assert imported["relativePath"].endswith("b.md")


def test_preset_route_lifecycle_compile_import_preview_and_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(routes, "project_service", types.SimpleNamespace(current_project=lambda: {"workspaceRoot": str(tmp_path)}))
    preset_root = tmp_path / ".storydex/presets"
    active = preset_root / "active/a.md"
    library = preset_root / "library/b.md"
    active.parent.mkdir(parents=True)
    library.parent.mkdir(parents=True)
    active.write_text("# A", encoding="utf-8")
    library.write_text("# B", encoding="utf-8")

    docs = {active.resolve(): PresetDocument(), library.resolve(): PresetDocument()}

    class Service:
        def list_presets(self, root):
            return {"active": [{"name": "a"}], "library": [{"name": "b"}]}

        def read_active_pointer(self, root):
            return {"activeMainPreset": ".storydex/presets/active/a.md"}

        def preset_root(self, root):
            return preset_root

        def write_preset_sidecar(self, md_path, document):
            docs[md_path.resolve()] = document
            md_path.with_name(md_path.stem + ".preset.json").write_text(document.model_dump_json(), encoding="utf-8")

        def load_preset_sidecar(self, md_path):
            return docs.get(md_path.resolve())

        def activate_preset(self, root, rel):
            if "bad" in rel:
                raise StorydexError("bad")
            return {"activeMainPreset": rel}

        def deactivate_preset(self, root, rel):
            return {"deactivated": rel}

    service = Service()
    monkeypatch.setattr(routes, "story_project_service", service)
    monkeypatch.setattr(routes, "load_preset_sidecar", lambda path: (PresetDocument(), ["loaded warning"]))
    monkeypatch.setattr(routes, "compile_preset", lambda document, overrides=None: types.SimpleNamespace(
        model_dump=lambda **kwargs: {"prompt": "compiled", "warnings": ["compile warning"], "risks": []}
    ))

    converted = types.SimpleNamespace(
        title="Imported", markdown="# Imported", document=PresetDocument(), module_count=0, filtered_count=0,
        filtered_blocks=[], warnings=[], import_warnings=[], display_regexes=[], chat_squash_meta={},
    )
    monkeypatch.setattr(routes, "convert_silly_tavern_preset", lambda raw, filename: converted)
    monkeypatch.setattr(routes, "safe_preset_filename_stem", lambda title, fallback="preset": "imported")
    request = Request()

    listing = routes.list_presets(request)
    assert listing["data"]["activeMainPreset"].endswith("a.md")
    encoded = base64.b64encode(b"{}").decode()
    payload = routes.SillyTavernPresetImportRequest(files=[{"name": "x.json", "contentBase64": encoded}])
    imported = routes.import_silly_tavern_presets(payload, request)
    assert imported["data"]["items"][0]["relativePath"]
    preview = routes.preview_silly_tavern_import(payload, request)
    assert preview["data"]["items"][0]["relativePath"] == ""
    assert routes.get_preset_schema(request)["data"]["version"] == 1
    assert routes.get_active_preset(request)["data"]["activeMainPreset"]
    assert routes.get_preset_document("a", request)["data"]["document"]

    sidecar = active.with_name("a.preset.json")
    sidecar.write_text("{}", encoding="utf-8")
    assert routes.get_preset_document("a", request)["data"]["warnings"] == ["loaded warning"]
    compiled = routes.compile_preset_document("a", request)
    assert compiled["data"]["warnings"] == ["loaded warning", "compile warning"]
    explicit = routes.compile_preset_document("a", request, routes.PresetCompileRequest(document=PresetDocument(), presetOverrides={"x": 1}))
    assert explicit["data"]["prompt"] == "compiled"
    assert routes.risk_check_preset_document("a", request)["data"]["risks"] == []
    assert routes.put_preset_document("a", PresetDocument(), request)["data"]["ok"] is True
    assert routes.patch_preset_params("a", {"sampling": {"temperature": 0.5}}, request)["data"]["ok"] is True
    with pytest.raises(HTTPException):
        routes.patch_preset_params("a", {"version": {}}, request)
    assert routes.activate_preset("a", request)["data"]["activeMainPreset"].endswith("a.md")
    assert routes.deactivate_preset("a", request)["data"]["deactivated"].endswith("a.md")

    bad = preset_root / "active/bad.md"
    bad.write_text("bad", encoding="utf-8")
    with pytest.raises(HTTPException):
        routes.activate_preset("bad", request)
