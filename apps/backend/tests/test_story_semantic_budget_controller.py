from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from services.story_semantic_budget_controller import (
    SemanticBudgetController,
    SemanticBudgetRequest,
    automatic_scene_count,
    automatic_scene_revision_limit,
    contextual_quality_issues,
    dynamic_scene_budget,
    initial_scene_budgets,
    mechanical_issues,
    parse_scene_plan,
    parse_story_plan,
    planning_messages,
    revision_quality_gate_issues,
    scene_contract_quality_issues,
    scene_plan_quality_issues,
    within_run_model_reference,
)
from services.story_semantic_budget_context import read_scene_constraint_context


def _plan(scene_count: int) -> str:
    return json.dumps(
        {
            "shortTermGoalOutcome": {
                "state": "partial",
                "description": "the current goal advances but remains unfinished",
            },
            "primaryHazard": None,
            "persistentClue": None,
            "abilityLimitAndCost": None,
            "scenes": [
                {
                    "title": f"scene-{index}",
                    "purpose": f"advance causal step {index}",
                    "development": f"act-{index}",
                    "exitHook": f"next-{index}",
                    "hazardRef": None,
                    "hazardRole": "none",
                    "clueRef": None,
                    "clueRole": "none",
                    "abilityRef": None,
                    "weight": 1.0,
                }
                for index in range(1, scene_count + 1)
            ]
        }
    )


def _prose(count: int, seed: int = 0) -> str:
    assert count >= 1
    start = seed * 1500
    body = "".join(chr(0x4E00 + ((start + index) % 19000)) for index in range(count - 1))
    return body + "。"


def _prose_with_prefix(prefix: str, count: int, seed: int = 0) -> str:
    assert len(prefix) < count
    return prefix + _prose(count - len(prefix), seed)


def _perspective_prose(pronoun: str, seed: int = 0, paragraphs: int = 6) -> str:
    return "\n\n".join(
        f"{pronoun}{_prose(75, seed + index)}" for index in range(paragraphs)
    )


class FakeAdapter:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[Dict[str, Any]] = []

    async def complete(
        self,
        *,
        messages: list[Dict[str, str]],
        purpose: str,
        metadata: Dict[str, Any],
    ) -> str:
        self.calls.append({"messages": messages, "purpose": purpose, "metadata": metadata})
        if not self.responses:
            raise AssertionError("fake adapter ran out of responses")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return str(response(purpose, metadata))
        return str(response)


def _request(**overrides: Any) -> SemanticBudgetRequest:
    values = {
        "product_target_word_count": 3000,
        "user_task": "continue the chapter with one new danger and a causal resolution",
        "source_context": "existing chapter ending.",
        "constraint_context": "keep restrained prose and causal action",
    }
    values.update(overrides)
    return SemanticBudgetRequest(**values)


@pytest.mark.parametrize(
    ("target", "expected_scene_count"),
    [(1500, 2), (3000, 4), (5000, 4), (7000, 5)],
)
def test_scene_mapping_and_initial_budget_sum(target: int, expected_scene_count: int) -> None:
    assert automatic_scene_count(target) == expected_scene_count
    scenes = parse_scene_plan(_plan(expected_scene_count), expected_scene_count)
    budgets = initial_scene_budgets(target, scenes)
    average = target / expected_scene_count

    assert len(budgets) == expected_scene_count
    assert sum(budgets) == target
    assert all(max(220, round(average * 0.80)) <= item <= round(average * 1.25) for item in budgets)


def test_planning_requires_a_concrete_exit_hook() -> None:
    messages = planning_messages(_request(), 4)
    prompt = "\n".join(message["content"] for message in messages)
    user_prompt = messages[1]["content"]
    example = user_prompt.split("JSON 格式：", 1)[1].split("\nexitHook", 1)[0]

    assert "正文末尾实际发生" in prompt
    assert "不得写成规划说明" in prompt
    assert "场景切分不代表引入新设定" in prompt
    assert "只保留一个持久线索" in prompt
    assert "不得让人物凭空" in prompt
    assert "不靠堆叠新名词或支线" in prompt
    assert "正在执行的短期目标" in prompt
    assert "优先关联前文已有伏笔" in prompt
    assert "一个主要危险源" in prompt
    assert "不得为交付线索另开" in prompt
    assert "不为凑篇幅新增外部冲突" in prompt
    assert len(parse_scene_plan(example, 4)) == 4


@pytest.mark.parametrize("target", [900, 1500, 3000, 5000, 7000])
def test_automatic_revision_limit_never_exceeds_two(target: int) -> None:
    assert automatic_scene_revision_limit(target) == 2


def test_automatic_1500_path_converges_with_two_revisions_under_large_model_gain() -> None:
    def respond(purpose: str, metadata: Dict[str, Any]) -> str:
        if purpose == "semantic_budget_plan":
            return _plan(int(metadata["sceneCount"]))
        if purpose == "semantic_budget_revision":
            return _prose(int(metadata["desiredWordCount"]), int(metadata["scene"]) + 10)
        return _prose(1400, int(metadata["scene"]))

    adapter = FakeAdapter([respond] * 8)

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(
                product_target_word_count=1500,
                maximum_scene_revisions=2,
            ),
            adapter,
        )
    )

    assert result.completed
    assert result.within_acceptance
    assert result.revision_attempts <= 2


def test_extreme_scene_weights_are_clamped_and_budget_still_sums_to_target() -> None:
    payload = json.loads(_plan(4))
    payload["scenes"][0]["weight"] = -100
    payload["scenes"][1]["weight"] = 100
    scenes = parse_scene_plan(json.dumps(payload), 4)
    budgets = initial_scene_budgets(3000, scenes)

    assert [scene["weight"] for scene in scenes[:2]] == [0.8, 1.2]
    assert sum(budgets) == 3000
    assert all(600 <= item <= 938 for item in budgets)


def test_duplicate_causal_steps_are_rejected() -> None:
    payload = json.loads(_plan(3))
    payload["scenes"][1]["purpose"] = payload["scenes"][0]["purpose"]
    payload["scenes"][1]["development"] = payload["scenes"][0]["development"]

    with pytest.raises(ValueError, match="duplicates"):
        parse_scene_plan(json.dumps(payload), 3)


@pytest.mark.parametrize(
    "description",
    [
        "岩壁坍塌、腐蚀瘴气以及妖兽袭击",
    ],
)
def test_primary_hazard_cannot_bundle_independent_dangers(description: str) -> None:
    payload = json.loads(_plan(2))
    payload["primaryHazard"] = {
        "id": "hazard-main",
        "description": description,
        "outcome": "escaped",
    }
    payload["scenes"][0].update(
        {"hazardRef": "hazard-main", "hazardRole": "foreshadow"}
    )
    payload["scenes"][1].update(
        {"hazardRef": "hazard-main", "hazardRole": "pressure"}
    )

    with pytest.raises(ValueError, match="one atomic item"):
        parse_scene_plan(json.dumps(payload, ensure_ascii=False), 2)


def test_primary_hazard_allows_clauses_describing_one_danger() -> None:
    payload = json.loads(_plan(2))
    payload["primaryHazard"] = {
        "id": "hazard-main",
        "description": (
            "青岩谷深处因五行灵气失衡形成的土灵气漩涡，"
            "吸入者经脉会被土灵气撑裂，周围其他属性灵气几乎被排空。"
        ),
        "outcome": "escaped",
    }
    payload["scenes"][0].update(
        {"hazardRef": "hazard-main", "hazardRole": "foreshadow"}
    )
    payload["scenes"][1].update(
        {"hazardRef": "hazard-main", "hazardRole": "pressure"}
    )

    scenes = parse_scene_plan(json.dumps(payload, ensure_ascii=False), 2)

    assert scenes[0]["hazardRef"] == "hazard-main"


