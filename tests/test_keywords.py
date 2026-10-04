import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from astrbot.api.message_components import Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata

from astrbot_plugin_qq_group_admin.main import QQGroupAdminPlugin, join_keyword_decision
from astrbot_plugin_qq_group_admin.storage import PluginStorage


class KeywordTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.plugin = object.__new__(QQGroupAdminPlugin)
        self.settings = {
            "enable_join_keyword_review": True,
            "join_whitelist_words": ["同好"],
            "join_blacklist_words": ["广告"],
            "join_keyword_priority": "黑词优先",
            "enable_keyword_mute": True,
            "mute_keywords": ["违禁", " SPAM ", "", "  "],
            "keyword_mute_duration": "2分30秒",
        }
        self.plugin.config = {"keyword_settings": self.settings}
        self.plugin.storage = PluginStorage(Path(directory.name) / "state.json")
        self.plugin._reviews_inflight = set()
        self.plugin.context = NS(
            get_platform_inst=Mock(return_value=NS(client=object())),
            get_config=Mock(return_value={"plugin_set": ["*"], "admins_id": []}),
        )
        self.api = NS(
            review_join_request=AsyncMock(),
            mute_member=AsyncMock(),
            send_group_markdown=AsyncMock(return_value={"id": "notice"}),
            send_group_text=AsyncMock(return_value={"id": "notice"}),
        )
        for target, value in (
            ("QQGroupManageAPI", Mock(return_value=self.api)),
            (
                "SessionPluginManager.is_plugin_enabled_for_session",
                AsyncMock(return_value=True),
            ),
        ):
            patcher = patch("astrbot_plugin_qq_group_admin.main." + target, new=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.session_enabled = value

    @staticmethod
    def application(*answers):
        return {
            "group_openid": "g",
            "member_openid": "applicant",
            "join_request_id": "request",
            "verify_info": {
                "review_qa_list": [{"question": "问题", "answer": a} for a in answers]
            },
        }

    @staticmethod
    def event(
        text="包含违禁内容", *, platform="qq_official", group="g", sender="member"
    ):
        message = AstrBotMessage()
        message.type = (
            MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
        )
        message.group_id, message.self_id = group, "bot"
        message.sender = MessageMember(sender, "test")
        message.message = [Plain(text=text)]
        event = AstrMessageEvent(
            text, message, PlatformMetadata(platform, "test", "p"), group
        )
        event.send = AsyncMock()
        return event

    def test_answer_rules_and_both_priorities(self):
        cases = [
            (("我是同好",), "黑词优先", True),
            (("发广告",), "白词优先", False),
            (("同好广告",), "黑词优先", False),
            (("同好广告",), "白词优先", True),
            (("同好", "广告"), "黑词优先", False),
            (("同好", "广告"), "白词优先", True),
            (("路过",), "黑词优先", None),
            (("同", "好"), "黑词优先", None),
            ((), "黑词优先", None),
        ]
        for answers, priority, expected in cases:
            with self.subTest(answers=answers, priority=priority):
                self.assertIs(
                    join_keyword_decision(
                        self.application(*answers), ["同好"], ["广告"], priority
                    ),
                    expected,
                )

    def test_only_answers_are_matched_and_empty_entries_are_ignored(self):
        item = self.application("正常内容")
        item["username"] = "广告"
        item["verify_info"]["verify_message"] = "广告"
        item["verify_info"]["review_qa_list"][0]["question"] = "广告"
        self.assertIsNone(join_keyword_decision(item, ["", "  "], ["广告"], "黑词优先"))
        self.assertIsNone(
            join_keyword_decision(self.application("同好广告"), [], [], "黑词优先")
        )
        self.assertIs(
            join_keyword_decision(
                self.application("I like ASTRBOT"), [" AstrBot "], [], "黑词优先"
            ),
            True,
        )

    async def test_auto_review_succeeds_without_notice_and_cleans_all_old_cards(self):
        item = self.application("同好")
        for key in ("old1", "old2"):
            self.plugin.storage.put_pending(key, dict(item, platform_id="p"))
        self.plugin.config["enable_join_notice"] = False
        self.plugin.config["enable_join_reply_review"] = False
        await self.plugin._handle_join_request_event("p", item)
        self.api.review_join_request.assert_awaited_once_with(
            "g", "applicant", "request", approve=True
        )
        self.assertEqual(self.plugin.storage.data["pending"], {})
        self.api.send_group_markdown.assert_not_awaited()
        self.assertEqual(self.plugin._reviews_inflight, set())

    async def test_blacklist_rejects_with_configured_priority(self):
        await self.plugin._handle_join_request_event("p", self.application("广告同好"))
        self.api.review_join_request.assert_awaited_once_with(
            "g", "applicant", "request", approve=False
        )
        self.api.send_group_markdown.assert_not_awaited()

    async def test_completed_requests_are_not_reopened_after_replay_or_reload(self):
        item = self.application("同好")
        await self.plugin._handle_join_request_event("p", item)
        await self.plugin._handle_join_request_event("p", item)
        self.plugin.storage = PluginStorage(self.plugin.storage.path)
        await self.plugin._handle_join_request_event("p", item)
        self.api.review_join_request.assert_awaited_once()
        self.api.send_group_markdown.assert_not_awaited()
        self.assertEqual(self.plugin.storage.data["pending"], {})

    async def test_manual_completion_blocks_automatic_replay_and_tool_retry(self):
        await self.plugin._review(self.event(), "g", "applicant", "request", False, "")
        self.plugin.storage = PluginStorage(self.plugin.storage.path)
        await self.plugin._handle_join_request_event("p", self.application("同好"))
        with self.assertRaisesRegex(ValueError, "已处理"):
            await self.plugin._review(
                self.event(), "g", "applicant", "request", True, ""
            )
        self.api.review_join_request.assert_awaited_once()
        self.api.send_group_markdown.assert_not_awaited()

    async def test_admin_command_reports_disk_failure_without_changing_permissions(
        self,
    ):
        self.plugin._mentioned_members = lambda event: ["a", "b"]
        event = self.event()
        event.role = "admin"
        for add in (True, False):
            with self.subTest(add=add):
                if not add:
                    self.plugin.storage.update_group_admins("g", ["a", "b"], add=True)
                command = (
                    self.plugin.add_group_admin
                    if add
                    else self.plugin.remove_group_admin
                )
                before = self.plugin.storage.group_admins("g")
                with patch.object(
                    self.plugin.storage, "save", side_effect=OSError("disk full")
                ):
                    results = [result async for result in command(event)]
                self.assertEqual(self.plugin.storage.group_admins("g"), before)
                self.assertIn("本次修改未生效", results[0].chain[0].text)

    async def test_qq_auto_approval_invalidates_cards_and_sends_only_information(self):
        item = dict(self.application("同好"), auto_approved={"strategy_id": "qq"})
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                item["join_request_id"] = f"auto-{fallback}"
                self.plugin.storage.put_pending("old", dict(item, platform_id="p"))
                self.api.send_group_markdown.side_effect = (
                    RuntimeError("markdown failed") if fallback else None
                )
                await self.plugin._handle_join_request_event("p", item)
                sender = (
                    self.api.send_group_text
                    if fallback
                    else self.api.send_group_markdown
                )
                call = sender.await_args
                self.assertIn("已自动通过", call.args[1])
                self.assertNotIn("引用", call.args[1])
                self.assertIsNone(call.kwargs.get("keyboard"))
                self.assertEqual(self.plugin.storage.data["pending"], {})
                self.assertTrue(
                    self.plugin.storage.is_reviewed("p", "g", item["join_request_id"])
                )
        self.api.review_join_request.assert_not_awaited()

    async def test_auto_approval_cleans_old_cards_even_with_notifications_disabled(
        self,
    ):
        self.plugin.config["enable_join_notice"] = False
        self.settings["enable_join_keyword_review"] = False
        item = dict(self.application("同好"), auto_approved={"strategy_id": "qq"})
        self.plugin.storage.put_pending("old", dict(item, platform_id="p"))
        await self.plugin._handle_join_request_event("p", item)
        self.assertEqual(self.plugin.storage.data["pending"], {})
        self.api.send_group_markdown.assert_not_awaited()

    async def test_completed_operation_remains_live_when_completion_write_fails(self):
        with patch.object(
            self.plugin.storage, "save", side_effect=OSError("disk full")
        ):
            await self.plugin._handle_join_request_event("p", self.application("同好"))
            await self.plugin._handle_join_request_event("p", self.application("同好"))
        self.api.review_join_request.assert_awaited_once()
        self.api.send_group_markdown.assert_not_awaited()

    async def test_only_authorized_real_review_precedes_keyword_mute(self):
        self.settings["mute_keywords"] = ["广告"]
        self.plugin.context.get_config.return_value["wake_prefix"] = ["!"]
        cases = (
            ("admin", "g", "notice", True, "拒绝 广告", True),
            ("admin", "g", "notice", True, "!拒绝 广告", True),
            ("member", "g", "notice", True, "拒绝 广告", False),
            ("admin", "other", "notice", True, "拒绝 广告", False),
            ("admin", "g", "missing", True, "拒绝 广告", False),
            ("admin", "g", "notice", False, "拒绝 广告", False),
            ("admin", "g", "notice", True, "普通广告", False),
        )
        for index, (role, group, quoted, enabled, text, allowed) in enumerate(cases):
            with self.subTest(index=index):
                self.plugin.storage.put_pending(
                    "notice",
                    dict(
                        self.application("广告"),
                        platform_id="p",
                        join_request_id=f"r-{index}",
                    ),
                )
                self.plugin.config["enable_join_reply_review"] = enabled
                event = self.event(text, group=group)
                event.role = role
                event.message_obj.message.insert(0, Reply(id=quoted))
                self.api.review_join_request.reset_mock()
                self.api.mute_member.reset_mock()
                await self.plugin.keyword_mute(event)
                self.assertTrue(event.is_stopped())
                if allowed:
                    self.api.mute_member.assert_not_awaited()
                    self.api.review_join_request.assert_awaited_once_with(
                        "g",
                        "applicant",
                        f"r-{index}",
                        approve=False,
                        reject_reason="广告",
                    )
                else:
                    self.api.mute_member.assert_awaited_once()
                    self.api.review_join_request.assert_not_awaited()

    async def test_failure_falls_back_to_manual_notice_and_releases_lock(self):
        self.api.review_join_request.side_effect = RuntimeError("QQ rejected")
        await self.plugin._handle_join_request_event("p", self.application("同好"))
        self.api.send_group_markdown.assert_awaited_once()
        self.assertIsNotNone(self.plugin.storage.get_pending("notice"))
        self.assertEqual(self.plugin._reviews_inflight, set())

    async def test_successful_review_does_not_send_card_if_storage_cleanup_fails(self):
        with patch.object(
            self.plugin.storage,
            "remove_reviewed_request",
            side_effect=OSError("disk full"),
        ):
            await self.plugin._handle_join_request_event("p", self.application("同好"))
        self.api.review_join_request.assert_awaited_once()
        self.api.send_group_markdown.assert_not_awaited()

    async def test_unmatched_and_already_approved_requests_are_not_auto_reviewed(self):
        for item in (
            self.application("路过"),
            dict(self.application("同好"), auto_approved={"strategy_id": "qq"}),
        ):
            await self.plugin._handle_join_request_event("p", item)
        self.api.review_join_request.assert_not_awaited()
        self.assertEqual(self.api.send_group_markdown.await_count, 2)

    async def test_concurrent_join_events_and_manual_review_share_lock(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def pending_review(*args, **kwargs):
            entered.set()
            await release.wait()

        self.api.review_join_request.side_effect = pending_review
        item = self.application("同好")
        task = asyncio.create_task(self.plugin._handle_join_request_event("p", item))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            await self.plugin._handle_join_request_event("p", item)
            with self.assertRaisesRegex(ValueError, "正在处理"):
                await self.plugin._review(
                    self.event(), "g", "applicant", "request", False, ""
                )
            self.api.review_join_request.assert_awaited_once()
        finally:
            release.set()
            await task

    async def test_both_features_respect_scope_and_session_disablement(self):
        for case in ("whitelist", "plugin_set", "session", "per_group"):
            with self.subTest(case=case):
                self.plugin.config["enabled_group_umos"] = (
                    ["other:GroupMessage:g"] if case == "whitelist" else []
                )
                self.plugin.context.get_config.return_value = {
                    "plugin_set": [] if case == "plugin_set" else ["*"]
                }
                self.session_enabled.return_value = case != "session"
                self.plugin.config["enable_per_group_feature_settings"] = (
                    case == "per_group"
                )
                for key in (
                    "enable_join_keyword_review",
                    "enable_keyword_mute",
                    "enable_join_notice",
                ):
                    self.plugin.storage.set_group_feature_override(
                        "p:GroupMessage:g", key, False
                    )
                await self.plugin._handle_join_request_event(
                    "p", self.application("同好")
                )
                await self.plugin.keyword_mute(self.event())
        self.api.review_join_request.assert_not_awaited()
        self.api.mute_member.assert_not_awaited()

    async def test_features_default_off_and_per_group_can_enable_them(self):
        self.settings.pop("enable_join_keyword_review")
        self.settings.pop("enable_keyword_mute")
        self.plugin.config["enable_join_notice"] = False
        await self.plugin._handle_join_request_event("p", self.application("同好"))
        await self.plugin.keyword_mute(self.event())
        self.api.review_join_request.assert_not_awaited()
        self.api.mute_member.assert_not_awaited()
        self.plugin.config["enable_per_group_feature_settings"] = True
        for key in ("enable_join_keyword_review", "enable_keyword_mute"):
            self.plugin.storage.set_group_feature_override(
                "p:GroupMessage:g", key, True
            )
        await self.plugin._handle_join_request_event("p", self.application("同好"))
        await self.plugin.keyword_mute(self.event())
        self.api.review_join_request.assert_awaited_once()
        self.api.mute_member.assert_awaited_once()

    async def test_keyword_mute_targets_sender_and_is_silent_on_success_and_failure(
        self,
    ):
        self.plugin.config["enable_mute_command"] = False
        for failure in (None, RuntimeError("cannot mute admin")):
            with self.subTest(failure=failure):
                event = self.event("Some SpAm here")
                self.api.mute_member.side_effect = failure
                with patch.object(
                    self.plugin, "_mute", wraps=self.plugin._mute
                ) as mute:
                    await self.plugin.keyword_mute(event)
                mute.assert_awaited_once_with(event, "g", "member", 150)
                self.assertTrue(event.is_stopped())
                event.send.assert_not_awaited()
        self.api.send_group_text.assert_not_awaited()

    async def test_invalid_or_zero_duration_never_unmutes(self):
        for duration in ("0", "解禁", "bad", "31天"):
            self.settings["keyword_mute_duration"] = duration
            event = self.event()
            await self.plugin.keyword_mute(event)
            self.assertTrue(event.is_stopped())
            event.send.assert_not_awaited()
        self.api.mute_member.assert_not_awaited()

    async def test_other_platforms_private_self_and_unmatched_messages_are_ignored(
        self,
    ):
        for event in (
            self.event(platform="aiocqhttp"),
            self.event(group=""),
            self.event(sender="bot"),
            self.event("正常内容"),
        ):
            await self.plugin.keyword_mute(event)
            self.assertFalse(event.is_stopped())
        self.api.mute_member.assert_not_awaited()

    async def test_quoted_text_is_not_a_mute_trigger(self):
        event = self.event("普通评论")
        event.message_obj.message.insert(
            0, Reply(id="old", message_str="违禁", chain=[Plain(text="违禁")])
        )
        await self.plugin.keyword_mute(event)
        self.api.mute_member.assert_not_awaited()
        self.assertFalse(event.is_stopped())

    async def test_framework_delivers_unwoken_group_messages_to_keyword_listener(self):
        from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
        from astrbot.core.star.star_handler import EventType, star_handlers_registry

        handlers = star_handlers_registry.get_handlers_by_event_type(
            EventType.AdapterMessageEvent
        )
        handler = next(h for h in handlers if h.handler_name == "keyword_mute")
        stage = WakingCheckStage()
        await stage.initialize(
            NS(
                astrbot_config={
                    "wake_prefix": ["!"],
                    "platform_settings": {},
                    "admins_id": [],
                }
            )
        )
        event = self.event()
        with (
            patch(
                "astrbot.core.pipeline.waking_check.stage.star_handlers_registry.get_handlers_by_event_type",
                return_value=[handler],
            ),
            patch(
                "astrbot.core.pipeline.waking_check.stage.SessionPluginManager.filter_handlers_by_session",
                new=AsyncMock(side_effect=lambda event, handlers: handlers),
            ),
        ):
            await stage.process(event)
        self.assertIn(handler, event.get_extra("activated_handlers"))
        self.assertFalse(event.is_at_or_wake_command)
        await self.plugin.keyword_mute(event)
        self.api.mute_member.assert_awaited_once()
        self.assertTrue(event.is_stopped())
