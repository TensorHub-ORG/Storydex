import pytest

from services.preset_compiler import compile_preset
from services.preset_schema import PresetDocument
from services.writing_guidance import effective_writing_guidance
from services.story_project_service import StoryProjectService


@pytest.mark.parametrize("imported", [False, True])
def test_optional_guidance_preserves_existing_preset_and_round_trips(imported):
    data = {"style": {"freeTextSlotPre": "ORIGINAL STYLE"}, "guidanceModules": [
        {"id": "writing_expression", "content": "NATURAL PROSE", "enabledByDefault": True},
        {"id": "writing_scene_focus", "content": "DISABLED ADVICE", "enabledByDefault": False},
    ]}
    if imported:
        data["meta"] = {"sourceFormat": "sillytavern"}
        data["modules"] = [{"id": "original", "content": "ORIGINAL STYLE"}]
    document = PresetDocument.model_validate(data)
    document = PresetDocument.model_validate_json(document.model_dump_json())
    text = compile_preset(document).compiled_text
    assert "ORIGINAL STYLE" in text
    assert "NATURAL PROSE" in text
    assert "DISABLED ADVICE" not in text
    document.guidance_modules[0]["enabledByDefault"] = False
    assert "NATURAL PROSE" not in compile_preset(document).compiled_text


@pytest.mark.parametrize("overrides", [None, []])
def test_existing_presets_inherit_default_guidance(overrides):
    data = {"guidanceModules": overrides} if overrides is not None else {}
    text = compile_preset(PresetDocument.model_validate(data)).compiled_text
    for module in effective_writing_guidance([]):
        assert module["content"] in text


def test_project_without_preset_receives_guidance_without_default_length_gate(tmp_path):
    context = StoryProjectService()._build_preset_context(tmp_path)
    for module in effective_writing_guidance([]):
        assert module["content"] in context
    assert "不少于" not in context


def test_all_guidance_can_be_disabled():
    overrides = [{"id": module["id"], "enabledByDefault": False} for module in effective_writing_guidance([])]
    text = compile_preset(PresetDocument(guidanceModules=overrides)).compiled_text
    for module in effective_writing_guidance([]):
        assert module["content"] not in text


def test_runtime_preserves_the_complete_guidance_after_preset_compilation(tmp_path):
    service = StoryProjectService()
    service.ensure_project_structure(tmp_path)
    markdown = service.preset_root(tmp_path) / "active" / "voice.md"
    markdown.write_text("# Voice\n", encoding="utf-8")
    document = PresetDocument(style={"freeTextSlotPre": "本书的叙述声音"})
    service.write_preset_sidecar(markdown, document)
    context = service._build_preset_context(tmp_path)
    assert "本书的叙述声音" in context
    for module in effective_writing_guidance([]):
        assert module["content"] in context


def test_runtime_does_not_reenable_explicitly_disabled_guidance(tmp_path):
    service = StoryProjectService()
    service.ensure_project_structure(tmp_path)
    markdown = service.preset_root(tmp_path) / "active" / "voice.md"
    markdown.write_text("# Voice\n", encoding="utf-8")
    document = PresetDocument(
        guidanceModules=[{"id": module["id"], "enabledByDefault": False} for module in effective_writing_guidance([])],
        lengthContract={"bodyMinChars": 0, "bodyTargetChars": 0, "bodyMaxChars": 0},
    )
    service.write_preset_sidecar(markdown, document)
    context = service._build_preset_context(tmp_path)
    for module in effective_writing_guidance([]):
        assert module["content"] not in context