@pytest.mark.parametrize(
    ("action", "development"),
    [
        ("按时辰比例重新分配体内五行灵气", "沈渡借整体平衡脱身。"),
        ("让五行灵气形成短暂的整体平衡", "沈渡运用自创的平衡法脱身。"),
    ],
)
def test_limited_ability_plan_rejects_unestablished_mastery(
    action: str,
    development: str,
) -> None:
    payload = json.loads(_plan(2))
    payload["abilityLimitAndCost"] = {
        "id": "ability-main",
        "purpose": "escape",
        "action": action,
        "limit": "只能暂时脱身，不能解决危险",
        "cost": "经脉受损并暂时失去行动能力",
    }
    payload["scenes"][0].update(
        {
            "development": development,
            "abilityRef": "ability-main",
        }
    )

    with pytest.raises(ValueError, match="unestablished mastery"):
        parse_scene_plan(json.dumps(payload, ensure_ascii=False), 2)


def test_persistent_clue_must_remain_evidence_only() -> None:
    payload = json.loads(_plan(2))
    payload["persistentClue"] = {
        "id": "clue-main",
        "description": "五行灵石可用作稳定灵气的媒介",
        "sourceRef": "existing-thread",
        "function": "evidence_only",
    }
    payload["scenes"][0].update(
        {"clueRef": "clue-main", "clueRole": "seed"}
    )
    payload["scenes"][1].update(
        {"clueRef": "clue-main", "clueRole": "reveal"}
    )

    with pytest.raises(ValueError, match="evidence only"):
        parse_scene_plan(json.dumps(payload, ensure_ascii=False), 2)


def test_plan_validation_reports_all_repairable_contract_errors() -> None:
    payload = json.loads(_plan(2))
    payload["persistentClue"] = {
        "id": "clue-main",
        "description": "五行灵石可用作稳定灵气的媒介",
        "sourceRef": "existing-thread",
        "function": "evidence_only",
    }
    payload["abilityLimitAndCost"] = {
        "id": "ability-main",
        "purpose": "escape",
        "action": "按时辰比例重新分配体内五行灵气",
        "limit": "只能暂时脱身，不能解决危险",
        "cost": "经脉受损并暂时失去行动能力",
    }
    payload["scenes"][0].update(
        {
            "development": "沈渡运用自创的平衡法脱身。",
            "clueRef": "clue-main",
            "clueRole": "seed",
            "abilityRef": "ability-main",
        }
    )
    payload["scenes"][1].update(
        {"clueRef": "clue-main", "clueRole": "reveal"}
    )

    with pytest.raises(ValueError) as caught:
        parse_scene_plan(json.dumps(payload, ensure_ascii=False), 2)

    reason = str(caught.value)
    assert "persistentClue" in reason
    assert "abilityLimitAndCost" in reason
    assert "scene 1 ability development" in reason


def test_scene_plan_cannot_use_an_unreferenced_ability() -> None:
    payload = json.loads(_plan(2))
    payload["abilityLimitAndCost"] = {
        "id": "ability-main",
        "purpose": "escape",
        "action": "使五行灵气形成短暂的整体平衡",
        "limit": "只能暂时脱身，不能解决危险",
        "cost": "经脉受损并暂时失去行动能力",
    }
    payload["scenes"][0]["development"] = "沈渡尝试运转五行灵气维持平衡。"
    payload["scenes"][1]["abilityRef"] = "ability-main"

    with pytest.raises(ValueError, match="without abilityRef"):
        parse_scene_plan(json.dumps(payload, ensure_ascii=False), 2)


def test_scene_plan_rejects_sequential_multi_attribute_control() -> None:
    payload = json.loads(_plan(2))
    payload["abilityLimitAndCost"] = {
        "id": "ability-main",
        "purpose": "escape",
        "action": "依次吸收不同属性灵气来暂时平衡体内暴动。",
        "limit": "只能勉强站稳，无法化解外界灵气潮汐。",
        "cost": "经脉受损，短时间无法再次运转灵气。",
    }
    payload["scenes"][0].update(
        {
            "development": "沈渡轮流吸收不同属性灵气以稳住身形。",
            "abilityRef": "ability-main",
        }
    )

    with pytest.raises(ValueError, match="unestablished mastery"):
        parse_scene_plan(json.dumps(payload, ensure_ascii=False), 2)


def test_scene_plan_cannot_stabilize_an_unreferenced_ability() -> None:
    payload = json.loads(_plan(2))
    payload["abilityLimitAndCost"] = {
        "id": "ability-main",
        "purpose": "escape",
        "action": "使五行灵气形成短暂的整体平衡",
        "limit": "只能暂时脱身，不能解决危险",
        "cost": "经脉受损并暂时失去行动能力",
    }
    payload["scenes"][0]["development"] = (
        "沈渡试图稳住体内翻涌的灵气，却发现冲突愈发剧烈。"
    )
    payload["scenes"][1]["abilityRef"] = "ability-main"

    with pytest.raises(ValueError, match="without abilityRef"):
        parse_scene_plan(json.dumps(payload, ensure_ascii=False), 2)


def test_dynamic_budget_moves_opposite_to_previous_scene_deviation() -> None:
    initial = [750, 750, 750, 750]

    after_overwrite = dynamic_scene_budget(target=3000, written=1000, initial=initial, index=1)
    after_underwrite = dynamic_scene_budget(target=3000, written=500, initial=initial, index=1)

    assert after_overwrite < initial[1]
    assert after_underwrite > initial[1]
    assert dynamic_scene_budget(target=3000, written=5000, initial=initial, index=3) == 562


def test_within_run_reference_uses_bounded_recent_gain() -> None:
    reference, gain = within_run_model_reference(800, [1.2, 4.0, 1.4])
    assert gain == 1.4
    assert reference == 571

    bounded_reference, bounded_gain = within_run_model_reference(800, [9.0, 9.0, 9.0])
    assert bounded_gain == 4.0
    assert bounded_reference == 200


def test_extreme_compression_is_not_accepted_just_for_being_closer() -> None:
    adapter = FakeAdapter(
        [
            _plan(4),
            _prose(2200, 0),
            _prose(400, 10),
            _prose(833, 1),
            _prose(833, 2),
            _prose(834, 3),
        ]
    )

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(maximum_scene_revisions=1),
            adapter,
        )
    )

    first_scene = result.scenes[0]
    assert first_scene["revisionAccepted"] is False
    assert "revision_below_scene_floor" in first_scene["revisionQualityIssues"]
    assert "revision_extreme_compression" in first_scene["revisionQualityIssues"]
    assert first_scene["acceptedWordCount"] == 2200


def test_substantial_revision_above_sixty_percent_clears_length_gates() -> None:
    issues = revision_quality_gate_issues(
        original=_prose(1200, 0),
        revision=_prose(650, 10),
        original_count=1200,
        revision_count=650,
        desired=1000,
        scene={
            "development": "perform distinct causal action",
            "exitHook": "lead naturally to the next scene",
        },
    )

    assert "revision_below_scene_floor" not in issues
    assert "revision_extreme_compression" not in issues


