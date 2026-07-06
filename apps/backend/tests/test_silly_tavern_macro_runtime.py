from services.silly_tavern_macro_runtime import create_silly_tavern_macro_runtime


def test_silly_tavern_variable_existence_and_delete_macros():
    runtime = create_silly_tavern_macro_runtime()

    text = runtime.expand(
        "{{setvar::flag::on}}"
        "local={{hasvar::flag}}/{{varexists::flag}};"
        "{{deletevar::flag}}"
        "after={{hasvar::flag}};"
        "{{setglobalvar::globalFlag::yes}}"
        "global={{hasglobalvar::globalFlag}}/{{globalvarexists::globalFlag}};"
        "{{deleteglobalvar::globalFlag}}"
        "globalAfter={{hasglobalvar::globalFlag}}"
    )

    assert text == "local=true/true;after=false;global=true/true;globalAfter=false"


def test_silly_tavern_global_increment_decrement_macros():
    runtime = create_silly_tavern_macro_runtime()

    text = runtime.expand(
        "{{setglobalvar::counter::2}}"
        "inc={{incglobalvar::counter}};"
        "dec={{decglobalvar::counter}};"
        "value={{getglobalvar::counter}}"
    )

    assert text == "inc=3;dec=2;value=2"


def test_silly_tavern_text_utility_macros():
    runtime = create_silly_tavern_macro_runtime()

    text = runtime.expand("A{{space::3}}B{{newline::2}}C{{noop}}D{{reverse::猫神}}")

    assert text == "A   B\n\nCD神猫"


def test_silly_tavern_roll_macro_treats_plain_number_as_die_sides():
    runtime = create_silly_tavern_macro_runtime({"randomSeed": 3})

    text = runtime.expand("roll={{roll::1}}")

    assert text == "roll=1"


def test_silly_tavern_if_else_macro_resolves_selected_branch_only():
    runtime = create_silly_tavern_macro_runtime()

    text = runtime.expand(
        "{{setvar::show::true}}"
        "{{if {{getvar::show}}}}visible {{setvar::branch::then}}{{else}}hidden {{setvar::branch::else}}{{/if}}"
        " branch={{getvar::branch}}"
    )

    assert text == "visible branch=then"


def test_silly_tavern_if_macro_supports_inverted_condition_and_variable_shorthand():
    runtime = create_silly_tavern_macro_runtime()

    text = runtime.expand("{{setvar::enabled::false}}{{if !.enabled}}disabled{{else}}enabled{{/if}}")

    assert text == "disabled"


def test_silly_tavern_inline_if_macro():
    runtime = create_silly_tavern_macro_runtime()

    text = runtime.expand("{{setvar::enabled::on}}A{{if .enabled::B}}C")

    assert text == "ABC"


def test_silly_tavern_time_macros_use_runtime_clock_context():
    runtime = create_silly_tavern_macro_runtime({"now": "2026-07-06T05:04:03+08:00"})

    text = runtime.expand(
        "{{isodate}}|{{isotime}}|{{date}}|{{weekday}}|{{datetimeformat::YYYY-MM-DD HH:mm:ss}}|{{idleDuration}}"
    )

    assert text == "2026-07-06|05:04|July 6, 2026|Monday|2026-07-06 05:04:03|just now"


def test_silly_tavern_time_diff_macro_reports_human_readable_delta():
    runtime = create_silly_tavern_macro_runtime()

    text = runtime.expand("{{timeDiff::2026-07-06 12:00:00::2026-07-06 15:00:00}}")

    assert text == "3 hours ago"


def test_silly_tavern_input_macro_reads_current_prompt_text():
    runtime = create_silly_tavern_macro_runtime({"input": "当前输入", "prompt": "提示回退"})

    text = runtime.expand("{{input}}")

    assert text == "当前输入"


def test_silly_tavern_chat_macros_can_derive_from_runtime_chat_context():
    runtime = create_silly_tavern_macro_runtime(
        {
            "chat": [
                {"mes": "用户上一句", "is_user": True},
                {"mes": "角色上一句", "is_user": False, "swipes": ["a", "b"], "swipe_id": 1},
            ],
            "chatMetadata": {"lastInContextMessageId": 0, "firstDisplayedMessageId": 0},
        }
    )

    text = runtime.expand(
        "{{lastMessage}}|{{lastMessageId}}|{{lastUserMessage}}|{{lastCharMessage}}|"
        "{{firstIncludedMessageId}}|{{firstDisplayedMessageId}}|{{lastSwipeId}}|{{currentSwipeId}}|{{allChatRange}}"
    )

    assert text == "角色上一句|1|用户上一句|角色上一句|0|0|2|2|0-1"


