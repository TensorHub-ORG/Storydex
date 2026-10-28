import pytest

from services.preset_compiler import compile_preset
from services.preset_schema import PresetDocument


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
