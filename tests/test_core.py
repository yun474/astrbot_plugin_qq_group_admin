import asyncio
import tempfile
import unittest
from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.message_components import At, Plain, Reply

from astrbot_plugin_qq_group_admin.main import (
    extract_mute_duration,
    format_apply_source,
    format_group_admin_help,
    format_mute_status,
    format_request,
    parse_duration,
    QQGroupAdminPlugin,
    GROUP_AND_C2C_INTENT,
    GROUP_MEMBER_INTENT,
    LIFECYCLE_EVENTS,
    quoted_join_request_index,
    render_member_notice,
    review_keyboard,
    review_action_text,
    resolve_tool_event,
)
from astrbot_plugin_qq_group_admin.api import QQBotRoute, QQGroupManageAPI
from astrbot_plugin_qq_group_admin.storage import PluginStorage


async def _collect_async_generator(generator):
    return [item async for item in generator]


class CoreTests(unittest.TestCase):
    def test_pending_request_can_be_found_without_reply_message_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = PluginStorage(Path(temp_dir) / "state.json")
            storage.put_pending(
                "notification-1",
                {
                    "group_openid": "group-1",
                    "join_request_id": "request-1",
                },
            )
            matched = storage.find_pending_by_join_request_id(
                "request-1",
                "group-1",
            )

        self.assertIsNotNone(matched)
        self.assertEqual(matched[0], "notification-1")
        self.assertEqual(matched[1]["join_request_id"], "request-1")

    def test_pending_request_gets_stable_per_group_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = PluginStorage(Path(temp_dir) / "state.json")
            key, index = storage.reserve_pending(
                {
                    "group_openid": "group-1",
                    "join_request_id": "request-1",
                }
            )
            storage.bind_pending_message(key, "notification-1")
            matched = storage.find_pending_by_index("group-1", 1)

        self.assertEqual(index, 1)
        self.assertEqual(matched[0], "notification-1")
        self.assertEqual(matched[1]["join_request_id"], "request-1")

    def test_group_pending_reset_clears_only_target_group_and_restarts_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = PluginStorage(Path(temp_dir) / "state.json")
            storage.reserve_pending(
                {"group_openid": "group-1", "join_request_id": "request-1"}
            )
            storage.reserve_pending(
                {"group_openid": "group-2", "join_request_id": "request-2"}
            )

            removed = storage.reset_group_pending("group-1")
            _, restarted_index = storage.reserve_pending(
                {"group_openid": "group-1", "join_request_id": "request-3"}
            )

        self.assertEqual(removed, 1)
        self.assertEqual(restarted_index, 1)
        self.assertIsNotNone(storage.find_pending_by_index("group-2", 1))

    def test_group_pending_reset_command_checks_permission(self) -> None:
        class Event:
            @staticmethod
            def get_group_id() -> str:
                return "group-1"

            @staticmethod
            def plain_result(text: str) -> str:
                return text

            @staticmethod
            def is_admin() -> bool:
                return False

            @staticmethod
            def get_sender_id() -> str:
                return "plugin-group-admin"

            @staticmethod
            def get_self_id() -> str:
                return "bot"

        plugin = object.__new__(QQGroupAdminPlugin)
        plugin._is_qq_group = lambda event: True
        plugin.storage = SimpleNamespace(reset_group_pending=lambda group_id: 99)

        results = asyncio.run(
            _collect_async_generator(plugin.reset_join_requests(Event()))
        )

        self.assertEqual(results, ["只有 AstrBot 管理员能重置入群申请编号。"])

    def test_reply_review_uses_only_new_plain_text(self) -> None:
        reply = Reply(
            id="",
            message_str="#1 新的入群申请",
            chain=[Plain(text="#1 新的入群申请")],
        )

        class Event:
            @staticmethod
            def get_messages():
                return [reply, At(qq="bot-openid"), Plain(text="同意")]

            @staticmethod
            def get_message_str() -> str:
                return "#1 新的入群申请 @bot 同意"

        self.assertEqual(review_action_text(Event()), "同意")
        self.assertEqual(quoted_join_request_index(reply), 1)

    def test_reply_review_handles_empty_reply_id_and_stops_other_plugins(self) -> None:
        reply = Reply(
            id="",
            message_str="#1 新的入群申请",
            chain=[Plain(text="#1 新的入群申请")],
        )

        class Event:
            stopped = False

            @staticmethod
            def get_messages():
                return [reply, At(qq="bot-openid"), Plain(text="同意")]

            @staticmethod
            def get_group_id() -> str:
                return "group-1"

            @classmethod
            def stop_event(cls) -> None:
                cls.stopped = True

            @staticmethod
            def plain_result(text: str) -> str:
                return text

        pending = {
            "group_openid": "group-1",
            "member_openid": "member-1",
            "join_request_id": "request-1",
        }
        removed = []
        plugin = object.__new__(QQGroupAdminPlugin)
        plugin.config = {
            "enable_join_reply_review": True,
        }
        plugin.storage = SimpleNamespace(
            get_pending=lambda message_id: None,
            find_pending_by_index=lambda group_id, index: (
                "notification-1",
                pending,
            ),
            find_pending_by_join_request_id=lambda request_id, group_id: None,
            remove_pending=lambda message_id: removed.append(message_id),
        )
        plugin._is_qq_group = lambda event: True
        plugin._can_manage = lambda event: True
        plugin._review = AsyncMock(return_value={})

        event = Event()

        async def run_handler():
            return [item async for item in plugin.reply_review(event)]

        results = asyncio.run(run_handler())

        self.assertTrue(Event.stopped)
        plugin._review.assert_awaited_once_with(
            event,
            "group-1",
            "member-1",
            "request-1",
            True,
            "",
        )
        self.assertEqual(removed, ["notification-1"])
        self.assertEqual(results, ["已同意入群申请。"])

    def test_index_review_checks_permission_and_stops_other_plugins(self) -> None:
        class Event:
            stopped = False

            @staticmethod
            def get_messages():
                return [Plain(text="/拒绝 7 测试理由")]

            @staticmethod
            def get_group_id() -> str:
                return "group-1"

            @classmethod
            def stop_event(cls) -> None:
                cls.stopped = True

            @staticmethod
            def plain_result(text: str) -> str:
                return text

        plugin = object.__new__(QQGroupAdminPlugin)
        plugin.config = {
            "enable_join_reply_review": True,
        }
        plugin.storage = SimpleNamespace(
            find_pending_by_index=lambda group_id, index: (
                "notification-7",
                {
                    "group_openid": "group-1",
                    "member_openid": "member-1",
                    "join_request_id": "request-7",
                },
            )
        )
        plugin._is_qq_group = lambda event: True
        plugin._can_manage = lambda event: False
        plugin._review = AsyncMock(return_value={})

        event = Event()
        results = asyncio.run(
            _collect_async_generator(plugin.reply_review(event))
        )

        self.assertTrue(event.stopped)
        plugin._review.assert_not_awaited()
        self.assertEqual(
            results,
            ["你不是本群群管，也不是 AstrBot 管理员。"],
        )

    def test_request_notice_hides_ids_and_translates_source(self) -> None:
        text = format_request(
            {
                "username": "测试用户",
                "member_openid": "member-secret",
                "join_request_id": "request-secret",
                "apply_source": "self_apply",
                "verify_info": {
                    "method": "verify_message",
                    "verify_message": "答案",
                },
            },
            3,
        )
        self.assertIn("#3 新的入群申请", text)
        self.assertIn("来源：自主申请", text)
        self.assertIn("验证消息：答案", text)
        self.assertNotIn("member-secret", text)
        self.assertNotIn("request-secret", text)
        self.assertNotIn("验证方式", text)
        self.assertEqual(format_apply_source("unknown-value"), "其他来源")

    def test_review_keyboard_uses_commands(self) -> None:
        keyboard = review_keyboard(4)
        buttons = keyboard["content"]["rows"][0]["buttons"]
        self.assertEqual(buttons[0]["action"]["data"], "/同意 4")
        self.assertEqual(buttons[1]["action"]["data"], "/拒绝 4")
        self.assertEqual(buttons[0]["action"]["permission"]["type"], 2)

    def test_keyboard_is_attached_to_markdown_payload(self) -> None:
        api = QQGroupManageAPI(object())
        api._request = AsyncMock(return_value={"id": "message-1"})
        keyboard = review_keyboard(2)

        asyncio.run(api.send_group_markdown("group-1", "申请内容", keyboard=keyboard))

        payload = api._request.await_args.kwargs["payload"]
        self.assertEqual(payload["msg_type"], 2)
        self.assertEqual(payload["markdown"], {"content": "申请内容"})
        self.assertIs(payload["keyboard"], keyboard)

    def test_hot_install_patches_existing_connection(self) -> None:
        dispatched = []

        class Client:
            intents = GROUP_AND_C2C_INTENT
            _active_websockets = set()

            def __init__(self) -> None:
                self._connection = SimpleNamespace(parser={}, _session_list=[])

            @staticmethod
            def ws_dispatch(event_name, data) -> None:
                dispatched.append((event_name, data))

        client = Client()
        platform = SimpleNamespace(
            client=client,
            intents=SimpleNamespace(value=GROUP_AND_C2C_INTENT),
            meta=lambda: SimpleNamespace(name="qq_official", id="platform-1"),
        )
        plugin = object.__new__(QQGroupAdminPlugin)
        plugin.config = {}
        plugin.context = SimpleNamespace(
            platform_manager=SimpleNamespace(platform_insts=[platform])
        )
        plugin._patched = {}

        asyncio.run(plugin._patch_platforms_once())

        self.assertTrue(set(LIFECYCLE_EVENTS).issubset(client._connection.parser))
        client._connection.parser["group_member_remove"](
            {"id": "event-1", "d": {"group_openid": "group-1"}}
        )
        self.assertEqual(
            dispatched,
            [
                (
                    "group_member_remove",
                    {"group_openid": "group-1", "_event_id": "event-1"},
                )
            ],
        )

    def test_lifecycle_parsers_exist_before_connection_is_created(self) -> None:
        from botpy.connection import ConnectionState

        attrs = [f"parse_{event_name}" for event_name in LIFECYCLE_EVENTS]
        originals = {attr: getattr(ConnectionState, attr, None) for attr in attrs}
        for attr in attrs:
            if hasattr(ConnectionState, attr):
                delattr(ConnectionState, attr)

        plugin = object.__new__(QQGroupAdminPlugin)
        plugin._parser_state_class = None
        plugin._owned_parser_methods = {}
        captured = []
        try:
            plugin._install_parser_patch()
            state = ConnectionState(
                lambda event_name, data: captured.append((event_name, data)),
                api=None,
            )
            self.assertTrue(set(LIFECYCLE_EVENTS).issubset(state.parsers))

            state.parsers["group_join_request"](
                {"id": "event-1", "d": {"group_openid": "group-1"}}
            )
            self.assertEqual(
                captured,
                [
                    (
                        "group_join_request",
                        {"group_openid": "group-1", "_event_id": "event-1"},
                    )
                ],
            )
        finally:
            for attr in attrs:
                if hasattr(ConnectionState, attr):
                    delattr(ConnectionState, attr)
                if originals[attr] is not None:
                    setattr(ConnectionState, attr, originals[attr])

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
        self.assertTrue(client.intents & GROUP_AND_C2C_INTENT)
        self.assertTrue(platform.intents.value & GROUP_MEMBER_INTENT)
        self.assertTrue(platform.intents.value & GROUP_AND_C2C_INTENT)
        self.assertTrue(pending_session["intent"] & GROUP_MEMBER_INTENT)
        self.assertTrue(pending_session["intent"] & GROUP_AND_C2C_INTENT)
        self.assertTrue(websocket._session["intent"] & GROUP_MEMBER_INTENT)
        self.assertTrue(websocket._session["intent"] & GROUP_AND_C2C_INTENT)
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
        )
        api.send_group_markdown.assert_awaited_once_with(
            "group-1",
            '欢迎 <qqbot-at-user id="member-1" /> 加入群聊！',
        )

    def test_join_request_notice_is_sent_as_proactive_message(self) -> None:
        api = SimpleNamespace(
            send_group_markdown=AsyncMock(return_value={"id": "msg-1"}),
            send_group_text=AsyncMock(),
        )
        stored = []
        plugin = object.__new__(QQGroupAdminPlugin)
        plugin.config = {
            "enable_join_notice": True,
            "enable_join_reply_review": True,
        }
        plugin.context = SimpleNamespace(
            get_platform_inst=lambda platform_id: SimpleNamespace(client=object())
        )
        plugin.storage = SimpleNamespace(
            reserve_pending=lambda item: (stored.append(item) or ("reserved-1", 1)),
            bind_pending_message=lambda pending_key, message_id: message_id,
            remove_pending=lambda pending_key: None,
        )

        with patch(
            "astrbot_plugin_qq_group_admin.main.QQGroupManageAPI",
            return_value=api,
        ):
            asyncio.run(
                plugin._handle_join_request_event(
                    "platform-1",
                    {
                        "group_openid": "group-1",
                        "member_openid": "member-1",
                        "join_request_id": "request-1",
                        "_event_id": "event-1",
                    },
                )
            )

        api.send_group_markdown.assert_awaited_once()
        api.send_group_text.assert_not_awaited()
        self.assertNotIn("event_id", api.send_group_markdown.await_args.kwargs)
        self.assertEqual(stored[0]["join_request_id"], "request-1")
        self.assertIn("keyboard", api.send_group_markdown.await_args.kwargs)

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
