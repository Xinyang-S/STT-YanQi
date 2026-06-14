import os
import unittest

import voice_core.runtime as core


RESEARCH_SCENARIOS = {
    "filled_pause_cn",
    "filled_pause_en",
    "repeated_start",
    "false_start_restart",
    "self_repair_cn",
    "self_repair_en",
    "discourse_marker",
    "code_switching_command",
    "punctuation_segmentation",
    "english_capitalization",
    "number_date_unit_itn",
    "url_email_path_preservation",
    "named_entity_professional_terms",
    "homophone_near_sound",
    "negative_preservation",
    "already_good",
}


POLISH_REGRESSION_CASES = [
    {
        "id": "filled_pause_cn",
        "raw": "嗯今天的话我想测试一下这个语音输入它能不能把标点加好然后不要改我的意思",
        "expected_contains": ["今天", "语音输入", "不要改我的意思"],
        "forbidden_contains": ["嗯"],
    },
    {
        "id": "filled_pause_en",
        "raw": "um please open the settings uh and turn on local polish",
        "expected_contains": ["please", "settings", "local polish"],
        "forbidden_contains": ["um", "uh"],
    },
    {
        "id": "repeated_start",
        "raw": "我想我想说一下今天这个版本需要重新打包",
        "expected_contains": ["我想说一下", "重新打包"],
    },
    {
        "id": "false_start_restart",
        "raw": "打开设置界面打开外观设置然后把透明度调低一点",
        "expected_contains": ["打开外观设置", "透明度"],
    },
    {
        "id": "self_repair_cn",
        "raw": "我明天去上海不对去北京开会然后发给alice看一下PR",
        "hint_expected": "我明天去北京开会然后发给alice看一下PR",
        "expected_contains": ["去北京开会", "alice", "PR"],
        "forbidden_contains": ["去上海"],
    },
    {
        "id": "self_repair_en",
        "raw": "send it to bob no I mean alice after the build passes",
        "expected_contains": ["alice", "build passes"],
        "forbidden_contains": ["bob"],
    },
    {
        "id": "discourse_marker",
        "raw": "就是这个功能然后其实我们先不要上线先放到测试包里面",
        "expected_contains": ["先不要上线", "测试包"],
    },
    {
        "id": "code_switching_command",
        "raw": "打开terminal然后运行npm run build呃如果报错就先不要提交",
        "expected_contains": ["terminal", "npm run build", "不要提交"],
        "forbidden_contains": ["呃"],
    },
    {
        "id": "punctuation_segmentation",
        "raw": "今天先修提示音然后补测试用例最后重新打包发给用户验证",
        "expected_contains": ["提示音", "测试用例", "重新打包"],
    },
    {
        "id": "english_capitalization",
        "raw": "please open github and check the readme before release",
        "expected_contains": ["GitHub", "README", "release"],
    },
    {
        "id": "number_date_unit_itn",
        "raw": "明天上午十点半把百分之五十的灰度版本发给三个人测试",
        "expected_contains": ["明天上午", "十点半", "百分之五十", "三个人"],
    },
    {
        "id": "url_email_path_preservation",
        "raw": "把日志发到dev@example.com然后检查c盘users目录下面的ver nest日志",
        "expected_contains": ["dev@example.com", "users", "日志"],
    },
    {
        "id": "named_entity_professional_terms",
        "raw": "SenseVoice和llama cpp python都不要翻译检查PyInstaller打包结果",
        "expected_contains": ["SenseVoice", "llama cpp python", "PyInstaller"],
    },
    {
        "id": "homophone_near_sound",
        "raw": "把缓存青掉然后重新启动后端",
        "expected_contains": ["缓存", "清掉", "后端"],
    },
    {
        "id": "negative_preservation",
        "raw": "这个版本先不要发布也不要删除旧日志",
        "expected_contains": ["不要发布", "不要删除旧日志"],
    },
    {
        "id": "already_good",
        "raw": "请打开设置，关闭润色功能，然后重新测试快捷键。",
        "expected_contains": ["请打开设置", "关闭润色功能", "重新测试快捷键"],
    },
]