def test_silly_tavern_environment_character_macros_use_runtime_context():
    runtime = create_silly_tavern_macro_runtime(
        {
            "user": "读者",
            "char": "夏瑾",
            "group": "夏瑾, 梁元",
            "groupNotMuted": "夏瑾",
            "notChar": "读者, 梁元",
            "charPrompt": "主提示覆盖",
            "charInstruction": "后置指令",
            "charDescription": "角色描述",
            "charPersonality": "角色性格",
            "charScenario": "角色场景",
            "persona": "用户人格",
            "mesExamplesRaw": "原始示例",
            "mesExamples": "格式化示例",
            "charDepthPrompt": "深度注释",
            "creatorNotes": "作者备注",
            "charFirstMessage": "初始问候",
            "alternateGreetings": ["备用问候一", "备用问候二"],
            "charVersion": "1.2.3",
            "model": "test-model",
            "original": "原文",
            "isMobile": False,
        }
    )

    text = runtime.expand(
        "{{group}}|{{charIfNotGroup}}|{{groupNotMuted}}|{{notChar}}|{{charPrompt}}|{{charInstruction}}|"
        "{{description}}|{{personality}}|{{scenario}}|{{persona}}|{{mesExamplesRaw}}|{{mesExamples}}|"
        "{{charDepthPrompt}}|{{creatorNotes}}|{{greeting}}|{{greeting::2}}|{{charVersion}}|{{version}}|"
        "{{model}}|{{original}}|{{isMobile}}"
    )

    assert text == (
        "夏瑾, 梁元|夏瑾, 梁元|夏瑾|读者, 梁元|主提示覆盖|后置指令|角色描述|角色性格|角色场景|"
        "用户人格|原始示例|格式化示例|深度注释|作者备注|初始问候|备用问候二|1.2.3|1.2.3|"
        "test-model|原文|false"
    )


def test_silly_tavern_context_limit_state_macros_use_runtime_context():
    runtime = create_silly_tavern_macro_runtime(
        {
            "maxPrompt": 12000,
            "maxContext": 16000,
            "maxResponse": 2000,
            "lastGenerationType": "normal",
            "extensions": ["world-info", "regex"],
        }
    )

    text = runtime.expand(
        "{{maxPrompt}}|{{maxPromptTokens}}|{{maxContext}}|{{maxContextTokens}}|"
        "{{maxResponse}}|{{maxResponseTokens}}|{{lastGenerationType}}|"
        "{{hasExtension::regex}}|{{hasExtension::missing}}"
    )

    assert text == "12000|12000|16000|16000|2000|2000|normal|true|false"


def test_silly_tavern_pick_macro_is_stable_for_same_chat_and_position():
    runtime = create_silly_tavern_macro_runtime({"chatId": "sample-chat", "randomSeed": 17})

    first = runtime.expand("first={{pick::red::blue::green}};second={{pick::red::blue::green}}")
    second = runtime.expand("first={{pick::red::blue::green}};second={{pick::red::blue::green}}")

    assert first == second
    assert "{{pick" not in first
    assert first.split(";")[0].split("=")[1] in {"red", "blue", "green"}
    assert first.split(";")[1].split("=")[1] in {"red", "blue", "green"}


def test_silly_tavern_outlet_macro_reads_runtime_outlet_context():
    runtime = create_silly_tavern_macro_runtime(
        {
            "outlets": {
                "character-achievements": "世界书出口内容",
            }
        }
    )

    text = runtime.expand("O={{outlet::character-achievements}};M={{outlet::missing}}")

    assert text == "O=世界书出口内容;M="


def test_silly_tavern_instruct_and_system_prompt_macros_read_runtime_context():
    runtime = create_silly_tavern_macro_runtime(
        {
            "instruct": {
                "enabled": True,
                "storyStringPrefix": "<story>",
                "storyStringSuffix": "</story>",
                "inputSequence": "<user>",
                "inputSuffix": "</user>",
                "outputSequence": "<assistant>",
                "outputSuffix": "</assistant>",
                "systemSequence": "<system>",
                "systemSuffix": "</system>",
                "firstOutputSequence": "<first-assistant>",
                "lastOutputSequence": "<last-assistant>",
                "stopSequence": "<stop>",
                "userAlignmentMessage": "<continue>",
                "lastSystemSequence": "<instruction>",
                "firstInputSequence": "<first-user>",
                "lastInputSequence": "<last-user>",
            },
            "systemPrompt": "默认系统提示",
            "charPrompt": "角色系统提示",
            "preferCharacterPrompt": True,
            "contextTemplate": {
                "exampleSeparator": "<example>",
                "chatStart": "<chat>",
            },
        }
    )

    text = runtime.expand(
        "{{instructStoryStringPrefix}}|{{instructStoryStringSuffix}}|"
        "{{instructUserPrefix}}|{{instructInput}}|{{instructUserSuffix}}|"
        "{{instructAssistantPrefix}}|{{instructOutput}}|{{instructAssistantSuffix}}|{{instructSeparator}}|"
        "{{instructSystemPrefix}}|{{instructSystemSuffix}}|"
        "{{instructFirstAssistantPrefix}}|{{instructFirstOutputPrefix}}|"
        "{{instructLastAssistantPrefix}}|{{instructLastOutputPrefix}}|"
        "{{instructStop}}|{{instructUserFiller}}|{{instructSystemInstructionPrefix}}|"
        "{{instructFirstUserPrefix}}|{{instructFirstInput}}|"
        "{{instructLastUserPrefix}}|{{instructLastInput}}|"
        "{{defaultSystemPrompt}}|{{instructSystem}}|{{instructSystemPrompt}}|{{systemPrompt}}|"
        "{{exampleSeparator}}|{{chatSeparator}}|{{chatStart}}"
    )

    assert text == (
        "<story>|</story>|<user>|<user>|</user>|"
        "<assistant>|<assistant>|</assistant>|</assistant>|"
        "<system>|</system>|"
        "<first-assistant>|<first-assistant>|"
        "<last-assistant>|<last-assistant>|"
        "<stop>|<continue>|<instruction>|"
        "<first-user>|<first-user>|"
        "<last-user>|<last-user>|"
        "默认系统提示|默认系统提示|默认系统提示|角色系统提示|"
        "<example>|<example>|<chat>"
    )