def test_verified_plan_coverage_allows_a_concise_revision() -> None:
    development = (
        "沈渡跌入浅坑后左腿受伤，以土灵气稳身、水灵气止血、"
        "木灵气调和火气并用金灵气抵挡吸扯。"
    )
    exit_hook = "他发现五行石碑并记下位置，带伤离谷，决定准备充分后再来。"
    original = development + _prose(900, 1) + exit_hook
    revision = development + _prose(300, 4) + exit_hook

    issues = revision_quality_gate_issues(
        original=original,
        revision=revision,
        original_count=1884,
        revision_count=470,
        desired=1056,
        scene={"development": development, "exitHook": exit_hook},
    )

    assert issues == []


def test_faithful_plan_paraphrase_clears_preservation_gate() -> None:
    development = (
        "沈渡继续深入，地面裂开后有三根墨绿色藤蔓封死退路。危急关头，他强行运转"
        "青木诀，激发丹田种子勾连土灵气，让木土二气形成平衡并弹开藤蔓。他随后散出"
        "木灵气模拟藤蔓气息，在裂缝中发现带有残缺阵纹的幽蓝玉质碎片，判断谷中异常"
        "与碎片有关，收起碎片后看见一条新路径。"
    )
    exit_hook = (
        "沈渡带着玉质碎片和经脉的刺痛，沿着新出现的路径继续前行，心中既后怕又想到"
        "碎片或许藏着改变命运的机会，而横断山脉远比预想危险。"
    )
    revision = (
        "沈渡向谷内走去，脚下忽然震动，裂开的地面射出数根带刺藤蔓，将他逼到岩壁前。"
        "他运转青木诀，以丹田种子牵引沉稳土气，让木、土二气在经脉中短暂交汇。柔韧"
        "气场弹开藤蔓后，他翻到巨石后散出木灵气，模仿藤蔓气息使其缩回裂缝。经脉刺痛"
        "未消，他又在裂缝边发现一截幽蓝玉片，上面刻着半幅阵纹。他用镰刀撬出碎片，"
        "确认它与暴走木灵气共鸣，可能是深入探查的关键。雾气渐淡，新路随之显现。沈渡"
        "带着碎片和伤痛沿路前行；虽然后怕，他仍想到这件东西也许能改变命运，而山脉"
        "中的凶险显然超过预想。"
    )

    issues = revision_quality_gate_issues(
        original=development + exit_hook,
        revision=revision,
        original_count=1777,
        revision_count=637,
        desired=930,
        scene={"development": development, "exitHook": exit_hook},
    )

    assert "revision_development_loss" not in issues
    assert "revision_exit_hook_loss" not in issues


def test_original_scene_missing_exit_hook_requires_plan_repair() -> None:
    development = "沈渡识破藤蔓陷阱，以火灵气灼开束缚并用水灵气护住伤口。"
    exit_hook = "他标记洞口位置，决定先回去养伤，随后沿原路折返并遇上落雨。"
    original = development + _prose(650, 1)

    issues = scene_plan_quality_issues(
        original,
        {"development": development, "exitHook": exit_hook},
    )

    assert "scene_development_loss" not in issues
    assert "scene_exit_hook_loss" in issues


def test_original_scene_accepts_a_faithful_exit_hook_paraphrase() -> None:
    exit_hook = "危险逼近，他必须立即做出反应，为下一场景的自救铺垫。"
    original = (
        _prose(200, 40)
        + "脚下传来一阵震动。沈渡猛地睁开眼，体内的平衡瞬间溃散，灵气反噬让经脉一阵刺痛。"
        "他必须立刻做出反应——不管是逃是躲，都不能留在原地等死。"
        "那道兽吼的主人离他越来越近，他几乎没有时间犹豫，只能咬牙做出选择。"
    )

    issues = scene_plan_quality_issues(
        original,
        {"development": "", "exitHook": exit_hook},
    )

    assert "scene_exit_hook_loss" not in issues


def test_original_scene_accepts_a_faithful_development_paraphrase() -> None:
    development = (
        "沈渡按地图进入青岩谷外围，发现晨雾异常浓密且不散，灵气流动紊乱，"
        "木灵气中夹杂不和谐的波动；他观察到草木枯萎方向一致，动物如鸟雀反常安静，"
        "心理警觉但试图保持冷静，继续向谷内移动。"
    )
    original = (
        "沈渡沿着地图标注的土路走了约莫两个时辰，晨雾非但没有散去，反而愈发浓密。"
        "原本只是薄纱般的白雾，此刻竟稠得像化不开的米汤，几步之外便只能看见模糊的轮廓。"
        "他停下脚步，习惯性地调动那点微薄的灵力去感知周围——这是他每晚练习的本能。"
        "然而指尖刚一探出，便皱起了眉头：木灵气依然充盈，却像一根绷紧的琴弦，"
        "内部隐隐震颤着某种不和谐的波动。沈渡蹲下身，指尖拂过路边的野草。"
        "草叶枯黄，但枯萎的方向出奇地一致，全都朝着谷内倾斜，仿佛被什么力量齐齐压倒。"
        "他抬头望向不远处的灌木丛，枝叶同样如此，甚至有几株青松的针叶开始泛起褐红。"
        "更令他不安的是寂静。横断山脉外围本该鸟鸣不断，虫声不绝，此刻却只剩下"
        "风穿过雾气的呜咽，连一只飞鸟的影子都看不见。沈渡握紧腰间短镰刀的刀柄，"
        "掌心渗出薄汗，却仍强迫自己稳住呼吸，继续朝地图上标记的青岩谷方向走去。"
        "雾气浓到几乎遮蔽了所有视觉参照。沈渡只能依靠脚下土路的质感和偶尔露出的岩石"
        "来辨别方向。他摸出怀中那卷地图，却发现兽皮边缘已经微微潮软，上面的墨迹"
        "在湿气中有些晕开。就在他试图辨认地图上一处模糊的标记时，一阵低沉的嗡鸣声"
        "毫无预兆地钻进耳朵。那声音不像是从某个具体方向传来，反而像是整片雾气在共振。"
        "沈渡猛地抬头，手已按在短镰刀的柄上。他转着身子警戒地扫视四周，但视野里只有"
        "翻涌的白雾。嗡鸣声却在他转身的瞬间停了，四周重新陷入死寂。他盯着雾气中的"
        "一个方向，深吸一口气，攥紧短镰刀，迈开脚步，朝那片未知的浓雾中走去。"
    )

    issues = scene_plan_quality_issues(
        original,
        {"development": development, "exitHook": ""},
    )

    assert "scene_development_loss" not in issues


def test_length_compliant_scene_repairs_a_missing_exit_hook() -> None:
    development = "沈渡识破藤蔓陷阱，以火灵气灼开束缚并用水灵气护住伤口。"
    exit_hook = "他标记洞口位置，决定先回去养伤，随后沿原路折返并遇上落雨。"
    plan = json.loads(_plan(2))
    plan["scenes"][0]["development"] = development
    plan["scenes"][0]["exitHook"] = exit_hook

    original = _prose_with_prefix(development, 750, 1)
    revision = (
        development
        + _prose(750 - len(development) - len(exit_hook), 2)
        + exit_hook
    )
    adapter = FakeAdapter(
        [
            json.dumps(plan, ensure_ascii=False),
            original,
            revision,
            _prose(750, 10),
        ]
    )

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(
                product_target_word_count=1500,
                scene_count=2,
                maximum_scene_revisions=1,
            ),
            adapter,
        )
    )

    first_scene = result.scenes[0]
    assert result.completed
    assert result.provider_calls == 4
    assert first_scene["originalWordCount"] == 750
    assert first_scene["lengthRevisionNeeded"] is False
    assert "scene_exit_hook_loss" in first_scene["originalMechanicalIssues"]
    assert first_scene["revisionTriggered"] is True
    assert first_scene["revisionAccepted"] is True
    assert first_scene["revisionQualityIssues"] == []
    assert first_scene["acceptedWordCount"] == 750
    assert exit_hook in result.text


