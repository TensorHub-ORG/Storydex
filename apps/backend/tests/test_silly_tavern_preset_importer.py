import json
import os
import re
from pathlib import Path

import pytest

from services.preset_compiler import compile_preset
from services.silly_tavern_preset_importer import convert_silly_tavern_preset


SAMPLE_PRESET_PATHS = [
    Path(r"E:\文档\1preset\双人成行 V4.5不测试会不会炸呢？.json"),
    Path(r"E:\文档\1preset\夏瑾 双鱼座 Beta 0.21.json"),
    Path(r"E:\文档\1preset\【梁元】lunareclipse 1.7.json"),
    Path(r"E:\文档\1preset\【DarkSide-小猫之神】v1.1.json"),
]


def test_import_preserves_silly_tavern_macros_in_module_content():
    result = convert_silly_tavern_preset(
        (
            "Keep {{user}} and {{char}}.\n"
            "Mood: {{getvar::mood}}.\n"
            "{{setvar::mood::focused}}\n"
            "Pick: {{random::quiet::sharp}}.\n"
            "{{// author note}}\n"
        ).encode("utf-8"),
        filename="macro-test.txt",
    )

    content = result.document.model_dump(mode="python")["modules"][0]["content"]

    assert "{{user}}" in content
    assert "{{char}}" in content
    assert "{{getvar::mood}}" in content
    assert "{{setvar::mood::focused}}" in content
    assert "{{random::quiet::sharp}}" in content
    assert "{{// author note}}" in content


def test_import_reports_preserved_silly_tavern_macros_without_removal_language():
    result = convert_silly_tavern_preset(
        "Mood: {{getvar::mood}} {{setvar::mood::focused}} {{random::a::b}}".encode("utf-8"),
        filename="macro-test.txt",
    )

    warnings = "\n".join(result.import_warnings)

    assert "保留外部预设宏" in warnings
    assert "移除" not in warnings


def test_import_preserves_raw_silly_tavern_payload_without_regex_runtime_modules():
    payload = {
        "temperature": 0.9,
        "prompts": [
            {
                "identifier": "main",
                "name": "Main",
                "role": "system",
                "content": "Use {{user}} and {{char}}.",
            }
        ],
        "prompt_order": [{"character_id": 100001, "order": [{"identifier": "main", "enabled": True}]}],
        "extensions": {
            "SPreset": {
                "RegexBinding": {
                    "regexes": [
                        {
                            "id": "regex-1",
                            "scriptName": "Prompt wrapper",
                            "findRegex": "^(.*)$",
                            "replaceString": "<wrapped>$1</wrapped>",
                            "promptOnly": True,
                            "markdownOnly": False,
                            "disabled": False,
                        }
                    ]
                }
            },
            "regex_scripts": [
                {
                    "id": "regex-1",
                    "scriptName": "Prompt wrapper",
                    "findRegex": "^(.*)$",
                    "replaceString": "<wrapped>$1</wrapped>",
                    "promptOnly": True,
                    "markdownOnly": False,
                    "disabled": False,
                }
            ],
        },
    }

    result = convert_silly_tavern_preset(json.dumps(payload, ensure_ascii=False).encode("utf-8"), filename="st.json")
    doc_dump = result.document.model_dump(mode="python", by_alias=True)

    assert len(doc_dump["modules"]) == 1
    assert doc_dump["modules"][0]["sourceOrder"] == 0
    assert doc_dump["sillyTavern"]["sourcePreset"]["prompts"][0]["identifier"] == "main"
    assert doc_dump["sillyTavern"]["sourcePreset"]["extensions"]["SPreset"]["RegexBinding"]["regexes"][0]["scriptName"] == "Prompt wrapper"
    assert result.display_regexes[0]["source"] == "SPreset.RegexBinding"


