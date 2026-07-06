import json

from services.preset_schema import write_preset_sidecar
from services.silly_tavern_preset_importer import convert_silly_tavern_preset


def test_writing_preset_sidecar_syncs_imported_markdown_module_states(tmp_path):
    payload = {
        "prompts": [
            {
                "identifier": "disclaimer",
                "name": "（必看）免责声明",
                "role": "system",
                "content": "免责声明内容",
            }
        ],
        "prompt_order": [{"character_id": 100001, "order": [{"identifier": "disclaimer", "enabled": False}]}],
    }
    imported = convert_silly_tavern_preset(
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        filename="sample.json",
    )
    md_path = tmp_path / "sample.md"
    md_path.write_text(imported.markdown, encoding="utf-8")
    assert "- [off] （必看）免责声明 (advanced)" in md_path.read_text(encoding="utf-8")

    doc_dump = imported.document.model_dump(mode="python", by_alias=True)
    doc_dump["modules"][0]["enabledByDefault"] = True
    document = imported.document.model_validate(doc_dump)

    write_preset_sidecar(md_path, document)

    markdown = md_path.read_text(encoding="utf-8")
    assert "- [on] （必看）免责声明 (advanced)" in markdown
    assert "- [off] （必看）免责声明 (advanced)" not in markdown