def test_exit_hook_coverage_must_be_retained_near_the_revision_ending() -> None:
    development = "沈渡识破陷阱并以五行灵气完成自救。"
    exit_hook = "他记下石碑位置，带伤离谷，决定准备充分后再来。"
    original = development + _prose(500, 1) + exit_hook
    revision = exit_hook + _prose(500, 4) + "沈渡最终留在原地，没有离开。"

    issues = revision_quality_gate_issues(
        original=original,
        revision=revision,
        original_count=700,
        revision_count=550,
        desired=600,
        scene={"development": development, "exitHook": exit_hook},
    )

    assert "revision_exit_hook_loss" in issues


def test_exit_hook_paraphrase_with_strong_character_coverage_is_accepted() -> None:
    development = (
        "沈渡缓过气来检查地底溶洞，发现岩壁上的灰白石块能吸引残余灵气，"
        "便将石块收入怀中。"
    )
    exit_hook = (
        "沈渡将石块收好，忍着经脉疼痛爬向裂缝通道，"
        "用短镰试探深度和稳固性。"
    )
    paraphrased_ending = (
        "他拍了拍怀里的石块，确认它没有掉落，然后咬着牙，一步一步挪向那条裂缝通道。"
        "沈渡从腰间抽出那把短镰，伸直手臂，将镰刀探入裂缝深处，左右刮了刮岩壁，"
        "又往下戳了戳地面。镰刀触到的是坚硬的岩石，地面也算平整，没有明显的松软或"
        "塌陷迹象，说明这条通道至少入口处是稳固的。"
    )
    text = development + _prose(500, 12) + paraphrased_ending

    issues = scene_plan_quality_issues(
        text,
        {"development": development, "exitHook": exit_hook},
    )

    assert "scene_exit_hook_loss" not in issues


@pytest.mark.parametrize(
    ("revision_prefix", "expected_issue"),
    [
        (
            "峡谷上方忽然滚落三块黑石封住退路，阴影里的第二头岩甲蜥正弓背蓄势准备突袭。",
            "revision_development_loss",
        ),
        (
            "沈渡挥动青铜短镰刀劈砍岩甲蜥，刀刃卷曲后改用赤焰火苗，火苗撞上土黄鳞片立即熄灭。",
            "revision_exit_hook_loss",
        ),
    ],
)
def test_revision_must_preserve_planned_development_and_exit_hook(
    revision_prefix: str,
    expected_issue: str,
) -> None:
    development = (
        "沈渡挥动青铜短镰刀劈砍岩甲蜥，刀刃卷曲后改用赤焰火苗，"
        "火苗撞上土黄鳞片立即熄灭。"
    )
    exit_hook = (
        "峡谷上方忽然滚落三块黑石封住退路，"
        "阴影里的第二头岩甲蜥正弓背蓄势准备突袭。"
    )
    plan = json.loads(_plan(4))
    plan["scenes"][0]["development"] = development
    plan["scenes"][0]["exitHook"] = exit_hook
    original = _prose_with_prefix(development + exit_hook, 2200, 20)
    revision = _prose_with_prefix(revision_prefix, 750, 30)
    adapter = FakeAdapter(
        [
            json.dumps(plan, ensure_ascii=False),
            original,
            revision,
            _prose(750, 1),
            _prose(750, 2),
            _prose(750, 3),
        ]
    )

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(maximum_scene_revisions=1),
            adapter,
        )
    )

    first_scene = result.scenes[0]
    assert first_scene["revisionAccepted"] is False
    assert expected_issue in first_scene["revisionQualityIssues"]
    assert first_scene["acceptedWordCount"] == 2200


@pytest.mark.parametrize("ending", ["。”", "！’", "？”", "？』", "。)", "。】"])
def test_sentence_ending_allows_trailing_closing_delimiters(ending: str) -> None:
    assert "incomplete_ending" not in mechanical_issues(f"正文{ending}")


def test_closing_delimiter_does_not_replace_sentence_punctuation() -> None:
    assert "incomplete_ending" in mechanical_issues("正文”")


def test_contextual_quality_detects_perspective_shift_and_unrequested_explicit_content() -> None:
    source = _perspective_prose("他", 20, paragraphs=10)
    shifted = _perspective_prose("你", 40)
    explicit = f"{_perspective_prose('他', 60)}阴茎。"

    assert "narrative_perspective_shift" in contextual_quality_issues(
        shifted,
        source_context=source,
    )
    assert "unexpected_explicit_content" in contextual_quality_issues(
        explicit,
        source_context=source,
        user_task="遭遇山中危险",
    )
    assert "unexpected_explicit_content" not in contextual_quality_issues(
        explicit,
        source_context=source,
        user_task="续写明确的阴茎性爱场景",
    )


def test_contextual_quality_detects_single_newline_second_person_prose() -> None:
    source = _perspective_prose("他", 20, paragraphs=10)
    shifted = "\n".join(
        [
            "你走进山雾。",
            "你察觉灵气正在失衡。",
            "寒意沿着你的经脉蔓延。",
            "你只能停下来重新调整呼吸。",
        ]
    )

    assert "narrative_perspective_shift" in contextual_quality_issues(
        shifted,
        source_context=source,
    )


def test_short_sound_effect_repetition_is_not_treated_as_duplicate_padding() -> None:
    text = "嗡——\n\n沈渡按住发烫的玉片。\n\n嗡——\n\n他抬头看向石门。"

    issues = contextual_quality_issues(
        text,
        source_context="沈渡走入石室。",
        user_task="让异常声音逐步逼近。",
    )

    assert "duplicate_paragraph" not in issues


def test_perspective_shift_uses_a_local_revision() -> None:
    source = _perspective_prose("他", 20, paragraphs=10)
    shifted = _perspective_prose("你", 40)
    corrected = _perspective_prose("他", 50)
    adapter = FakeAdapter(
        [
            _plan(3),
            shifted,
            corrected,
            _perspective_prose("他", 70, paragraphs=7),
            _perspective_prose("他", 80, paragraphs=7),
        ]
    )

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(
                product_target_word_count=1500,
                source_context=source,
                scene_count=3,
            ),
            adapter,
        )
    )

    assert result.completed
    assert result.revision_attempts == 1
    assert result.revision_acceptances == 1
    assert "narrative_perspective_shift" in result.scenes[0]["originalMechanicalIssues"]
    assert result.scenes[0]["mechanicalIssues"] == []
    revision_prompt = adapter.calls[2]["messages"][1]["content"]
    assert "narrative_perspective_shift" in revision_prompt


def test_happy_path_generates_four_scenes_and_completes_without_writes() -> None:
    adapter = FakeAdapter(
        [
            _plan(4),
            _prose(750, 0),
            _prose(750, 1),
            _prose(750, 2),
            _prose(750, 3),
        ]
    )

    result = asyncio.run(SemanticBudgetController().generate(_request(), adapter))

    assert result.completed
    assert result.generated_word_count == 3000
    assert result.within_acceptance
    assert result.provider_calls == 5
    assert result.revision_attempts == 0
    assert len(result.scenes) == 4
    assert adapter.responses == []
    assert result.events[0]["state"] == "PLANNING"
    assert result.events[-1]["state"] == "COMPLETED"


