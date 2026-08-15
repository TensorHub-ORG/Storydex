from __future__ import annotations

import asyncio
import json
from pathlib import Path

from services.coomi_agent_service import _build_coomi_system_prompt
from services.context_policy import ContextPolicy
from services.document_structure_context_service import DocumentStructureContextService
from services.story_project_service import get_story_project_service
from services.storydex_context_assembler_service import StorydexContextAssemblerService


def _write(root: Path, relative_path: str, content: str) -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _large_markdown(*, title: str, target_heading: str, target_body: str) -> str:
    sections = [f"# {title}", "开头摘要"]
    for index in range(44):
        if index == 25:
            sections.extend([f"## {target_heading}", target_body])
        else:
            sections.extend(
                [
                    f"## 普通字段 {index:02d} " + ("冗长结构名" * 10),
                    f"占位设定 {index:02d} " + ("甲" * 260),
                ]
            )
    content = "\n\n".join(sections) + "\n"
    assert content.index(f"## {target_heading}") > 6_000
    return content


def test_late_markdown_field_survives_structure_map_budget(tmp_path: Path) -> None:
    target_body = "沈月真正想要的是摆脱家族安排，并证明自己的选择能够保护同伴。"
    _write(
        tmp_path,
        ".storydex/characters/Shenyue.md",
        _large_markdown(
            title="沈月", target_heading="核心动机", target_body=target_body
        ),
    )
    block, paths, diagnostic = DocumentStructureContextService().build_context(
        tmp_path,
        kind="character",
        prompt="修改沈月角色卡核心动机，只修改这个字段",
        active_file="",
        active_entities=["沈月"],
        catalog_snapshot=None,
        max_files=1,
        max_chars_per_file=850,
        total_chars=1_300,
    )

    assert paths == [".storydex/characters/Shenyue.md"]
    assert "H2 核心动机" in block
    assert target_body in block
    assert "Matched evidence span:" in block
    assert "revision=sha256:" in block
    assert "sha256:sha256:" not in block
    assert len(block) <= 1_300
    assert diagnostic["matchedSpanCount"] >= 1
    assert diagnostic["requiresFullReadBeforeWrite"] is True


def test_worldbook_late_rule_is_returned_as_matched_span(tmp_path: Path) -> None:
    target_body = "雾海航道每逢赤潮封闭，任何民用船只都不得穿越第三浮标。"
    _write(
        tmp_path,
        ".storydex/worldbook/Mist-Sea.md",
        _large_markdown(
            title="雾海", target_heading="通行规则", target_body=target_body
        ),
    )

    block, paths, diagnostic = DocumentStructureContextService().build_context(
        tmp_path,
        kind="worldbook",
        prompt="检查雾海通行规则",
        active_file="",
        active_entities=[],
        catalog_snapshot=None,
        max_files=1,
        max_chars_per_file=850,
        total_chars=1_300,
    )

    assert paths == [".storydex/worldbook/Mist-Sea.md"]
    assert "H2 通行规则" in block
    assert target_body in block
    assert diagnostic["matchedSpanCount"] >= 1


def test_json_field_alias_maps_core_motivation_to_motivation_key(
    tmp_path: Path,
) -> None:
    motivation = "她必须在王城审判前找到失踪的证人。"
    payload = {
        "name": "沈月",
        "aliases": ["阿月"],
        **{f"custom_{index:02d}": f"占位值 {index}" for index in range(60)},
        "background": "来自北境",
        "motivation": motivation,
    }
    _write(
        tmp_path,
        ".storydex/characters/Shenyue.json",
        json.dumps(payload, ensure_ascii=False, indent=2),
    )

    block, paths, diagnostic = DocumentStructureContextService().build_context(
        tmp_path,
        kind="character",
        prompt="修改沈月的核心动机",
        active_file="",
        active_entities=["沈月"],
        catalog_snapshot=None,
        max_files=1,
        max_chars_per_file=1_000,
        total_chars=1_400,
    )

    assert paths == [".storydex/characters/Shenyue.json"]
    assert "key=motivation" in block
    assert motivation in block
    assert diagnostic["matchedSpanCount"] >= 1


def test_active_file_is_selected_even_without_text_match(tmp_path: Path) -> None:
    _write(tmp_path, ".storydex/characters/A.md", "# 甲\n\n## 身份\n甲方角色\n")
    _write(tmp_path, ".storydex/characters/B.md", "# 乙\n\n## 身份\n乙方角色\n")

    _block, paths, _diagnostic = DocumentStructureContextService().build_context(
        tmp_path,
        kind="character",
        prompt="读取当前文件",
        active_file=".storydex/characters/B.md",
        active_entities=[],
        catalog_snapshot=None,
        max_files=1,
        max_chars_per_file=800,
        total_chars=1_200,
    )

    assert paths == [".storydex/characters/B.md"]


