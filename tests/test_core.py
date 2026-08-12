import asyncio
import tempfile
import unittest
from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.message_components import At, Plain

from astrbot_plugin_qq_group_admin.main import (
    extract_mute_duration,
    format_group_admin_help,
    format_mute_status,
    format_request,
    parse_duration,
    QQGroupAdminPlugin,
    GROUP_MEMBER_INTENT,
    render_member_notice,
    resolve_tool_event,
)
from astrbot_plugin_qq_group_admin.api import QQBotRoute
from astrbot_plugin_qq_group_admin.storage import PluginStorage


class CoreTests(unittest.TestCase):
    def test_group_member_intent_updates_sessions_and_reconnects(self) -> None:
        class WebSocket:
            def __init__(self) -> None:
                self._session = {
                    "intent": 1,
                    "session_id": "old-session",
                    "last_seq": 8,
                }
                self.closed = False

            async def close(self) -> None:
                self.closed = True

        websocket = WebSocket()
        pending_session = {"intent": 1}
        client = SimpleNamespace(
            intents=1,
            _connection=SimpleNamespace(_session_list=[pending_session]),
            _active_websockets={websocket},
        )
        platform = SimpleNamespace(intents=SimpleNamespace(value=1))
        plugin = object.__new__(QQGroupAdminPlugin)

        asyncio.run(plugin._ensure_group_member_intent(platform, client))

        self.assertTrue(client.intents & GROUP_MEMBER_INTENT)
        self.assertTrue(platform.intents.value & GROUP_MEMBER_INTENT)
        self.assertTrue(pending_session["intent"] & GROUP_MEMBER_INTENT)
        self.assertTrue(websocket._session["intent"] & GROUP_MEMBER_INTENT)
        self.assertEqual(websocket._session["session_id"], "")
        self.assertEqual(websocket._session["last_seq"], 0)
        self.assertTrue(websocket.closed)

    def test_join_markdown_failure_falls_back_to_plain_text(self) -> None:
        api = SimpleNamespace(
            send_group_markdown=AsyncMock(side_effect=RuntimeError("no markdown")),
            send_group_text=AsyncMock(return_value={}),
        )
        plugin = object.__new__(QQGroupAdminPlugin)
        plugin.config = {
            "enable_member_join_notice": True,
            "member_join_message": "欢迎 {member_at} 加入群聊！",
        }
        plugin.context = SimpleNamespace(
            get_platform_inst=lambda platform_id: SimpleNamespace(client=object())
        )

        with patch(
            "astrbot_plugin_qq_group_admin.main.QQGroupManageAPI",
            return_value=api,
        ):
            asyncio.run(
                plugin._handle_member_event(
                    "platform-1",
                    "member_join",
                    {
                        "group_openid": "group-1",
                        "member_openid": "member-1",
                        "_event_id": "event-1",
                    },
                )
            )

        api.send_group_text.assert_awaited_once_with(
            "group-1",
            "欢迎 member-1 加入群聊！",
            event_id="event-1",
        )

    def test_silent_notice_config_never_stops_llm_natural_reply(self) -> None:
        class Event:
            @staticmethod
            def get_platform_name() -> str:
                return "qq_official"

            @staticmethod
            def get_group_id() -> str:
                return "group-1"

        async def fake_mute(*args, **kwargs):
            return {}

        plugin = object.__new__(QQGroupAdminPlugin)
        plugin.config = {
            "enable_mute_tool": True,
            "silent_mute_success_notice": True,
        }
        plugin._mute = fake_mute
        plugin._validate_duration = lambda seconds: None
        result = asyncio.run(plugin.mute_tool(Event(), "member-1", "1分"))
        self.assertIn("请根据用户语境自然回复", result)
        self.assertNotIn("不要在最终回复", result)

    def test_llm_executor_at_component_is_detected(self) -> None:
        class MessageObject:
            raw_message = None
            message = [
                Plain(text="/禁言"),
                At(qq="MEMBER_OPENID"),
                Plain(text=" 3分"),
            ]

        class Event:
            message_obj = MessageObject()

            @classmethod
            def get_messages(cls):
                return cls.message_obj.message

            @staticmethod
            def get_self_id() -> str:
                return "BOT_OPENID"

        plugin = object.__new__(QQGroupAdminPlugin)
        self.assertEqual(plugin._mentioned_members(Event()), ["MEMBER_OPENID"])

    def test_resolve_astrbot_426_tool_context(self) -> None:
        expected_event = object()

        class AgentContext:
            event = expected_event

        class ContextWrapper:
            context = AgentContext()

        self.assertIs(resolve_tool_event(ContextWrapper()), expected_event)

    def test_resolve_legacy_tool_event(self) -> None:
        class Event:
            @staticmethod
            def get_platform_name() -> str:
                return "qq_official"

            @staticmethod
            def get_group_id() -> str:
                return "group-1"

        event = Event()
        self.assertIs(resolve_tool_event(event), event)

    def test_llm_tools_return_text_to_continue_agent_loop(self) -> None:
        tool_names = (
            "mute_tool",
            "unmute_tool",
            "mute_status_tool",
            "list_join_requests_tool",
            "review_join_request_tool",
        )
        for name in tool_names:
            self.assertEqual(signature(getattr(QQGroupAdminPlugin, name)).return_annotation, "str")

    def test_bot_sender_has_group_management_permission(self) -> None:
        class Storage:
            @staticmethod
            def group_admins(group_id: str) -> list[str]:
                return []

        class Event:
            @staticmethod
            def get_sender_id() -> str:
                return "bot-1"

            @staticmethod
            def get_self_id() -> str:
                return "bot-1"

            @staticmethod
            def get_group_id() -> str:
                return "group-1"

            @staticmethod
            def is_admin() -> bool:
                return False

        plugin = object.__new__(QQGroupAdminPlugin)
        plugin.storage = Storage()
        self.assertTrue(plugin._can_manage(Event()))

    def test_group_admin_help_uses_configured_default(self) -> None:
        text = format_group_admin_help("3分")
        self.assertIn("# 🛡️ QQ 群管帮助", text)
        self.assertIn("默认 **3分**", text)
        self.assertIn("`/解禁 @用户`", text)
        self.assertIn("`/禁言状态`", text)

    def test_format_mute_status_contains_rules_and_member(self) -> None:
        text = format_mute_status(
            {
                "global_rule": {
                    "mode": "schedule",
                    "recurring_rules": [
                        {
                            "task_id": "task-1",
                            "weekdays": [1, 7],
                            "start_time": "23:00",
                            "end_time": "07:00",
                            "enabled": True,
                        }
                    ],
                },
                "members": [
                    {
                        "username": "测试用户",
                        "member_openid": "member-1",
                        "mute_expire_at": "2026-08-12T12:00:00+08:00",
                    }
                ],
            }
        )
        self.assertIn("按规则全员禁言", text)
        self.assertIn("周一、周日 23:00-07:00", text)
        self.assertIn("测试用户（member-1）", text)

    def test_missing_duration_uses_configured_default(self) -> None:
        self.assertEqual(
            extract_mute_duration("禁言 <@member-1>", "20分"),
            "20分",
        )

    def test_explicit_duration_overrides_default(self) -> None:
        self.assertEqual(
            extract_mute_duration("禁言 <@member-1> 2小时", "20分"),
            "2小时",
        )

    def test_llm_executor_text_at_does_not_become_duration(self) -> None:
        self.assertEqual(
            extract_mute_duration("/禁言 @MEMBER_OPENID 3分", "1分"),
            "3分",
        )
        self.assertEqual(
            extract_mute_duration("/禁言 @MEMBER_OPENID", "1分"),
            "1分",
        )

    def test_qq_bot_xml_at_does_not_become_duration(self) -> None:
        self.assertEqual(
            extract_mute_duration(
                '/禁言 <qqbot-at-user id="MEMBER_OPENID" /> 4分',
                "1分",
            ),
            "4分",
        )

    def test_mute_command_has_no_positional_parameters(self) -> None:
        params = list(signature(QQGroupAdminPlugin.mute_command).parameters)
        self.assertEqual(params, ["self", "event"])

    def test_member_notice_only_renders_at_placeholder(self) -> None:
        text = render_member_notice(
            "欢迎 {member_at}，{member_nickname}",
            "member-1",
            can_at=True,
        )
        self.assertEqual(
            text,
            '欢迎 <qqbot-at-user id="member-1" />，{member_nickname}',
        )

    def test_leave_notice_cannot_at(self) -> None:
        text = render_member_notice(
            "{member_at} 退出了群聊",
            "member-1",
            can_at=False,
        )
        self.assertEqual(text, "member-1 退出了群聊")
        self.assertNotIn("qqbot-at-user", text)

    def test_new_openapi_domain(self) -> None:
        route = QQBotRoute("GET", "/v2/groups/test/join_request_list")
        self.assertEqual(
            route.url,
            "https://api.bot.qq.com/v2/groups/test/join_request_list",
        )

    def test_parse_duration(self) -> None:
        self.assertEqual(parse_duration("10分"), 600)
        self.assertEqual(parse_duration("1天2小时30分"), 95400)
        self.assertEqual(parse_duration("15"), 900)
        self.assertEqual(parse_duration("解除"), 0)

    def test_format_request_contains_qa(self) -> None:
        text = format_request(
            {
                "username": "测试用户",
                "member_openid": "member-1",
                "join_request_id": "request-1",
                "apply_source": "search",
                "verify_info": {
                    "method": "qa",
                    "review_qa_list": [{"question": "答案？", "answer": "42"}],
                },
            }
        )
        self.assertIn("测试用户", text)
        self.assertIn("答案？ / 42", text)

    def test_storage_is_scoped_by_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = PluginStorage(path)
            self.assertTrue(store.add_group_admin("group-a", "user-1"))
            self.assertFalse(store.add_group_admin("group-a", "user-1"))
            self.assertEqual(store.group_admins("group-a"), ["user-1"])
            self.assertEqual(store.group_admins("group-b"), [])
            reloaded = PluginStorage(path)
            self.assertEqual(reloaded.group_admins("group-a"), ["user-1"])


if __name__ == "__main__":
    unittest.main()