def test_invalid_plan_gets_one_structure_repair() -> None:
    adapter = FakeAdapter(
        [
            "not json",
            _plan(4),
            _prose(750, 0),
            _prose(750, 1),
            _prose(750, 2),
            _prose(750, 3),
        ]
    )

    result = asyncio.run(SemanticBudgetController().generate(_request(), adapter))

    assert result.completed
    assert result.provider_calls == 6
    assert [call["purpose"] for call in adapter.calls[:2]] == [
        "semantic_budget_plan",
        "semantic_budget_plan_repair",
    ]


def test_unstructured_stacked_hazard_plan_fails_after_one_plan_repair() -> None:
    stacked_plan = json.dumps(
        {
            "scenes": [
                {
                    "title": "异常",
                    "purpose": "发现环境异常和矿石线索",
                    "development": (
                        "沈渡发现灵气紊乱和暗红矿石，决定进入山沟确认来源。"
                    ),
                    "exitHook": "沈渡沿山沟继续探查。",
                    "weight": 1.0,
                },
                {
                    "title": "复合危机",
                    "purpose": "遭遇危险并运用五行灵气自救",
                    "development": (
                        "岩壁坍塌后涌出腐蚀瘴气，同时一头妖兽冲来。沈渡同时调动五种灵气，"
                        "击退妖兽并逃出瘴气，又发现矿石会指向地下遗迹。"
                    ),
                    "exitHook": "沈渡记住遗迹位置，决定养伤后再来。",
                    "weight": 1.0,
                },
            ]
        },
        ensure_ascii=False,
    )
    adapter = FakeAdapter([stacked_plan, stacked_plan])

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(product_target_word_count=1500, scene_count=2),
            adapter,
        )
    )

    assert result.status == "failed_plan"
    assert result.provider_calls == 2
    assert result.scenes == []
    assert result.revision_attempts == 0
    assert [call["purpose"] for call in adapter.calls] == [
        "semantic_budget_plan",
        "semantic_budget_plan_repair",
    ]
    assert result.error["stage"] == "planning_repair"


def test_plan_repair_does_not_consume_or_expand_scene_revision_limit() -> None:
    def respond(purpose: str, metadata: Dict[str, Any]) -> str:
        desired = int(metadata["desiredWordCount"])
        scene = int(metadata["scene"])
        if purpose == "semantic_budget_revision":
            return _prose(desired, scene + 20)
        prose = _prose(desired, scene)
        return f"<content>{prose}</content>" if scene <= 2 else prose

    adapter = FakeAdapter(["not json", _plan(4), *([respond] * 6)])

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(maximum_scene_revisions=2),
            adapter,
        )
    )

    purposes = [call["purpose"] for call in adapter.calls]
    assert result.completed
    assert result.provider_calls == 8
    assert purposes.count("semantic_budget_plan_repair") == 1
    assert purposes.count("semantic_budget_revision") == 2
    assert result.revision_attempts == 2
    assert result.revision_acceptances == 2


def test_single_hazard_and_linked_clue_plan_generates_without_plan_repair() -> None:
    payload = json.loads(_plan(2))
    payload["shortTermGoalOutcome"] = {
        "state": "delayed",
        "description": "沈渡因受伤延后寻找青岩草，先退到安全处养伤。",
    }
    payload["primaryHazard"] = {
        "id": "hazard-main",
        "description": "山沟中松动的岩壁突然坍塌",
        "outcome": "escaped",
    }
    payload["persistentClue"] = {
        "id": "clue-main",
        "description": "坍塌断面露出的暗红矿石碎片",
        "sourceRef": "hazard-main",
        "function": "evidence_only",
    }
    payload["abilityLimitAndCost"] = {
        "id": "ability-main",
        "purpose": "escape",
        "action": "以少量土灵气稳住落脚点",
        "limit": "只能维持一次呼吸，无法反击或清除危险",
        "cost": "经脉刺痛，脱险后短时无力",
    }
    first = payload["scenes"][0]
    first.update(
        {
            "purpose": "让环境异常发展为同一个可见危险",
            "development": "沈渡察觉岩壁松动，改走山沟边缘。",
            "exitHook": "碎石落下，他被迫贴近岩壁寻找落脚处。",
            "hazardRef": "hazard-main",
            "hazardRole": "foreshadow",
            "clueRef": "clue-main",
            "clueRole": "seed",
        }
    )
    second = payload["scenes"][1]
    second.update(
        {
            "purpose": "让沈渡有限自救并承担后果",
            "development": "岩壁坍塌时，沈渡稳住一步后撤出山沟并受伤。",
            "exitHook": "他拾起断面掉落的矿石碎片，记下位置后离开。",
            "hazardRef": "hazard-main",
            "hazardRole": "pressure",
            "clueRef": "clue-main",
            "clueRole": "reveal",
            "abilityRef": "ability-main",
        }
    )
    scene_texts = [
        _prose_with_prefix(first["development"], 700, 20)[:-1]
        + first["exitHook"],
        _prose_with_prefix(second["development"], 700, 30)[:-1]
        + second["exitHook"],
    ]
    adapter = FakeAdapter(
        [json.dumps(payload, ensure_ascii=False), *scene_texts]
    )

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(product_target_word_count=1500, scene_count=2),
            adapter,
        )
    )

    assert result.completed
    assert result.provider_calls == 3
    assert result.revision_attempts == 0
    assert [call["purpose"] for call in adapter.calls] == [
        "semantic_budget_plan",
        "semantic_budget_scene",
        "semantic_budget_scene",
    ]
    assert result.plan_contract["primaryHazard"]["id"] == "hazard-main"
    assert result.plan_contract["persistentClue"]["sourceRef"] == "hazard-main"
    generation_prompt = adapter.calls[1]["messages"][1]["content"]
    assert "章级唯一约束" in generation_prompt
    assert '"purpose": "escape"' in generation_prompt


def test_scene_repairs_unestablished_per_element_ability_mastery() -> None:
    payload = json.loads(_plan(2))
    payload["abilityLimitAndCost"] = {
        "id": "ability-main",
        "purpose": "escape",
        "action": "使五行灵气形成短暂的整体平衡",
        "limit": "只能压住失控势头，不能分别控制各属性或击败危险",
        "cost": "右臂经脉受损并在数日内持续疼痛",
    }
    development = "沈渡只维持短暂的整体平衡，借机向安全处撤离。"
    exit_hook = "他的右臂失去知觉，只能带伤继续后退。"
    payload["scenes"][0].update(
        {
            "development": development,
            "exitHook": exit_hook,
            "abilityRef": "ability-main",
        }
    )
    original_detail = (
        "他同时调动五种灵气。木入肝经、水润肾脉、火归心络、"
        "金行肺腑、土沉脾胃，五股气流各自归位。"
    )
    original = (
        development
        + original_detail
        + _prose(700 - len(development) - len(original_detail) - len(exit_hook), 40)[:-1]
        + exit_hook
    )
    revised_detail = "五种灵气仍混作一团，他只勉强维持住整体平衡。"
    revision = (
        development
        + revised_detail
        + _prose(700 - len(development) - len(revised_detail) - len(exit_hook), 50)[:-1]
        + exit_hook
    )
    adapter = FakeAdapter(
        [
            json.dumps(payload, ensure_ascii=False),
            original,
            revision,
            _prose(800, 60),
        ]
    )

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(
                product_target_word_count=1500,
                scene_count=2,
                maximum_scene_revisions=1,
            ),
            adapter,
        )
    )

    first_scene = result.scenes[0]
    assert result.completed
    assert result.provider_calls == 4
    assert first_scene["originalMechanicalIssues"] == [
        "scene_ability_scope_expansion"
    ]
    assert first_scene["revisionTriggered"] is True
    assert first_scene["revisionAccepted"] is True
    assert first_scene["mechanicalIssues"] == []
    assert "木入肝经" not in result.text


