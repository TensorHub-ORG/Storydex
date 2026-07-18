from __future__ import annotations

import json

from services.prompt_repository_service import PromptRepositoryService
from services.story_project_service import StoryProjectService, _LEGACY_AGENT_SKILLS_V1


def test_prompt_repository_reads_categories_prompt_blocks_and_placeholders(monkeypatch, tmp_path):
    root = tmp_path / "prompts"
    category = root / "世界观"
    category.mkdir(parents=True)
    (root / "README.md").write_text("# index\n", encoding="utf-8")
    (category / "01-theme.md").write_text(
        "# 制定主题世界观\n\n> 通用世界观模板。\n\n```prompt\n请制定[主题]世界观，规模为[世界规模]。\n```\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("STORYDEX_PROMPT_REPOSITORY_ROOT", str(root))

    service = PromptRepositoryService()
    repository = service.read_repository()
    assert repository["categories"] == [{"id": "世界观", "label": "世界观", "count": 1}]
    assert repository["items"][0]["promptText"].startswith("请制定")
    assert repository["items"][0]["placeholders"] == ["[主题]", "[世界规模]"]
    assert service.read_repository(query="主题")["items"]
    assert service.read_repository(category="角色创作")["items"] == []


def test_default_agent_skill_templates_are_detailed_universal_and_migrate_legacy(tmp_path):
    service = StoryProjectService()
    service.ensure_project_structure(tmp_path)
    skills_root = service.agent_root(tmp_path) / "skills"
    source_root = service._resolve_builtin_skills_root()
    assert source_root is not None
    expected = {
        "设计角色.md",
        "角色更新.md",
        "设计世界书条目.md",
        "设计剧本.md",
        "变量思考.md",
        "WIKI整理.md",
        "项目目录整理.md",
        "故事生成后更新.md",
    }
    source_names = {
        path.name for path in source_root.glob("*.md") if path.name.lower() != "readme.md"
    }
    assert source_names == expected | {"story_preset_constraints.md"}
    for name in expected:
        content = (skills_root / name).read_text(encoding="utf-8")
        assert content == (source_root / name).read_text(encoding="utf-8")
        assert "模板版本：2" in content
        assert "适用范围：任意 Storydex 小说项目" in content
        assert "## 7. 输出模板" in content
        assert "## 8. 完成前自检" in content
        assert "## 9. 安全边界" in content

    preset_skill = (skills_root / "story_preset_constraints.md").read_text(encoding="utf-8")
    assert preset_skill == (source_root / "story_preset_constraints.md").read_text(encoding="utf-8")
    assert "模板版本：2" in preset_skill
    assert "当前没有启用项目预设" in preset_skill

    registry = json.loads((skills_root / "registry.json").read_text(encoding="utf-8"))
    assert registry["version"] == 2
    assert registry["policy"]["universalForAnyNovelProject"] is True

    legacy_path = skills_root / "设计角色.md"
    legacy_path.write_text(_LEGACY_AGENT_SKILLS_V1["设计角色.md"].strip() + "\n", encoding="utf-8")
    custom_path = skills_root / "角色更新.md"
    custom_path.write_text("# 我的自定义角色更新\n", encoding="utf-8")
    service.ensure_project_structure(tmp_path)
    assert "模板版本：2" in legacy_path.read_text(encoding="utf-8")
    assert custom_path.read_text(encoding="utf-8") == "# 我的自定义角色更新\n"
