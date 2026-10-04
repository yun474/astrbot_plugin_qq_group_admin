from copy import deepcopy
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from astrbot.api.message_components import Reply
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
    QQOfficialPlatformAdapter,
)
from botpy.message import GroupMessage

from astrbot_plugin_qq_group_admin.api import QQGroupManageAPI
from astrbot_plugin_qq_group_admin.main import join_keyword_decision
import test_keywords
from http_fakes import make_http


class RecallRegexTests(unittest.IsolatedAsyncioTestCase):
    event = staticmethod(test_keywords.KeywordTests.event)
    application = staticmethod(test_keywords.KeywordTests.application)

    def setUp(self):
        test_keywords.KeywordTests.setUp(self)
        self.api.recall_group_message = AsyncMock()

    async def test_recall_and_mute_are_independent_and_never_persist_messages(self):
        self.plugin.storage.save()
        original = self.plugin.storage.path.read_bytes()
        for mute, recall in (
            (True, False),
            (False, True),
            (True, True),
            (False, False),
        ):
            with self.subTest(mute=mute, recall=recall):
                self.settings.update(
                    enable_keyword_mute=mute, enable_keyword_recall=recall
                )
                self.api.recall_group_message.reset_mock()
                self.api.mute_member.reset_mock()
                event = self.event()
                event.message_obj.message_id = "current/id"
                event.message_obj.message.insert(0, Reply(id="quoted/id"))
                await self.plugin.keyword_mute(event)
                self.assertEqual(self.api.recall_group_message.await_count, int(recall))
                if recall:
                    self.api.recall_group_message.assert_awaited_with("g", "current/id")
                self.assertEqual(self.api.mute_member.await_count, int(mute))
                self.assertEqual(event.is_stopped(), mute or recall)
                event.send.assert_not_awaited()
                self.assertEqual(self.plugin.storage.path.read_bytes(), original)

    async def test_recall_errors_and_missing_ids_do_not_prevent_mute(self):
        self.settings["enable_keyword_recall"] = True
        for message_id, failure in (
            ("", None),
            ("id", RuntimeError("denied")),
            ("id", TimeoutError()),
        ):
            with self.subTest(message_id=message_id, failure=failure):
                self.api.recall_group_message.reset_mock()
                self.api.recall_group_message.side_effect = failure
                self.api.mute_member.reset_mock()
                event = self.event()
                event.message_obj.message_id = message_id
                event.message_obj.message.insert(0, Reply(id="must-not-recall-this"))
                await self.plugin.keyword_mute(event)
                self.api.mute_member.assert_awaited_once()
                self.assertEqual(
                    self.api.recall_group_message.await_count, int(bool(message_id))
                )
                event.send.assert_not_awaited()

    async def test_invalid_mute_duration_does_not_prevent_recall(self):
        self.settings.update(enable_keyword_recall=True, keyword_mute_duration="bad")
        event = self.event()
        event.message_obj.message_id = "current"
        await self.plugin.keyword_mute(event)
        self.api.recall_group_message.assert_awaited_once_with("g", "current")
        self.api.mute_member.assert_not_awaited()

    async def test_recall_obeys_existing_scope_and_authorized_command_exemptions(self):
        self.settings.update(enable_keyword_recall=True, enable_keyword_mute=False)
        event = self.event("群管功能 违禁词 添加 违禁")
        event.role = "admin"
        event.is_at_or_wake_command = True
        for current in (
            event,
            self.event(platform="aiocqhttp"),
            self.event(group=""),
            self.event(sender="bot"),
            self.event("正常"),
        ):
            current.message_obj.message_id = "id"
            await self.plugin.keyword_mute(current)
        self.session_enabled.return_value = False
        await self.plugin.keyword_mute(self.event())
        self.api.recall_group_message.assert_not_awaited()

    async def test_official_adapter_keeps_current_id_without_history_lookup(self):
        raw = GroupMessage(
            None,
            "event-id",
            {
                "id": "current-message-id",
                "content": "违禁",
                "group_openid": "g",
                "author": {"member_openid": "member"},
                "attachments": [],
            },
        )
        message = await QQOfficialPlatformAdapter._parse_from_qqofficial(
            raw, MessageType.GROUP_MESSAGE
        )
        self.assertEqual(message.message_id, "current-message-id")
        self.settings.update(enable_keyword_recall=True, enable_keyword_mute=False)
        event = self.event()
        event.message_obj = message
        await self.plugin.keyword_mute(event)
        self.api.recall_group_message.assert_awaited_once_with(
            "g", "current-message-id"
        )

    async def test_recall_endpoint_encodes_both_ids(self):
        http = make_http(empty=True)
        await QQGroupManageAPI(NS(api=NS(_http=http))).recall_group_message(
            "g/one", "id/+?"
        )
        call = http._session.request.call_args
        self.assertEqual(call.kwargs["method"], "DELETE")
        self.assertTrue(
            call.kwargs["url"].endswith("/v2/groups/g%2Fone/messages/id%2F%2B%3F")
        )
        self.assertNotIn("json", call.kwargs)

    def test_regex_search_anchors_alternation_escapes_and_priority(self):
        cases = [
            ("我爱猫", ["猫|狗"], [], "黑词优先", True),
            ("我爱猫", ["^猫$"], [], "黑词优先", None),
            ("CAT", ["^cat$"], [], "黑词优先", True),
            ("abc42", [r"\d+"], [r"\D+"], "黑词优先", False),
            ("abc42", [r"\d+"], [r"\D+"], "白词优先", True),
            ("a.b", [r"a\.b"], [], "黑词优先", True),
            ("axb", [r"a\.b"], [], "黑词优先", None),
        ]
        for answer, white, black, priority, expected in cases:
            with self.subTest(answer=answer, white=white, priority=priority):
                self.assertIs(
                    join_keyword_decision(
                        self.application(answer), white, black, priority, "正则匹配"
                    ),
                    expected,
                )

    async def test_invalid_lower_priority_regex_never_allows_automatic_approval(self):
        self.settings.update(
            join_whitelist_words=[".*"],
            join_blacklist_words=["["],
            join_keyword_priority="白词优先",
            join_keyword_match_mode="正则匹配",
        )
        await self.plugin._handle_join_request_event("p", self.application("同好"))
        self.api.review_join_request.assert_not_awaited()
        self.api.send_group_markdown.assert_awaited_once()

    async def test_pathological_regex_times_out_and_falls_back_to_manual(self):
        self.settings.update(
            join_whitelist_words=[],
            join_blacklist_words=["(a|aa)+$"],
            join_keyword_match_mode="正则匹配",
        )
        with patch("astrbot_plugin_qq_group_admin.keyword_rules.MATCH_TIMEOUT", 0.001):
            await self.plugin._handle_join_request_event(
                "p", self.application("a" * 1000 + "!")
            )
        self.api.review_join_request.assert_not_awaited()
        self.api.send_group_markdown.assert_awaited_once()

    def test_chat_regex_validation_and_case_sensitive_escape_edits(self):
        self.settings["join_keyword_match_mode"] = "正则匹配"
        self.settings["join_whitelist_words"] = []
        for pattern in (r"\d+", r"\D+"):
            self.plugin._keyword_config_command(
                self.event(), "进群白词", "添加", pattern
            )
        self.assertEqual(self.settings["join_whitelist_words"], [r"\d+", r"\D+"])
        self.plugin._keyword_config_command(self.event(), "进群白词", "删除", r"\d+")
        self.assertEqual(self.settings["join_whitelist_words"], [r"\D+"])
        before = deepcopy(self.plugin.config)
        with self.assertRaisesRegex(ValueError, "正则无效"):
            self.plugin._keyword_config_command(self.event(), "进群白词", "添加", "[")
        self.assertEqual(self.plugin.config, before)

    def test_default_matching_uses_literal_substrings(self):
        for answer, word, expected in (
            ("我来发广告", "广告", True),
            ("学习C++", "C++", True),
            ("prefix a.b suffix", "a.b", True),
            ("axb", "a.b", None),
            ("[广告]", "[", True),
            ("CAT lover", "cat", True),
            ("我爱猫", "猫|狗", None),
            ("123", r"\d+", None),
        ):
            with self.subTest(answer=answer, word=word):
                self.assertIs(
                    join_keyword_decision(
                        self.application(answer), [word], [], "黑词优先"
                    ),
                    expected,
                )

    async def test_default_auto_review_accepts_literal_regex_metacharacters(self):
        self.settings.update(join_whitelist_words=["["], join_blacklist_words=[])
        await self.plugin._handle_join_request_event("p", self.application("[同好]"))
        self.api.review_join_request.assert_awaited_once()
        self.api.send_group_markdown.assert_not_awaited()

    def test_mode_switch_validates_both_lists_without_rewriting(self):
        self.settings.update(join_whitelist_words=["猫"], join_blacklist_words=[])
        self.plugin._keyword_config_command(self.event(), "进群黑词", "添加", "[")
        before = deepcopy(self.plugin.config)
        with self.assertRaisesRegex(ValueError, "正则无效"):
            self.plugin._keyword_config_command(
                self.event(), "进群匹配模式", "设置", "正则匹配"
            )
        self.assertEqual(self.plugin.config, before)
        self.plugin._keyword_config_command(self.event(), "进群黑词", "删除", "[")
        for mode in ("正则匹配", "包含匹配"):
            self.plugin._keyword_config_command(
                self.event(), "进群匹配模式", "设置", mode
            )
            self.assertEqual(self.settings["join_keyword_match_mode"], mode)
            self.assertEqual(self.settings["join_whitelist_words"], ["猫"])
            self.assertEqual(self.settings["join_blacklist_words"], [])
        with self.assertRaises(ValueError):
            self.plugin._keyword_config_command(
                self.event(), "进群匹配模式", "设置", "未知"
            )