def test_scene_repairs_active_ability_use_without_a_reference() -> None:
    payload = json.loads(_plan(2))
    payload["abilityLimitAndCost"] = {
        "id": "ability-main",
        "purpose": "escape",
        "action": "使五行灵气形成短暂的整体平衡",
        "limit": "只能暂时脱身，不能解决危险",
        "cost": "经脉受损并暂时失去行动能力",
    }
    payload["scenes"][1]["abilityRef"] = "ability-main"
    original_detail = "沈渡主动运转五行灵气，勉强维持住体内平衡。"
    original = original_detail + _prose(700 - len(original_detail), 120)
    revised_detail = "沈渡只感到经脉剧痛，没有能力再次主动引导灵气。"
    revision = revised_detail + _prose(700 - len(revised_detail), 130)
    adapter = FakeAdapter(
        [
            json.dumps(payload, ensure_ascii=False),
            original,
            revision,
            _prose(800, 140),
        ]
    )

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(
                product_target_word_count=1500,
                scene_count=2,
                maximum_scene_revisions=1,
            ),
            adapter,
        )
    )

    first_scene = result.scenes[0]
    assert result.completed
    assert result.provider_calls == 4
    assert first_scene["originalMechanicalIssues"] == [
        "scene_unreferenced_ability_use"
    ]
    assert first_scene["revisionTriggered"] is True
    assert first_scene["revisionAccepted"] is True
    assert "主动运转五行灵气" not in result.text


def test_scene_allows_an_unreferenced_ability_to_lose_control() -> None:
    text = (
        "沈渡试图稳住呼吸，但体内灵气开始不受控制地躁动，经脉刺痛加剧。"
    )
    scene = {"clueRef": None, "abilityRef": None}
    contract = {
        "persistentClue": None,
        "abilityLimitAndCost": {
            "id": "ability-main",
            "purpose": "escape",
            "action": "使五行灵气形成短暂的整体平衡",
            "limit": "只能暂时脱身，不能解决危险",
            "cost": "经脉受损并暂时失去行动能力",
        },
    }

    issues = scene_contract_quality_issues(text, scene, contract)

    assert "scene_unreferenced_ability_use" not in issues


def test_scene_repairs_repetition_of_the_previous_scene_ending() -> None:
    payload = json.loads(_plan(2))
    repeated_ending = (
        "漩涡的吸力把沈渡拽向中心，经脉传来撕裂般的剧痛，"
        "他只能咬紧牙关稳住翻腾的气血。"
    )
    first_development = "沈渡被突然爆发的灵力漩涡卷入。"
    payload["scenes"][0].update(
        {
            "development": first_development,
            "exitHook": repeated_ending,
        }
    )
    second_development = "沈渡顺着旋转方向找到短暂的平衡点。"
    second_exit_hook = "他带伤离开山谷，决定准备充分后再来。"
    payload["scenes"][1].update(
        {
            "development": second_development,
            "exitHook": second_exit_hook,
        }
    )
    first = (
        first_development
        + _prose(700 - len(first_development) - len(repeated_ending), 70)[:-1]
        + repeated_ending
    )
    repeated_second = (
        repeated_ending
        + second_development
        + _prose(700 - len(repeated_ending) - len(second_development) - len(second_exit_hook), 80)[:-1]
        + second_exit_hook
    )
    revised_second = (
        "他没有停留，立刻改变身体倾斜的方向。"
        + second_development
        + _prose(700 - 20 - len(second_development) - len(second_exit_hook), 90)[:-1]
        + second_exit_hook
    )
    adapter = FakeAdapter(
        [
            json.dumps(payload, ensure_ascii=False),
            first,
            repeated_second,
            revised_second,
        ]
    )

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(
                product_target_word_count=1500,
                scene_count=2,
                maximum_scene_revisions=1,
            ),
            adapter,
        )
    )

    second_scene = result.scenes[1]
    assert result.completed
    assert result.provider_calls == 4
    assert second_scene["originalMechanicalIssues"] == [
        "scene_context_repetition"
    ]
    assert second_scene["revisionTriggered"] is True
    assert second_scene["revisionAccepted"] is True
    assert "repeated_ngram" not in result.mechanical_issues


def test_scene_repairs_a_clue_that_becomes_an_ability_aid() -> None:
    payload = json.loads(_plan(2))
    payload["persistentClue"] = {
        "id": "clue-main",
        "description": "裂隙边缘带有五色纹路的矿石碎片",
        "sourceRef": "existing-thread",
        "function": "evidence_only",
    }
    payload["scenes"][0].update(
        {"clueRef": "clue-main", "clueRole": "seed"}
    )
    development = "沈渡观察矿石纹路并判断它来自裂隙深处。"
    exit_hook = "他收起矿石碎片，带伤离开山谷。"
    payload["scenes"][1].update(
        {
            "development": development,
            "exitHook": exit_hook,
            "clueRef": "clue-main",
            "clueRole": "reveal",
        }
    )
    original_detail = "他握住矿石，石中灵气立刻稳定了经脉并帮助他恢复行动。"
    original = (
        development
        + original_detail
        + _prose(700 - len(development) - len(original_detail) - len(exit_hook), 100)[:-1]
        + exit_hook
    )
    revised_detail = "他只记下纹路和裂隙方向，没有从矿石中获得力量。"
    revision = (
        development
        + revised_detail
        + _prose(700 - len(development) - len(revised_detail) - len(exit_hook), 110)[:-1]
        + exit_hook
    )
    adapter = FakeAdapter(
        [
            json.dumps(payload, ensure_ascii=False),
            _prose(700, 95),
            original,
            revision,
        ]
    )

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(
                product_target_word_count=1500,
                scene_count=2,
                maximum_scene_revisions=1,
            ),
            adapter,
        )
    )

    second_scene = result.scenes[1]
    assert result.completed
    assert result.provider_calls == 4
    assert second_scene["originalMechanicalIssues"] == [
        "scene_clue_utility_expansion"
    ]
    assert second_scene["revisionTriggered"] is True
    assert second_scene["revisionAccepted"] is True
    assert "帮助他恢复行动" not in result.text


def test_scene_clue_does_not_claim_a_character_self_recovery() -> None:
    text = "沈渡把矿石碎片收进布袋，随后扶住岩壁，强迫自己稳住呼吸。"
    scene = {"clueRef": "clue-main", "abilityRef": None}
    contract = {
        "persistentClue": {
            "id": "clue-main",
            "description": "裂隙边缘带有五色纹路的矿石碎片",
            "sourceRef": "existing-thread",
            "function": "evidence_only",
        },
        "abilityLimitAndCost": None,
    }

    issues = scene_contract_quality_issues(text, scene, contract)

    assert "scene_clue_utility_expansion" not in issues


