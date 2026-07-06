"""预设生效性端到端验证。

覆盖真实链路：激活预设 → orchestration 构建 TurnContract（内含上下文装配）
→ coomi_agent_service._render_turn_contract 渲染成系统提示词附加文本。
这条链路就是每次 agent 请求实际走的路径（routes_agent → stream_events →
_build_coomi_system_prompt），因此文本出现在渲染结果里即代表 agent 能读到。
"""
import json

from services.coomi_agent_service import _render_turn_contract
from services.silly_tavern_preset_importer import convert_silly_tavern_preset, safe_preset_filename_stem
from services.story_project_service import StoryProjectService
from services.storydex_context_assembler_service import StorydexContextAssemblerService
from services.storydex_orchestration_service import StorydexOrchestrationService


def _make_orchestration(tmp_path):
    service = StoryProjectService()
    service.ensure_project_structure(tmp_path)
    assembler = StorydexContextAssemblerService(story_project_service=service)
    return service, StorydexOrchestrationService(story_project_service=service, context_assembler=assembler)


def _activate_import(service: StoryProjectService, tmp_path, raw: bytes, filename: str) -> None:
    imported = convert_silly_tavern_preset(raw, filename=filename)
    active_dir = service.preset_root(tmp_path) / "active"
    active_dir.mkdir(parents=True, exist_ok=True)
    md_path = active_dir / (safe_preset_filename_stem(imported.title) + ".md")
    md_path.write_text(imported.markdown, encoding="utf-8")
    service.write_preset_sidecar(md_path, imported.document)


def test_active_st_preset_reaches_agent_system_prompt_text(tmp_path):
    service, orchestration = _make_orchestration(tmp_path)
    payload = {
        "prompts": [
            {
                "identifier": "style",
                "name": "Style",
                "role": "system",
                "content": "本作规定：所有场景使用第二人称现在时，称呼{{user}}为旅人。",
            },
            {
                "identifier": "taboo",
                "name": "Taboo",
                "role": "system",
                "content": "禁止使用『忽然』开头的句子。{{setvar::mood::阴郁}}基调={{getvar::mood}}。",
            },
        ],
        "prompt_order": [
            {
                "character_id": 100001,
                "order": [
                    {"identifier": "style", "enabled": True},
                    {"identifier": "taboo", "enabled": True},
                ],
            }
        ],
    }
    _activate_import(service, tmp_path, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "style.json")

    contract = orchestration.build_turn_contract(tmp_path, prompt="继续写一段剧情")
    rendered = _render_turn_contract(contract)

    # 预设文本完整进入系统提示词附加段，宏已展开。
    assert "第二人称现在时" in rendered
    assert "禁止使用『忽然』开头的句子" in rendered
    assert "基调=阴郁" in rendered
    assert "{{setvar" not in rendered
    # 指令措辞要求 agent 遵循预设。
    assert "authoritative creative directives" in rendered
    assert "binding creative rules" in rendered


def test_active_plain_text_preset_reaches_agent_system_prompt_text(tmp_path):
    service, orchestration = _make_orchestration(tmp_path)
    _activate_import(service, tmp_path, "普通文本预设：每章结尾必须落在动作上。".encode("utf-8"), "普通.txt")

    rendered = _render_turn_contract(orchestration.build_turn_contract(tmp_path, prompt="写一段"))

    assert "每章结尾必须落在动作上" in rendered


def test_large_active_preset_is_not_truncated_in_agent_prompt_text(tmp_path):
    service, orchestration = _make_orchestration(tmp_path)
    filler = "规则内容填充" * 150  # 900 字/条
    payload = {
        "prompts": [
            {"identifier": f"p{i}", "name": f"P{i}", "role": "system", "content": f"[规则{i}]{filler}"}
            for i in range(10)
        ],
        "prompt_order": [
            {
                "character_id": 100001,
                "order": [{"identifier": f"p{i}", "enabled": True} for i in range(10)],
            }
        ],
    }
    _activate_import(service, tmp_path, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "big.json")

    rendered = _render_turn_contract(orchestration.build_turn_contract(tmp_path, prompt="写一段"))

    assert "[规则0]" in rendered
    assert "[规则9]" in rendered
    assert "[truncated]" not in rendered
