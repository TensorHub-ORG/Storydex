import json

from services.silly_tavern_preset_importer import convert_silly_tavern_preset
from services.story_project_service import StoryProjectService


def test_runtime_preset_context_compiles_silly_tavern_macros(tmp_path):
    service = StoryProjectService()
    service.ensure_project_structure(tmp_path)
    payload = {
        "prompts": [
            {
                "identifier": "seed",
                "name": "Seed",
                "role": "system",
                "content": "seed visible {{setvar::tone::月光}}",
            },
            {
                "identifier": "main",
                "name": "Main",
                "role": "system",
                "content": "Tone={{getvar::tone}}; user={{user}}; char={{char}}; {{//hidden}}",
            },
        ],
        "prompt_order": [
            {
                "character_id": 100001,
                "order": [
                    {"identifier": "seed", "enabled": True},
                    {"identifier": "main", "enabled": True},
                ],
            }
        ],
    }
    imported = convert_silly_tavern_preset(json.dumps(payload, ensure_ascii=False).encode("utf-8"), filename="runtime.json")
    active_dir = service.preset_root(tmp_path) / "active"
    active_dir.mkdir(parents=True, exist_ok=True)
    md_path = active_dir / "runtime.md"
    md_path.write_text(imported.markdown, encoding="utf-8")
    service.write_preset_sidecar(md_path, imported.document)

    context = service._build_preset_context(  # noqa: SLF001 - this is the runtime preset context path
        tmp_path,
        total_chars=5000,
        runtime_context={"user": "读者", "char": "夏瑾"},
    )

    assert "Tone=月光" in context
    assert "user=读者" in context
    assert "char=夏瑾" in context
    assert "{{getvar" not in context
    assert "hidden" not in context
