import json

from services.preset_compiler import compile_preset
from services.silly_tavern_macro_runtime import create_silly_tavern_macro_runtime
from services.silly_tavern_preset_importer import convert_silly_tavern_preset
from services.story_project_service import StoryProjectService


def _import_payload(payload: dict, filename: str = "st.json"):
    return convert_silly_tavern_preset(json.dumps(payload, ensure_ascii=False).encode("utf-8"), filename=filename)


def test_prompt_order_prefers_dummy_character_100001_over_longer_order():
    payload = {
        "prompts": [
            {"identifier": "a", "name": "A", "role": "system", "content": "A"},
            {"identifier": "b", "name": "B", "role": "system", "content": "B"},
            {"identifier": "c", "name": "C", "role": "system", "content": "C"},
        ],
        "prompt_order": [
            {
                "character_id": 100000,
                "order": [
                    {"identifier": "a", "enabled": True},
                    {"identifier": "b", "enabled": True},
                    {"identifier": "c", "enabled": True},
                ],
            },
            {
                "character_id": 100001,
                "order": [
                    {"identifier": "c", "enabled": True},
                    {"identifier": "a", "enabled": False},
                ],
            },
        ],
    }

    result = _import_payload(payload)
    doc_dump = result.document.model_dump(mode="python", by_alias=True)

    assert [item["identifier"] for item in doc_dump["sillyTavern"]["selectedPromptOrder"]] == ["c", "a"]
    assert compile_preset(result.document).compiled_text == "C"


def test_prompts_missing_from_prompt_order_import_disabled_and_sorted_last():
    payload = {
        "prompts": [
            {"identifier": "orphan", "name": "Orphan", "role": "system", "content": "Orphan prompt"},
            {"identifier": "main", "name": "Main", "role": "system", "content": "Main prompt"},
        ],
        "prompt_order": [
            {"character_id": 100001, "order": [{"identifier": "main", "enabled": True}]}
        ],
    }

    result = _import_payload(payload)
    modules = result.document.model_dump(mode="python", by_alias=True)["modules"]
    by_identifier = {module["sourceIdentifier"]: module for module in modules}

    # 全部导入，不丢内容；只是遵循 ST 的开关语义：不在 order 中 = 默认关闭。
    assert set(by_identifier) == {"orphan", "main"}
    assert by_identifier["main"]["enabledByDefault"] is True
    assert by_identifier["orphan"]["enabledByDefault"] is False
    assert by_identifier["orphan"]["sourceOrder"] > by_identifier["main"]["sourceOrder"]
    assert compile_preset(result.document).compiled_text == "Main prompt"

    # 用户手动开启后可参与编译（排在有序模块之后）。
    enabled = compile_preset(result.document, overrides={"enabledModuleIds": ["st_orphan"]})
    assert enabled.compiled_text == "Main prompt\n\nOrphan prompt"


def test_gemini_style_model_role_normalizes_to_assistant():
    payload = {
        "prompts": [
            {
                "identifier": "model_abs",
                "name": "Model Absolute",
                "role": "model",
                "content": "Model injection",
                "injection_position": 1,
                "injection_depth": 3,
                "injection_order": 100,
            },
            {"identifier": "human_rel", "name": "Human", "role": "human", "content": "Human prompt"},
        ],
        "prompt_order": [
            {"identifier": "model_abs", "enabled": True},
            {"identifier": "human_rel", "enabled": True},
        ],
    }

    result = _import_payload(payload)
    modules = result.document.model_dump(mode="python", by_alias=True)["modules"]
    by_identifier = {module["sourceIdentifier"]: module for module in modules}
    assert by_identifier["model_abs"]["sourceRole"] == "assistant"
    assert by_identifier["human_rel"]["sourceRole"] == "user"

    compiled = compile_preset(result.document)
    assert [injection.role for injection in compiled.injections] == ["assistant"]
    assert compiled.injections[0].text == "Model injection"


def test_absolute_injections_are_not_duplicated_into_compiled_text():
    payload = {
        "prompts": [
            {"identifier": "relative", "name": "Relative", "role": "system", "content": "Relative prompt"},
            {
                "identifier": "absolute",
                "name": "Absolute",
                "role": "system",
                "content": "Absolute prompt",
                "injection_position": 1,
                "injection_depth": 4,
            },
        ],
        "prompt_order": [
            {"identifier": "relative", "enabled": True},
            {"identifier": "absolute", "enabled": True},
        ],
    }

    compiled = compile_preset(_import_payload(payload).document)

    assert compiled.compiled_text == "Relative prompt"
    assert [injection.text for injection in compiled.injections] == ["Absolute prompt"]
    # 绝对注入模块不应被误报为未注入。
    assert not [risk for risk in compiled.risks if risk.code == "not_injected"]


def test_import_macro_hints_are_aggregated_per_macro_name():
    content = "\n".join(f"{{{{setvar::v{i}::{i}}}}} {{{{getvar::v{i}}}}}" for i in range(200))
    payload = {
        "prompts": [{"identifier": "vars", "name": "Vars", "role": "system", "content": content}],
        "prompt_order": [{"identifier": "vars", "enabled": True}],
    }

    result = _import_payload(payload)

    assert len(result.import_warnings) <= 45
    joined = "\n".join(result.import_warnings)
    assert "{{setvar}} ×200" in joined
    assert "{{getvar}} ×200" in joined


def test_macro_runtime_trim_eats_surrounding_newlines():
    runtime = create_silly_tavern_macro_runtime({})
    assert runtime.expand("Line A\n\n{{trim}}\n\nLine B") == "Line ALine B"


def test_runtime_preset_context_keeps_large_silly_tavern_preset_untruncated(tmp_path):
    service = StoryProjectService()
    service.ensure_project_structure(tmp_path)
    filler = "字数填充" * 200  # 800 字/条，10 条相对 prompt 共 8000 字
    prompts = [
        {"identifier": f"p{i}", "name": f"P{i}", "role": "system", "content": f"[{i}]{filler}"}
        for i in range(10)
    ]
    prompts.append(
        {
            "identifier": "abs",
            "name": "Abs",
            "role": "system",
            "content": "绝对注入内容",
            "injection_position": 1,
            "injection_depth": 4,
        }
    )
    payload = {
        "prompts": prompts,
        "prompt_order": [
            {
                "character_id": 100001,
                "order": [{"identifier": prompt["identifier"], "enabled": True} for prompt in prompts],
            }
        ],
    }
    imported = _import_payload(payload, filename="big.json")
    active_dir = service.preset_root(tmp_path) / "active"
    active_dir.mkdir(parents=True, exist_ok=True)
    md_path = active_dir / "big.md"
    md_path.write_text(imported.markdown, encoding="utf-8")
    service.write_preset_sidecar(md_path, imported.document)

    context = service._build_preset_context(  # noqa: SLF001 - runtime preset context path
        tmp_path,
        max_chars_per_file=720,
        total_chars=2200,
        runtime_context={"user": "读者", "char": "角色"},
    )

    # 默认 720/2200 预算不应截掉 ST 编译文本：首尾模块都要在。
    assert "[0]" in context
    assert "[9]" in context
    assert "[truncated]" not in context
    # 绝对注入按 depth 追加到文本尾部，不重复出现。
    assert context.count("绝对注入内容") == 1
    assert context.rindex("绝对注入内容") > context.rindex("[9]")