def test_no_exact_match_keeps_location_map_without_claiming_absence(
    tmp_path: Path,
) -> None:
    _write(tmp_path, ".storydex/worldbook/city.md", "# 石桥城\n\n## 地理\n位于河谷。\n")

    block, paths, diagnostic = DocumentStructureContextService().build_context(
        tmp_path,
        kind="worldbook",
        prompt="查找量子幽灵协议",
        active_file="",
        active_entities=[],
        catalog_snapshot=None,
        max_files=1,
        max_chars_per_file=800,
        total_chars=1_200,
    )

    assert paths == [".storydex/worldbook/city.md"]
    assert "Structure map (Markdown headings):" in block
    assert "Matched evidence span:" not in block
    assert diagnostic["matchedSpanCount"] == 0


def test_context_assembler_uses_structure_maps_and_records_diagnostics(
    tmp_path: Path,
) -> None:
    project_service = get_story_project_service()
    project_service.ensure_project_structure(tmp_path)
    target_body = "她拒绝继承旧誓约，只愿保护亲自选择的同伴。"
    _write(
        tmp_path,
        ".storydex/characters/Shenyue.md",
        _large_markdown(
            title="沈月", target_heading="核心动机", target_body=target_body
        ),
    )
    _write(
        tmp_path,
        ".storydex/worldbook/unrelated-city.md",
        "# 无关城邦\n\n## 地理\n位于南方群岛。\n",
    )
    policy = ContextPolicy(
        base_story_context=True,
        story_structured_memory=False,
        passive_fts=False,
        wiki_context=False,
        coomi_memory=False,
        active_retrieval_tools=False,
    )

    assembly = StorydexContextAssemblerService(project_service).assemble(
        tmp_path,
        prompt="修改沈月角色卡核心动机",
        intent_primary="character_work",
        policy=policy,
    )

    block = next(
        item for item in assembly["promptBlocks"] if item["id"] == "active_characters"
    )
    source = next(
        item
        for item in assembly["contextTrace"]["sources"]
        if item["kind"] == "active_characters"
    )
    assert block["title"] == "Relevant character structure maps and matched evidence"
    assert "Hard Constraints" not in block["content"]
    assert target_body in block["content"]
    assert source["policy"] == "structure_map_matched_spans_jit_read"
    assert source["structureMapCount"] >= 1
    assert source["matchedSpanCount"] >= 1
    assert source["requiresFullReadBeforeWrite"] is True
    assert all(item["id"] != "worldbook" for item in assembly["promptBlocks"])
    worldbook_source = next(
        item
        for item in assembly["contextTrace"]["sources"]
        if item["kind"] == "worldbook"
    )
    assert worldbook_source["included"] is False
    assert worldbook_source["structureMapCount"] == 0


def test_system_prompt_requires_full_read_before_existing_file_write(
    tmp_path: Path,
) -> None:
    prompt = asyncio.run(
        _build_coomi_system_prompt(
            workspace_root=tmp_path,
            prompt="修改角色卡",
            turn_contract={
                "intentFrame": {
                    "primary": "character_work",
                    "operationType": "modify_existing",
                    "decision": "decided",
                    "canWrite": True,
                },
                "executionPolicy": {
                    "capabilityMode": "scoped_write",
                    "directFileWrites": True,
                    "allowedWriteRoots": [".storydex/characters/"],
                },
                "contextPolicy": {"sources": ContextPolicy().to_dict()},
                "contextAssembly": {"promptBlocks": [], "sources": [], "budget": {}},
            },
        )
    )

    assert "structure map" in prompt.lower()
    assert "read_file" in prompt
    assert "hasMore=true" in prompt
    assert "revision" in prompt.lower()


def test_related_passage_excludes_keep_truncated_chapter_eligible_for_span_retrieval() -> (
    None
):
    excludes = StorydexContextAssemblerService._related_passage_excludes(
        active_file="chapters/第3章/001.md",
        recent_segments=[
            {
                "relativePath": "chapters/第3章/001.md",
                "content": "... [tail anchor]\n章节尾部",
            },
            {
                "relativePath": "chapters/第2章/001.md",
                "content": "完整短章节",
            },
        ],
        rolling_paths=[".storydex/memory/summaries/rolling/第2章.md"],
    )

    assert "chapters/第3章/001.md" not in excludes
    assert "chapters/第2章/001.md" in excludes
    assert ".storydex/memory/summaries/rolling/第2章.md" in excludes

    structured_active = StorydexContextAssemblerService._related_passage_excludes(
        active_file=".storydex/characters/Shenyue.md",
        recent_segments=[],
        rolling_paths=[],
    )
    assert ".storydex/characters/Shenyue.md" in structured_active