def test_scene_clue_cannot_reduce_an_injury_as_a_passive_effect() -> None:
    text = (
        "沈渡将獠牙碎片凑到鼻尖，那股冷香钻进鼻腔，"
        "经脉里残余的刺痛竟被压下去几分。"
    )
    scene = {"clueRef": "clue-main", "abilityRef": None}
    contract = {
        "persistentClue": {
            "id": "clue-main",
            "description": "噬灵蟾折断后留下的獠牙碎片和冷香",
            "sourceRef": "existing-thread",
            "function": "evidence_only",
        },
        "abilityLimitAndCost": None,
    }

    issues = scene_contract_quality_issues(text, scene, contract)

    assert "scene_clue_utility_expansion" in issues


def test_content_wrapper_requires_a_clean_local_revision() -> None:
    wrapped = f"<content>{_prose(730, 0)}</content>"
    adapter = FakeAdapter(
        [
            _plan(4),
            wrapped,
            _prose(750, 0),
            _prose(750, 1),
            _prose(750, 2),
            _prose(750, 3),
        ]
    )

    result = asyncio.run(SemanticBudgetController().generate(_request(), adapter))

    assert result.completed
    assert result.provider_calls == 6
    assert result.revision_attempts == 1
    assert result.revision_acceptances == 1
    assert result.scenes[0]["originalMechanicalIssues"] == ["content_wrapper", "incomplete_ending"]
    assert result.scenes[0]["mechanicalIssues"] == []


def test_quality_failure_is_never_accepted_when_revision_budget_is_zero() -> None:
    adapter = FakeAdapter([_plan(4), f"<content>{_prose(750, 0)}</content>"])

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(maximum_scene_revisions=0),
            adapter,
        )
    )

    assert result.status == "failed_quality"
    assert not result.completed
    assert result.generated_word_count == 0
    assert result.provider_calls == 2
    assert result.scenes[0]["revisionSkippedReason"] == "quality_revision_limit"


def test_provider_failure_during_revision_keeps_the_original_scene_audit() -> None:
    class GatewayTimeout(Exception):
        status_code = 504

    adapter = FakeAdapter([_plan(4), _prose(2200, 0), GatewayTimeout("hidden upstream detail")])

    result = asyncio.run(SemanticBudgetController().generate(_request(), adapter))

    assert result.status == "failed_provider"
    assert result.provider_calls == 3
    assert len(result.scenes) == 1
    assert result.scenes[0]["originalWordCount"] == 2200
    assert result.scenes[0]["revisionTriggered"] is True
    assert result.scenes[0]["revisionError"]["statusCode"] == 504
    assert "hidden upstream detail" not in json.dumps(result.error)


def test_clean_final_scene_inside_product_range_skips_unnecessary_revision() -> None:
    class GatewayTimeout(Exception):
        status_code = 504

    adapter = FakeAdapter(
        [
            _plan(3),
            _prose(500, 0),
            _prose(500, 1),
            _prose(700, 2),
            GatewayTimeout("hidden upstream detail"),
        ]
    )

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(product_target_word_count=1500, scene_count=3),
            adapter,
        )
    )

    assert result.completed
    assert result.generated_word_count == 1700
    assert result.provider_calls == 4
    assert result.revision_attempts == 0
    assert result.revision_acceptances == 0
    assert result.scenes[-1]["revisionTriggered"] is False
    assert result.scenes[-1]["lengthRevisionNeeded"] is False
    assert len(adapter.responses) == 1
    assert "hidden upstream detail" not in json.dumps(result.to_dict())


def test_clean_final_scene_near_scene_floor_uses_safe_chapter_capacity() -> None:
    class GatewayTimeout(Exception):
        status_code = 504

    adapter = FakeAdapter(
        [
            _plan(4),
            _prose(1053, 0),
            _prose(1082, 1),
            _prose(1034, 2),
            _prose(929, 3),
            GatewayTimeout("length-only revision should not run"),
        ]
    )

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(product_target_word_count=5000, scene_count=4),
            adapter,
        )
    )

    assert result.completed
    assert result.generated_word_count == 4098
    assert result.provider_calls == 5
    assert result.revision_attempts == 0
    assert result.scenes[-1]["lengthRevisionNeeded"] is False
    assert result.scenes[-1]["revisionTriggered"] is False
    assert len(adapter.responses) == 1


def test_final_revision_targets_the_nearest_product_boundary() -> None:
    def respond(purpose: str, metadata: Dict[str, Any]) -> str:
        assert purpose == "semantic_budget_revision"
        return _prose(int(metadata["desiredWordCount"]), 20)

    adapter = FakeAdapter(
        [
            _plan(3),
            _prose(700, 0),
            _prose(700, 1),
            _prose(700, 2),
            respond,
        ]
    )

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(
                product_target_word_count=1500,
                scene_count=3,
                maximum_scene_revisions=1,
            ),
            adapter,
        )
    )

    assert result.completed
    assert result.generated_word_count == result.acceptance_maximum == 1950
    assert result.scenes[-1]["revisionDesiredWordCount"] == 550
    assert result.scenes[-1]["revisionAccepted"] is True
    assert adapter.calls[-1]["metadata"]["desiredWordCount"] == 550


def test_capacity_revision_is_accepted_when_it_resolves_projected_overflow() -> None:
    plan = json.loads(_plan(2))
    development = "沈渡识别失控木灵气，以土灵气稳定经脉并避开袭来的藤蔓。"
    exit_hook = "他确认退路被封，只能握紧短镰刀继续向峡谷深处前行。"
    plan["scenes"][0]["development"] = development
    plan["scenes"][0]["exitHook"] = exit_hook

    def planned_prose(count: int, seed: int) -> str:
        filler_count = count - len(development) - len(exit_hook)
        assert filler_count > 0
        return development + _prose(filler_count, seed) + exit_hook

    adapter = FakeAdapter(
        [
            json.dumps(plan, ensure_ascii=False),
            planned_prose(1480, 0),
            planned_prose(1020, 10),
            _prose(900, 1),
        ]
    )

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(
                product_target_word_count=1500,
                scene_count=2,
                maximum_scene_revisions=1,
            ),
            adapter,
        )
    )

    assert result.completed
    assert result.generated_word_count == 1920
    assert result.scenes[0]["revisionDesiredWordCount"] == 1275
    assert result.scenes[0]["revisionAccepted"] is True
    assert result.scenes[0]["acceptedWordCount"] == 1020


def test_capacity_safe_middle_scene_does_not_use_revision_budget() -> None:
    def respond(purpose: str, metadata: Dict[str, Any]) -> str:
        desired = int(metadata.get("desiredWordCount") or 750)
        scene = int(metadata.get("scene") or 1)
        if purpose == "semantic_budget_scene" and scene == 1:
            return _prose(917, 0)
        return _prose(desired, scene * 2 + 1)

    adapter = FakeAdapter([_plan(4), *([respond] * 6)])

    result = asyncio.run(SemanticBudgetController().generate(_request(), adapter))

    assert result.completed
    assert result.provider_calls == 5
    assert result.revision_attempts == 0
    assert result.scenes[0]["lengthRevisionNeeded"] is True
    assert result.scenes[0]["chapterInternalUpperBoundAtRisk"] is False
    assert result.scenes[0]["revisionSkippedReason"] == "chapter_capacity_safe"