LOGIC_PRESERVATION_CASES = [
    {
        "id": "logic_disable_vs_delete",
        "raw": "没有必要禁用python提示音逻辑而是直接删除",
        "expected_contains": ["没有必要禁用", "直接删除"],
        "forbidden_contains": ["没有必要删除", "直接禁用"],
    },
    {
        "id": "logic_do_not_delete_move_legacy",
        "raw": "先不要删除旧代码只是移动到legacy目录",
        "expected_contains": ["不要删除旧代码", "移动到legacy目录"],
        "forbidden_contains": ["不要移动到legacy目录"],
    },
    {
        "id": "logic_not_close_hide_tray",
        "raw": "这个按钮不是关闭程序而是隐藏到托盘",
        "expected_contains": ["不是关闭程序", "隐藏", "托盘"],
        "forbidden_contains": ["关闭程序并隐藏", "关闭到托盘"],
    },
    {
        "id": "logic_disable_autostart_not_shortcut",
        "raw": "把开机自启动禁用掉不要禁用快捷键",
        "expected_contains": ["开机自启动", "不要禁用快捷键"],
        "forbidden_contains": ["禁用掉快捷键", "把快捷键禁用"],
    },
    {
        "id": "logic_default_off_not_remove_model",
        "raw": "不是把润色模型删掉而是默认关闭润色功能",
        "expected_contains": ["不是把润色模型删掉", "默认关闭润色功能"],
        "forbidden_contains": ["删除润色模型", "默认删除"],
    },
    {
        "id": "logic_keep_installer_delete_old_zip",
        "raw": "保留安装包但是删除旧的便携版zip",
        "expected_contains": ["保留安装包", "删除旧的便携版zip"],
        "forbidden_contains": ["删除安装包", "保留旧的便携版zip"],
    },
]


class PolishRegressionTests(unittest.TestCase):
    def test_cases_cover_all_researched_scenarios(self):
        self.assertEqual({case["id"] for case in POLISH_REGRESSION_CASES}, RESEARCH_SCENARIOS)

    def test_prompt_mentions_core_guardrails(self):
        prompt = core.POLISH_SYSTEM_PROMPT
        for phrase in [
            "不是聊天助手",
            "不新增信息",
            "code-switching",
            "不要翻译",
            "无语义填充词",
            "自我纠正",
            "否定词",
            "专业词",
            "原样输出",
        ]:
            self.assertIn(phrase, prompt)

    def test_self_repair_hint_handles_high_confidence_chinese_repair(self):
        case = next(case for case in POLISH_REGRESSION_CASES if case["id"] == "self_repair_cn")
        self.assertEqual(
            core.LocalTextPolisher._apply_self_repair_hints(case["raw"]),
            case["hint_expected"],
        )

    def test_self_repair_hint_leaves_ambiguous_negative_sentence_unchanged(self):
        raw = "我不是很想去北京但是可以先看一下时间"
        self.assertEqual(core.LocalTextPolisher._apply_self_repair_hints(raw), raw)

    def test_added_assistant_acknowledgement_is_removed(self):
        self.assertEqual(
            core.LocalTextPolisher._strip_added_acknowledgement("今天测试一下", "好的，今天测试一下。"),
            "今天测试一下。",
        )

    def test_contrast_marker_loss_is_rejected(self):
        self.assertFalse(
            core.LocalTextPolisher._preserves_protected_content(
                "没有必要禁用python提示音逻辑而是直接删除",
                "没有必要禁用python提示音逻辑",
            )
        )

    def test_negated_action_object_swap_is_rejected(self):
        self.assertFalse(
            core.LocalTextPolisher._preserves_protected_content(
                "把开机自启动禁用掉不要禁用快捷键",
                "不要禁用开机自启动",
            )
        )

    @unittest.skipUnless(
        os.environ.get("VERNEST_RUN_LLM_POLISH_TESTS") == "1",
        "set VERNEST_RUN_LLM_POLISH_TESTS=1 to run local GGUF regression cases",
    )
    def test_local_llm_polish_regression_cases(self):
        previous_config = dict(core.config)
        try:
            core.load_config()
            core.config["polish_enabled"] = True
            for case in [*POLISH_REGRESSION_CASES, *LOGIC_PRESERVATION_CASES]:
                with self.subTest(case=case["id"]):
                    polished = core.polish_text_if_enabled(case["raw"])
                    for expected in case.get("expected_contains", []):
                        self.assertIn(expected, polished)
                    for forbidden in case.get("forbidden_contains", []):
                        self.assertNotIn(forbidden, polished)
        finally:
            core.config.clear()
            core.config.update(previous_config)
            core.unload_polish_model()


if __name__ == "__main__":
    unittest.main()
