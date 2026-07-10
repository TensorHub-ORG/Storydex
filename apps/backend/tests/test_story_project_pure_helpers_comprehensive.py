import json

from services.story_project_service import StoryProjectService


def test_text_number_path_and_naming_helpers_cover_boundaries(tmp_path):
    service = StoryProjectService()
    assert service._coerce_text_list([" a ", "", None, 2]) == ["a", "2"]
    assert service._coerce_text_list(" x ") == ["x"]
    assert service._coerce_text_list(None) == []
    assert service._merge_text_lists(["A", ""], "a") == ["A"]
    assert service._append_limited_records([{"a": 1}, None], {"a": 1}, limit=0) == [{"a": 1}]
    assert service._append_limited_records([{"a": 1}], {"b": 2}, limit=1) == [{"b": 2}]
    assert service._normalize_character_lookup_key(" 001_ A B ") == "ab"
    assert service._strip_asset_sequence("02- Hero") == "Hero"
    assert service._segment_relative_from_snapshot_path("bad") == ""
    assert service._segment_relative_from_snapshot_path(".storydex/memory/chapters/one.variables.json").endswith("one.md")
    assert service._resolve_active_chapter_relative("") == ""
    assert service._resolve_active_chapter_relative("notes/a.md") == ""
    assert service._resolve_active_chapter_relative("chapters/a.md") == "chapters/a.md"
    assert service._resolve_active_chapter_relative("chapters/chapter/001.md") == "chapters/chapter"
    assert service._extract_chapter_number("chapter 012 title") == 12
    assert service._extract_chapter_number("nothing") == 0
    assert service._parse_chapter_number("") == 0
    assert service._parse_chapter_number("12") == 12
    assert service._number_to_chinese(0)
    for number in (1, 10, 11, 20, 99, 100, 101, 110):
        assert service._number_to_chinese(number)
    assert service._safe_int("bad", fallback=5, minimum=1, maximum=10) == 5
    assert service._safe_int(99, fallback=5, minimum=1, maximum=10) == 10
    assert service._safe_template_path_part("../bad:name", fallback="fallback") == "badname"
    assert service._safe_template_path_part("..", fallback="fallback") == "fallback"
    assert service._sanitize_chapter_title("a/b\\c")
    assert service._chapter_name_from_template({"chapterNamePattern": "{number}-{title}"}, number=2, title="Title")
    assert service._chapter_name_from_template({}, number=2, title="")
    assert service._build_new_chapter_name(3, "Title", number_style="arabic")
    assert service._build_new_chapter_name(3, "Title", number_style="chinese")
    assert service._build_chapter_display_name("", fallback_number=2)
    assert service._normalize_relative_path("././chapters\\a.md") == "chapters/a.md"
    assert service._normalize_story_segment_format("TXT") == "txt"
    assert service._normalize_story_segment_format("other") == "md"
    assert service._safe_leaf_name("")
    assert len(service._safe_leaf_name("a" * 80)) == 48
    assert service._truncate_text("short", 10) == "short"
    assert "truncated" in service._truncate_text("x" * 100, 30)