def test_severely_short_early_scene_is_revised_before_the_final_scene() -> None:
    def respond(purpose: str, metadata: Dict[str, Any]) -> str:
        scene = int(metadata.get("scene") or 1)
        if purpose == "semantic_budget_revision":
            count = 1100 if scene == 1 else int(metadata["desiredWordCount"])
            return _prose(count, 40 + scene)
        counts = {1: 480, 2: 1000, 3: 1000, 4: 900}
        return _prose(counts[scene], scene * 3)

    adapter = FakeAdapter([_plan(4), *([respond] * 6)])

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(
                product_target_word_count=5000,
                scene_count=4,
                maximum_scene_revisions=2,
            ),
            adapter,
        )
    )

    assert [
        scene["order"] for scene in result.scenes if scene["revisionTriggered"]
    ] == [1]


def test_final_revision_failure_stays_failed_outside_product_range() -> None:
    class GatewayTimeout(Exception):
        status_code = 504

    adapter = FakeAdapter(
        [
            _plan(3),
            _prose(500, 0),
            _prose(500, 1),
            _prose(1000, 2),
            GatewayTimeout("hidden upstream detail"),
        ]
    )

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(product_target_word_count=1500, scene_count=3),
            adapter,
        )
    )

    assert result.status == "failed_provider"
    assert result.generated_word_count == 1000
    assert result.scenes[-1]["revisionFallbackAccepted"] is False
    assert result.scenes[-1]["revisionError"]["statusCode"] == 504


def test_final_revision_failure_rejects_a_tiny_scene_inside_product_range() -> None:
    class GatewayTimeout(Exception):
        status_code = 504

    adapter = FakeAdapter(
        [
            _plan(3),
            _prose(600, 0),
            _prose(600, 1),
            _prose(100, 2),
            GatewayTimeout("hidden upstream detail"),
        ]
    )

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(
                product_target_word_count=1500,
                scene_count=3,
                maximum_scene_revisions=1,
            ),
            adapter,
        )
    )

    assert result.status == "failed_provider"
    assert result.generated_word_count == 1200
    assert result.scenes[-1]["originalWordCount"] == 100
    assert result.scenes[-1]["revisionFallbackAccepted"] is False


def test_duplicate_paragraphs_across_scenes_fail_before_assembly() -> None:
    repeated = _prose(750, 0)
    adapter = FakeAdapter([_plan(4), repeated, repeated, repeated, repeated])

    result = asyncio.run(SemanticBudgetController().generate(_request(), adapter))

    assert result.status == "failed_quality"
    assert result.generated_word_count == 750
    assert result.provider_calls == 4
    assert result.scenes[1]["originalMechanicalIssues"] == [
        "scene_context_repetition"
    ]
    assert result.scenes[1]["revisionAccepted"] is False
    assert result.scenes[1]["revisionQualityIssues"] == [
        "scene_context_repetition"
    ]


def test_normal_3000_path_never_exceeds_seven_semantic_calls() -> None:
    def respond(purpose: str, metadata: Dict[str, Any]) -> str:
        desired = int(metadata.get("desiredWordCount") or 750)
        if purpose == "semantic_budget_revision":
            return _prose(desired, int(metadata["scene"]) + 10)
        scene = int(metadata.get("scene") or 1)
        return _prose(int(round(desired * 1.25)), scene)

    adapter = FakeAdapter([_plan(4), respond, respond, respond, respond, respond, respond])

    result = asyncio.run(SemanticBudgetController().generate(_request(), adapter))

    assert result.provider_calls <= 7
    assert result.revision_attempts <= 2
    assert len(result.scenes) == 4


def test_middle_scene_uses_revision_only_when_chapter_capacity_is_at_risk() -> None:
    def respond(purpose: str, metadata: Dict[str, Any]) -> str:
        desired = int(metadata.get("desiredWordCount") or 500)
        scene = int(metadata.get("scene") or 1)
        if purpose == "semantic_budget_revision":
            return _prose(desired, scene + 10)
        if scene == 1:
            return _prose(900, 0)
        if scene == 2:
            return _prose(760, 2)
        return _prose(500, 4)

    adapter = FakeAdapter([_plan(3), *([respond] * 5)])

    result = asyncio.run(
        SemanticBudgetController().generate(
            _request(product_target_word_count=1500, scene_count=3),
            adapter,
        )
    )

    assert result.completed
    assert result.generated_word_count == result.acceptance_maximum == 1950
    assert result.revision_attempts == 1
    assert result.scenes[0]["revisionTriggered"] is False
    assert result.scenes[0]["revisionSkippedReason"] == "chapter_capacity_safe"
    assert result.scenes[1]["chapterUpperBoundAtRisk"] is True
    assert result.scenes[1]["revisionTriggered"] is True
    assert result.scenes[1]["revisionDesiredWordCount"] == 550
    assert result.scenes[1]["revisionAccepted"] is True
    assert result.scenes[2]["revisionTriggered"] is False
    assert result.scenes[2]["revisionSkippedReason"] == ""


def test_late_capacity_risk_preserves_unused_revision_budget() -> None:
    def respond(purpose: str, metadata: Dict[str, Any]) -> str:
        desired = int(metadata.get("desiredWordCount") or 750)
        scene = int(metadata.get("scene") or 1)
        if purpose == "semantic_budget_revision":
            return _prose(desired, {1: 1, 2: 5}.get(scene, 11))
        if scene == 1:
            return _prose(1500, 0)
        if scene == 2:
            return _prose(1700, 3)
        return _prose(desired, scene * 2 + 1)

    adapter = FakeAdapter([_plan(4), *([respond] * 8)])

    result = asyncio.run(SemanticBudgetController().generate(_request(), adapter))

    assert result.scenes[0]["revisionTriggered"] is False
    assert result.scenes[0]["revisionSkippedReason"] == "chapter_capacity_safe"
    assert result.scenes[1].get("chapterInternalUpperBoundAtRisk") is True
    assert result.scenes[1]["revisionTriggered"] is True
    assert result.scenes[1]["revisionDesiredWordCount"] == 1050
    assert result.scenes[1]["revisionAccepted"] is True
    assert result.revision_attempts == 1
    assert result.completed
    assert all(scene["acceptedWordCount"] >= 500 for scene in result.scenes)


def test_scene_constraint_context_keeps_style_and_drops_output_protocol(tmp_path: Path) -> None:
    active = tmp_path / ".storydex" / "presets" / "active"
    active.mkdir(parents=True)
    (active / "test.preset.json").write_text(
        json.dumps(
            {
                "modules": [
                    {
                        "id": "style",
                        "title": "prose style",
                        "enabledByDefault": True,
                        "content": "Keep concrete action.\n字数：800字起步",
                    },
                    {
                        "id": "format",
                        "title": "<content>标签",
                        "enabledByDefault": True,
                        "content": "Wrap prose in <content></content>.",
                    },
                    {
                        "id": "nsfw",
                        "title": "NSFW风格",
                        "enabledByDefault": True,
                        "content": "色情描写要直白。",
                    },
                    {
                        "id": "perspective",
                        "title": "人称控制",
                        "enabledByDefault": True,
                        "content": "使用第二人称称呼主角。",
                    },
                    {
                        "id": "disabled",
                        "title": "disabled style",
                        "enabledByDefault": False,
                        "content": "Never include this.",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    context, audit = read_scene_constraint_context(tmp_path)

    assert "Keep concrete action." in context
    assert "字数" not in context
    assert "<content>" not in context
    assert "色情" not in context
    assert "第二人称" not in context
    assert "Never include this" not in context
    assert [item["moduleId"] for item in audit] == ["style"]
