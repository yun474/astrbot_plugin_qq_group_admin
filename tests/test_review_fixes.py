import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot_plugin_qq_group_admin.main import QQGroupAdminPlugin, format_request


class ReviewFixTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = object.__new__(QQGroupAdminPlugin)
        self.plugin.config = {
            "join_request_settings": {"join_request_page_size": 55},
        }
        self.plugin._is_qq_group = lambda event: True
        self.plugin._can_manage = lambda event: True
        self.plugin._event_feature_setting = lambda event, key, default=True: (
            self.plugin.config.get(key, default)
        )
        self.plugin._platform = lambda event: SimpleNamespace(client=object())
        self.plugin._mentioned_members = lambda event: ["first", "second", "third"]
        self.event = SimpleNamespace(
            get_group_id=lambda: "group",
            get_platform_name=lambda: "qq_official",
            get_message_str=lambda: "/禁言 2分",
            plain_result=lambda text: text,
        )

    async def test_list_preserves_review_identifiers_and_cursor(self):
        item = {
            "username": "测试成员",
            "member_openid": "member-unique-123",
            "join_request_id": "request-unique-456",
        }
        api = SimpleNamespace(
            list_join_requests=AsyncMock(
                return_value={"list": [item], "next_cursor": "next-page"}
            )
        )
        with patch(
            "astrbot_plugin_qq_group_admin.main.QQGroupManageAPI", return_value=api
        ):
            text = await self.plugin.list_join_requests_tool(self.event)
        self.assertIn("member_openid：member-unique-123", text)
        self.assertIn("join_request_id：request-unique-456", text)
        self.assertIn("下一页 cursor：next-page", text)
        for markdown in (False, True):
            notice = format_request(item, markdown=markdown)
            self.assertNotIn(item["member_openid"], notice)
            self.assertNotIn(item["join_request_id"], notice)

    async def test_list_uses_configured_default_or_explicit_limit(self):
        api = SimpleNamespace(list_join_requests=AsyncMock(return_value={"list": []}))
        with patch(
            "astrbot_plugin_qq_group_admin.main.QQGroupManageAPI", return_value=api
        ):
            for arguments, expected in (
                ({}, 55),
                ({"limit": 0}, 55),
                ({"limit": 7}, 7),
            ):
                with self.subTest(arguments=arguments):
                    await self.plugin.list_join_requests_tool(
                        self.event, cursor="page", **arguments
                    )
                    api.list_join_requests.assert_awaited_with(
                        "group", cursor="page", limit=expected
                    )

    async def test_batch_continues_after_failure_even_when_success_is_silent(self):
        for command, seconds in (("mute_command", 120), ("unmute_command", 0)):
            for silent in (False, True):
                with self.subTest(command=command, silent=silent):
                    self.plugin.config["silent_mute_success_notice"] = silent
                    self.plugin._mute = AsyncMock(
                        side_effect=[{}, RuntimeError("无法操作该成员"), {}]
                    )
                    results = [
                        text async for text in getattr(self.plugin, command)(self.event)
                    ]
                    self.assertEqual(self.plugin._mute.await_count, 3)
                    self.plugin._mute.assert_awaited_with(
                        self.event, "group", "third", seconds
                    )
                    self.assertEqual(len(results), 1)
                    self.assertIn("成功 2 名，失败 1 名", results[0])
                    self.assertIn("second：无法操作该成员", results[0])

    async def test_batch_reports_all_failures(self):
        self.plugin._mute = AsyncMock(side_effect=RuntimeError("无权限"))
        results = [text async for text in self.plugin.unmute_command(self.event)]
        self.assertIn("成功 0 名，失败 3 名", results[0])
        for member in ("first", "second", "third"):
            self.assertIn(f"{member}：无权限", results[0])

    async def test_success_notice_respects_silent_setting(self):
        for command in ("mute_command", "unmute_command"):
            for silent in (False, True):
                with self.subTest(command=command, silent=silent):
                    self.plugin.config["silent_mute_success_notice"] = silent
                    self.plugin._mute = AsyncMock(return_value={})
                    results = [
                        text async for text in getattr(self.plugin, command)(self.event)
                    ]
                    self.assertEqual(self.plugin._mute.await_count, 3)
                    if silent:
                        self.assertEqual(results, [])
                    else:
                        self.assertIn("3 名成员", results[0])

    async def test_invalid_duration_does_not_start_batch(self):
        self.event.get_message_str = lambda: "/禁言 错误时长"
        self.plugin._mute = AsyncMock()
        results = [text async for text in self.plugin.mute_command(self.event)]
        self.assertIn("禁言失败", results[0])
        self.plugin._mute.assert_not_awaited()