def test_increment_normalizers_relationships_and_nested_operations():
    service = StoryProjectService()
    assert service._normalize_story_increment_fragments({"fragments": [None, {"text": "a"}]}) == [{"text": "a"}]
    assert service._normalize_story_increment_fragments({"segment_path": "chapters/a.md", "content": "body"}) == [{"path": "chapters/a.md", "text": "body"}]
    assert service._normalize_story_increment_fragments({}) == []
    assert service._story_increment_fragment_text({"segment_text": " a\r\nb "}) == "a\nb"
    assert service._story_increment_fragment_text({}) == ""
    assert service._segment_metadata_from_relative_path("chapters/a.md")["chapter_id"] == "a"
    assert service._segment_metadata_from_relative_path("chapters/chapter/001.md")["chapter_id"] == "chapter"

    items = service._normalize_item_updates([" Sword ", None, {}, {"name": "Ring", "type": "artifact", "description": "magic", "aliases": ["R"], "tags": "tag"}])
    assert [item["item"] for item in items] == ["Sword", "Ring"]
    assert items[1]["kind"] == "artifact" and items[1]["aliases"] == ["R"]
    assert service._normalize_item_updates("Orb") == [{"item": "Orb"}]
    facts = service._normalize_fact_updates([None, {}, {"subject": "A", "relation": "knows", "value": "B", "confidence": "CANON"}])
    assert facts[0]["predicate"] == "knows" and facts[0]["confidence"] == "canon"
    assert service._normalize_fact_updates("bad") == []
    relationships = service._normalize_relationship_updates([
        None, {}, {"source": "A", "target": "A"},
        {"from": "A", "to": "B", "type": "trust", "currentLevel": 20, "summary": "friends"},
    ])
    assert relationships[0]["current_level"] == 10 and relationships[0]["dimension"] == "trust"
    assert service._normalize_relationship_updates("bad") == []
    assert service._safe_relationship_level(None) is None
    assert service._safe_relationship_level("bad") is None
    assert service._safe_relationship_level(-99) == -10
    assert service._clamp_relationship_level("bad") == 0
    assert service._relationship_delta_value({"delta": "increase", "magnitude": "strong"}) == 2
    assert service._relationship_delta_value({"delta": "decrease"}) == -1
    assert service._relationship_delta_value({}) == 0
    assert service._story_increment_has_variable_payload({"fact_updates": [{}]})
    assert not service._story_increment_has_variable_payload({"fact_updates": []})
    assert service._story_increment_has_structured_variable_operations({"memory_updates": [{}]})
    assert not service._story_increment_has_structured_variable_operations({"fact_updates": [{}]})

    normalized = service._normalize_stage2_output({
        "memory_updates": [1], "fact_updates": [2], "item_updates": ["Sword"],
        "variable_updates": [None, {}, {"path": "stats.hp", "op": "", "value": 3, "evidence": " e "}],
        "character_updates": [], "event_updates": [3], "snapshot_comment": " c ",
    })
    assert normalized["variable_updates"] == [{"op": "set", "path": "stats.hp", "value": 3, "evidence": "e"}]
    assert service._normalize_snapshot_operations("bad") == []
    operations = service._normalize_snapshot_operations([None, {}, {"path": "a.b", "op": "add", "value": 2}, {"path": "x", "op": "remove"}])
    assert len(operations) == 2

    state = {"a": {"b": 1}, "remove": {"x": 1}, "bad": 1}
    service._apply_operations_to_full_state(full_state=state, operations=[
        {}, {"op": "add", "path": "a.b", "value": 2}, {"op": "add", "path": "a.c", "value": "x"},
        {"op": "set", "path": "bad.child", "value": 4}, {"op": "remove", "path": "remove.x"},
        {"op": "remove", "path": "missing.child"},
    ])
    assert state["a"]["b"] == 3 and state["a"]["c"] == "x"
    assert state["bad"] == {"child": 4} and state["remove"] == {}
    service._set_nested_path(state, "", 1)
    service._add_nested_path(state, "", 1)
    service._remove_nested_path(state, "",)


def test_merge_and_misc_helpers_cover_empty_duplicate_and_file_paths(tmp_path):
    service = StoryProjectService()
    merged = service._merge_text_or_mapping_lists(["A", {"x": 1}, None], ["A", {"x": 1}, {"y": 2}])
    assert merged == ["A", {"x": 1}, {"y": 2}]
    item_merged = service._merge_item_update_lists(
        [{"item": "Sword", "status": "old", "tags": ["a"]}],
        [{"name": "sword", "status": "new", "tags": ["a", "b"], "owner": "Hero"}, {}],
    )
    assert item_merged[0]["status"] == "new" and item_merged[0]["tags"] == ["a", "b"]
    assert service._clean_increment_text(" a   b ") == "a b"
    assert service._character_template_key_from_title("Custom Title", 2) == "custom_title"
    assert service._character_template_key_from_title("!!!", 2) == "section_2"

    thread = service._renderable_foreshadow_thread("key", {"callbacks": ["bad", {"summary": "latest", "evidence": "proof"}]})
    assert thread == {"id": "key", "status": "open", "summary": "latest", "evidence": "proof"}
    thread2 = service._renderable_foreshadow_thread("key", {"id": "id", "status": "", "planted_at": {"summary": "start"}})
    assert thread2["status"] == "open" and thread2["summary"] == "start"
    assert service._score_foreshadow_thread(thread, set()) == 0
    assert service._score_foreshadow_thread(thread, {"latest", "LATEST", ""}) >= 3

    path = tmp_path / "data.json"
    assert service._read_json(path) == {}
    path.write_text("bad", encoding="utf-8")
    assert service._read_json(path) == {}
    path.write_text("[]", encoding="utf-8")
    assert service._read_json(path) == {}
    path.write_text(json.dumps({"a": 1}), encoding="utf-8")
    assert service._read_json(path) == {"a": 1}

    assert service._infer_character_summary(update={"role": "hero", "appearance": "tall"}, cast_state={})
    assert service._infer_character_summary(update={"changes": ["changed"]}, cast_state={"status": "ok", "emotion": ""})