def test_compile_silly_tavern_macros_in_prompt_order_without_agent_base_changes():
    payload = {
        "prompts": [
            {
                "identifier": "set_style",
                "name": "Style setter",
                "role": "system",
                "content": "setter-visible {{setvar::tone::月光}}\n{{addvar::tone::与潮声}}",
            },
            {
                "identifier": "main",
                "name": "Main task",
                "role": "system",
                "content": (
                    "Tone={{getvar::tone}}; user={{user}}; char={{char}}; "
                    "persona={{personality}}; scene={{scenario}}; pick={{random::a::b}}; roll={{roll 1d1}}; "
                    "{{// hidden note}}{{trim}}"
                ),
            },
        ],
        "prompt_order": [
            {
                "character_id": 100001,
                "order": [
                    {"identifier": "set_style", "enabled": True},
                    {"identifier": "main", "enabled": True},
                ],
            }
        ],
    }
    result = convert_silly_tavern_preset(json.dumps(payload, ensure_ascii=False).encode("utf-8"), filename="st.json")

    compiled = compile_preset(
        result.document,
        runtime_context={
            "user": "读者",
            "char": "夏瑾",
            "personality": "冷静",
            "scenario": "雨夜",
            "randomSeed": 7,
        },
    )

    text = compiled.compiled_text
    assert "Tone=月光与潮声" in text
    assert "user=读者" in text
    assert "char=夏瑾" in text
    assert "persona=冷静" in text
    assert "scene=雨夜" in text
    assert "roll=1" in text
    assert "pick={{random" not in text
    assert "{{setvar" not in text
    assert "hidden note" not in text
    assert text.index("setter-visible") < text.index("Tone=")


def test_compile_silly_tavern_runtime_text_omits_storydex_section_headers():
    payload = {
        "prompts": [
            {
                "identifier": "first",
                "name": "First",
                "role": "system",
                "content": "First ST prompt.",
            },
            {
                "identifier": "second",
                "name": "Second",
                "role": "system",
                "content": "Second ST prompt.",
            },
        ],
        "prompt_order": [
            {
                "character_id": 100001,
                "order": [
                    {"identifier": "first", "enabled": True},
                    {"identifier": "second", "enabled": True},
                ],
            }
        ],
    }
    result = convert_silly_tavern_preset(json.dumps(payload, ensure_ascii=False).encode("utf-8"), filename="st.json")

    compiled = compile_preset(result.document)

    assert compiled.compiled_text == "First ST prompt.\n\nSecond ST prompt."
    assert "[boundary/" not in compiled.compiled_text
    assert "[advanced/" not in compiled.compiled_text


def test_import_accepts_direct_prompt_order_array_from_silly_tavern_exports():
    payload = {
        "prompts": [
            {"identifier": "a", "name": "A", "role": "system", "content": "A enabled"},
            {"identifier": "b", "name": "B", "role": "system", "content": "B disabled"},
            {"identifier": "c", "name": "C", "role": "system", "content": "C enabled first"},
        ],
        "prompt_order": [
            {"identifier": "c", "enabled": True},
            {"identifier": "b", "enabled": False},
            {"identifier": "a", "enabled": True},
        ],
    }

    result = convert_silly_tavern_preset(json.dumps(payload, ensure_ascii=False).encode("utf-8"), filename="direct.json")
    doc_dump = result.document.model_dump(mode="python", by_alias=True)
    compiled = compile_preset(result.document)

    assert [item["identifier"] for item in doc_dump["sillyTavern"]["selectedPromptOrder"]] == ["c", "b", "a"]
    assert [(item["sourceIdentifier"], item["enabledByDefault"], item["sourceOrder"]) for item in doc_dump["modules"]] == [
        ("c", True, 0),
        ("b", False, 1),
        ("a", True, 2),
    ]
    assert compiled.compiled_text == "C enabled first\n\nA enabled"


def test_import_preserves_silly_tavern_prompt_execution_metadata():
    payload = {
        "prompts": [
            {
                "identifier": "absolute_user",
                "name": "Absolute User",
                "role": "user",
                "content": "Absolute prompt",
                "system_prompt": True,
                "forbid_overrides": True,
                "injection_position": 1,
                "injection_depth": 8,
                "injection_order": 42,
                "injection_trigger": ["continue", "swipe"],
            }
        ],
        "prompt_order": [{"identifier": "absolute_user", "enabled": True}],
    }

    result = convert_silly_tavern_preset(json.dumps(payload, ensure_ascii=False).encode("utf-8"), filename="metadata.json")
    module = result.document.model_dump(mode="python", by_alias=True)["modules"][0]

    assert module["sourceRole"] == "user"
    assert module["sourceSystemPrompt"] is True
    assert module["forbidOverrides"] is True
    assert module["injectionPosition"] == 1
    assert module["injectionDepth"] == 8
    assert module["injectionOrder"] == 42
    assert module["injectionTrigger"] == ["continue", "swipe"]


