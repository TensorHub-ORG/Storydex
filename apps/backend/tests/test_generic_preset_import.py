import json

from services.preset_compiler import compile_preset
from services.silly_tavern_preset_importer import convert_silly_tavern_preset


def _import_bytes(raw: bytes, filename: str):
    return convert_silly_tavern_preset(raw, filename=filename)


def _import_json(payload, filename: str = "generic.json"):
    return _import_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), filename)


def test_plain_text_preset_imports_as_generic_and_compiles_verbatim():
    text = "全文写作规范：\n1. 第三人称过去时。\n2. 每段不超过五行。"
    result = _import_bytes(text.encode("utf-8"), "规范.txt")
    doc_dump = result.document.model_dump(mode="python", by_alias=True)

    assert doc_dump["meta"]["sourceFormat"] == "generic"
    assert result.module_count == 1
    compiled = compile_preset(result.document)
    assert compiled.compiled_text == text
    # 通用格式与 ST 一样不加 Storydex 段头。
    assert "[advanced/" not in compiled.compiled_text


def test_markdown_preset_with_macros_expands_at_runtime():
    text = "写给{{user}}的规则。\n{{// 内部备注}}\n称呼角色为{{char}}。"
    result = _import_bytes(text.encode("utf-8"), "rules.md")

    compiled = compile_preset(result.document, runtime_context={"user": "读者", "char": "夏瑾"})
    assert "写给读者的规则。" in compiled.compiled_text
    assert "称呼角色为夏瑾。" in compiled.compiled_text
    assert "内部备注" not in compiled.compiled_text


def test_json_array_of_strings_imports_each_item_as_module():
    result = _import_json(["第一条规则内容", "第二条规则内容"], "array.json")
    doc_dump = result.document.model_dump(mode="python", by_alias=True)

    assert doc_dump["meta"]["sourceFormat"] == "generic"
    assert result.module_count == 2
    assert compile_preset(result.document).compiled_text == "第一条规则内容\n\n第二条规则内容"


def test_json_array_of_prompt_objects_keeps_order_and_enabled_flags():
    payload = [
        {"name": "开头", "content": "开头规则", "role": "system"},
        {"name": "关闭项", "content": "关闭的规则", "enabled": False},
        {"name": "结尾", "content": "结尾规则"},
    ]
    result = _import_json(payload, "array-objects.json")
    modules = result.document.model_dump(mode="python", by_alias=True)["modules"]

    assert [m["title"] for m in modules] == ["开头", "关闭项", "结尾"]
    assert [m["enabledByDefault"] for m in modules] == [True, False, True]
    assert compile_preset(result.document).compiled_text == "开头规则\n\n结尾规则"


def test_generic_dict_json_falls_back_to_string_fields():
    payload = {
        "name": "我的通用预设",
        "version": "1.0",
        "system_prompt": "系统提示词内容，必须遵守。",
        "style_guide": "文风指南：简洁、克制。",
        "temperature": 0.7,
        "rules": ["规则甲的完整内容", "规则乙的完整内容"],
    }
    result = _import_json(payload, "custom.json")
    doc_dump = result.document.model_dump(mode="python", by_alias=True)

    assert result.title == "我的通用预设"
    assert doc_dump["meta"]["sourceFormat"] == "generic"
    identifiers = [m["sourceIdentifier"] for m in doc_dump["modules"]]
    assert "system_prompt" in identifiers
    assert "style_guide" in identifiers
    assert doc_dump["sampling"]["default"]["temperature"] == 0.7
    text = compile_preset(result.document).compiled_text
    assert "系统提示词内容" in text
    assert "规则甲的完整内容" in text


def test_character_card_style_json_extracts_nested_data_fields():
    payload = {
        "spec": "chara_card_v2",
        "name": "卡片角色",
        "data": {
            "name": "卡片角色",
            "description": "角色描述：她是一名雨夜里的侦探。",
            "personality": "冷静、话少、观察力强。",
            "first_mes": "……你终于来了。",
        },
    }
    result = _import_json(payload, "card.json")
    text = compile_preset(result.document).compiled_text

    assert "雨夜里的侦探" in text
    assert "冷静、话少" in text


def test_unrecognized_json_never_imports_empty():
    payload = {"alpha": 1, "beta": [1, 2, 3], "gamma": {"deep": {"deeper": True}}}
    result = _import_json(payload, "opaque.json")

    # 结构无法识别时兜底导入原文，绝不产出空预设。
    assert result.module_count == 1
    modules = result.document.model_dump(mode="python", by_alias=True)["modules"]
    assert modules[0]["sourceIdentifier"] == "raw_content"
    assert '"alpha"' in compile_preset(result.document).compiled_text


def test_storydex_native_v1_document_still_compiles_with_slot_headers():
    from services.preset_schema import PresetDocument

    doc = PresetDocument.model_validate(
        {
            "version": 1,
            "meta": {"name": "原生预设"},
            "style": {"free_text_slot_pre": "硬边界规则内容"},
        }
    )
    compiled = compile_preset(doc)
    assert "[boundary/v1_free_text_slot_pre" in compiled.compiled_text
    assert "硬边界规则内容" in compiled.compiled_text