def test_compile_silly_tavern_respects_injection_trigger_generation_type():
    payload = {
        "prompts": [
            {
                "identifier": "normal",
                "name": "Normal",
                "role": "system",
                "content": "Normal prompt",
                "injection_trigger": ["normal"],
            },
            {
                "identifier": "continue",
                "name": "Continue",
                "role": "system",
                "content": "Continue prompt",
                "injection_trigger": ["continue"],
            },
        ],
        "prompt_order": [
            {"identifier": "normal", "enabled": True},
            {"identifier": "continue", "enabled": True},
        ],
    }

    result = convert_silly_tavern_preset(json.dumps(payload, ensure_ascii=False).encode("utf-8"), filename="triggers.json")

    normal_text = compile_preset(result.document, runtime_context={"generationType": "normal"}).compiled_text
    continue_text = compile_preset(result.document, runtime_context={"generationType": "continue"}).compiled_text

    assert normal_text == "Normal prompt"
    assert continue_text == "Continue prompt"


def test_compile_silly_tavern_exposes_absolute_injections_grouped_by_depth_order_and_role():
    payload = {
        "prompts": [
            {
                "identifier": "relative",
                "name": "Relative",
                "role": "system",
                "content": "Relative prompt",
                "injection_position": 0,
            },
            {
                "identifier": "system_high",
                "name": "System High",
                "role": "system",
                "content": "System high",
                "injection_position": 1,
                "injection_depth": 2,
                "injection_order": 200,
            },
            {
                "identifier": "assistant_high",
                "name": "Assistant High",
                "role": "assistant",
                "content": "Assistant high",
                "injection_position": 1,
                "injection_depth": 2,
                "injection_order": 200,
            },
            {
                "identifier": "user_middle",
                "name": "User Middle",
                "role": "user",
                "content": "User middle",
                "injection_position": 1,
                "injection_depth": 2,
                "injection_order": 100,
            },
            {
                "identifier": "system_low",
                "name": "System Low",
                "role": "system",
                "content": "System low",
                "injection_position": 1,
                "injection_depth": 2,
                "injection_order": 50,
            },
        ],
        "prompt_order": [
            {"identifier": "relative", "enabled": True},
            {"identifier": "system_high", "enabled": True},
            {"identifier": "assistant_high", "enabled": True},
            {"identifier": "user_middle", "enabled": True},
            {"identifier": "system_low", "enabled": True},
        ],
    }

    result = convert_silly_tavern_preset(json.dumps(payload, ensure_ascii=False).encode("utf-8"), filename="absolute.json")
    compiled = compile_preset(result.document)
    injections = [item.model_dump(mode="python", by_alias=True) for item in compiled.injections]

    assert injections == [
        {"depth": 2, "order": 200, "role": "system", "text": "System high", "sourceModuleIds": ["st_system_high"]},
        {"depth": 2, "order": 200, "role": "assistant", "text": "Assistant high", "sourceModuleIds": ["st_assistant_high"]},
        {"depth": 2, "order": 100, "role": "user", "text": "User middle", "sourceModuleIds": ["st_user_middle"]},
        {"depth": 2, "order": 50, "role": "system", "text": "System low", "sourceModuleIds": ["st_system_low"]},
    ]
    assert "Relative prompt" in compiled.compiled_text


@pytest.mark.skipif(not all(path.exists() for path in SAMPLE_PRESET_PATHS), reason="local SillyTavern preset samples are not available")
def test_local_silly_tavern_samples_import_with_raw_payload_and_enabled_modules():
    unresolved_macro_pattern = re.compile(r"\{\{\s*([^}:\s]+)")
    for path in SAMPLE_PRESET_PATHS:
        result = convert_silly_tavern_preset(path.read_bytes(), filename=path.name)
        doc_dump = result.document.model_dump(mode="python", by_alias=True)
        enabled_modules = [item for item in doc_dump["modules"] if item.get("enabledByDefault") is not False]
        compiled = compile_preset(
            result.document,
            runtime_context={
                "user": "读者",
                "char": "角色",
                "personality": "性格",
                "scenario": "场景",
                "lastUserMessage": "用户输入",
                "lastCharMessage": "角色回复",
                "randomSeed": 7,
            },
        )

        assert result.module_count > 0, path.name
        assert enabled_modules, path.name
        assert doc_dump["sillyTavern"]["sourcePreset"]["prompts"], path.name
        assert doc_dump["sillyTavern"]["sourcePreset"]["prompt_order"], path.name
        assert doc_dump["runtimeDefaults"]["sourceFormat"] == "sillytavern"
        assert result.filtered_count == 0
        assert result.filtered_blocks == []
        assert not unresolved_macro_pattern.findall(compiled.compiled_text), path.name
