import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from astrbot.api.message_components import Plain, Reply
from astrbot_plugin_qq_group_admin.api import QQGroupManageAPI
from astrbot_plugin_qq_group_admin.callback_guard import ReviewCallbackGuard
from astrbot_plugin_qq_group_admin.main import (
    INTERACTION_INTENT,
    QQGroupAdminPlugin,
    format_request,
    review_keyboard,
)
from astrbot_plugin_qq_group_admin.storage import PluginStorage
from botpy.connection import ConnectionState
from botpy.interaction import Interaction


class Event:
    def __init__(
        self, sender="member", role="member", astr_admin=False, reply=None, text="同意"
    ):
        self.sender, self.astr_admin, self.reply = sender, astr_admin, reply
        self.message_obj = NS(raw_message={"author": {"member_role": role}})
        self.unified_msg_origin = "p:GroupMessage:g"
        self.text = text
        self.sent = []
        self.stopped = False

    def get_sender_id(self):
        return self.sender

    def get_self_id(self):
        return "bot"

    def get_group_id(self):
        return "g"

    def get_platform_id(self):
        return "p"

    def get_platform_name(self):
        return "qq_official"

    def is_admin(self):
        return self.astr_admin

    def get_messages(self):
        return ([self.reply] if self.reply else []) + [Plain(text=self.text)]

    def stop_event(self):
        self.stopped = True

    async def send(self, result):
        self.sent.append(result)

    def plain_result(self, text):
        return text


class InteractionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plugin = object.__new__(QQGroupAdminPlugin)
        self.plugin.config = {}
        self.plugin.storage = PluginStorage(Path(self.tmp.name) / "state.json")
        self.plugin._reviews_inflight = set()
        self.plugin._callback_guard = ReviewCallbackGuard()
        self.now = 1000.0
        clock = patch(
            "astrbot_plugin_qq_group_admin.callback_guard.monotonic",
            side_effect=lambda: self.now,
        )
        clock.start()
        self.addCleanup(clock.stop)
        self.plugin._patched = {}
        self.plugin._patch_task = None
        self.plugin._parser_state_class = None
        self.plugin._owned_parser_methods = {}
        self.client = NS(
            _connection=NS(parser={}, _session_list=[]),
            intents=0,
            _active_websockets=set(),
            ws_dispatch=lambda *args: None,
        )
        self.platform = NS(
            client=self.client, meta=lambda: NS(name="qq_official", id="p")
        )
        self.plugin.context = NS(
            get_platform_inst=lambda pid: self.platform,
            get_config=lambda umo: {"admins_id": ["astr-admin"]},
            platform_manager=NS(platform_insts=[self.platform]),
        )
        self.api = NS(
            acknowledge_interaction=AsyncMock(),
            review_join_request=AsyncMock(),
            send_group_text=AsyncMock(),
            get_mute_status=AsyncMock(return_value={}),
            list_join_requests=AsyncMock(return_value={"list": []}),
            get_group_member_info=AsyncMock(return_value={"member_role": "member"}),
        )
        patcher = patch(
            "astrbot_plugin_qq_group_admin.main.QQGroupManageAPI", return_value=self.api
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.token = "a" * 32
        self.pending = {
            "platform_id": "p",
            "group_openid": "g",
            "member_openid": "applicant",
            "join_request_id": "request",
            "callback_token": self.token,
        }
        self.plugin.storage.put_pending("notice", self.pending)

    def interaction(
        self, action="approve", audience="shared", sender="astr-admin", **extra
    ):
        payload = {
            "id": "interaction-id",
            "type": 11,
            "chat_type": 1,
            "scene": "group",
            "group_openid": "g",
            "group_member_openid": sender,
            "data": {
                "type": 11,
                "resolved": {
                    "button_data": f"qqga:{self.token}:{action}:{audience}",
                    "button_id": f"qqga-{action}-{audience}",
                },
            },
        }
        payload.update(extra)
        return Interaction(None, "outer-event", payload)

    async def test_shared_callback_approves_once_and_survives_storage_reload(self):
        self.plugin.storage = PluginStorage(self.plugin.storage.path)
        callback = self.interaction()
        self.assertTrue(await self.plugin._handle_review_interaction("p", callback))
        self.api.review_join_request.assert_awaited_once_with(
            "g", "applicant", "request", approve=True
        )
        self.api.get_group_member_info.assert_not_awaited()
        self.api.send_group_text.assert_awaited_once_with(
            "g", "已同意入群申请。", event_id="outer-event"
        )
        self.assertIsNone(self.plugin.storage.get_pending("notice"))
        self.now += 2
        await self.plugin._handle_review_interaction("p", callback)
        self.assertEqual(self.api.review_join_request.await_count, 1)
        self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 3)

    async def test_shared_callback_queries_native_admin_and_owner_roles(self):
        for role in ("admin", "owner"):
            self.now += 2
            self.plugin.storage.put_pending("notice", self.pending)
            self.api.get_group_member_info.return_value = {
                "member_openid": "qq-admin",
                "member_role": role,
            }
            await self.plugin._handle_review_interaction(
                "p", self.interaction("decline", sender="qq-admin")
            )
            self.api.get_group_member_info.assert_awaited_with("g", "qq-admin")
            self.api.review_join_request.assert_awaited_with(
                "g", "applicant", "request", approve=False
            )
        self.assertEqual(self.api.review_join_request.await_count, 2)

    async def test_role_lookup_fails_closed(self):
        for result in (
            None,
            {},
            {"member_openid": "member", "member_role": "member"},
            {"member_openid": "someone-else", "member_role": "owner"},
            {"member_openid": "member", "member_role": "unknown"},
        ):
            self.now += 120
            self.api.get_group_member_info.return_value = result
            await self.plugin._handle_review_interaction(
                "p", self.interaction(sender="member")
            )
            self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 4)
        for error in (RuntimeError("11253: no API permission"), TimeoutError()):
            self.now += 120
            self.api.get_group_member_info.side_effect = error
            await self.plugin._handle_review_interaction(
                "p", self.interaction(sender="member")
            )
            self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 1)
        self.api.review_join_request.assert_not_awaited()
        self.assertIsNotNone(self.plugin.storage.get_pending("notice"))
        self.assertFalse(self.plugin._reviews_inflight)

    async def test_native_role_is_rechecked_after_demotion(self):
        self.api.get_group_member_info.return_value = {
            "member_openid": "qq-admin",
            "member_role": "admin",
        }
        self.api.review_join_request.side_effect = RuntimeError("retry later")
        await self.plugin._handle_review_interaction(
            "p", self.interaction(sender="qq-admin")
        )
        self.api.get_group_member_info.return_value["member_role"] = "member"
        self.now += 2
        await self.plugin._handle_review_interaction(
            "p", self.interaction(sender="qq-admin")
        )
        self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 4)
        self.assertEqual(self.api.get_group_member_info.await_count, 2)
        self.assertEqual(self.api.review_join_request.await_count, 1)

    async def test_resolved_request_during_role_lookup_is_not_approved_again(self):
        async def get_member(*args):
            self.plugin.storage.remove_pending("notice")
            return {"member_openid": "qq-admin", "member_role": "admin"}

        self.api.get_group_member_info.side_effect = get_member
        await self.plugin._handle_review_interaction(
            "p", self.interaction(sender="qq-admin")
        )
        self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 3)
        self.api.review_join_request.assert_not_awaited()

    async def test_old_button_audience_cannot_bypass_server_authorization(self):
        for audience in ("native", "assigned"):
            self.now += 2
            await self.plugin._handle_review_interaction(
                "p", self.interaction(audience=audience, sender="member")
            )
            self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 4)
            self.api.review_join_request.assert_not_awaited()
            self.plugin.storage.add_group_admin("g", "plugin-admin")
            self.plugin.storage.put_pending("notice", self.pending)
            await self.plugin._handle_review_interaction(
                "p", self.interaction(audience=audience, sender="plugin-admin")
            )
            self.api.review_join_request.assert_awaited_once()
            self.api.review_join_request.reset_mock()
            self.plugin.storage.put_pending("notice", self.pending)

    async def test_shared_callback_denies_removed_or_ordinary_admin(self):
        self.plugin.storage.add_group_admin("g", "plugin-admin")
        self.plugin.storage.remove_group_admin("g", "plugin-admin")
        for sender in ("member", "plugin-admin"):
            await self.plugin._handle_review_interaction(
                "p", self.interaction(sender=sender)
            )
            self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 4)
        self.api.review_join_request.assert_not_awaited()

    async def test_plugin_admin_callback_is_allowed(self):
        self.plugin.storage.add_group_admin("g", "plugin-admin")
        await self.plugin._handle_review_interaction(
            "p", self.interaction(sender="plugin-admin")
        )
        self.api.review_join_request.assert_awaited_once()
        self.api.get_group_member_info.assert_not_awaited()

    async def test_denied_clicks_return_no_permission_without_requery(self):
        self.api.get_group_member_info.return_value = {
            "member_openid": "member",
            "member_role": "member",
        }
        for _ in range(50):
            await self.plugin._handle_review_interaction(
                "p", self.interaction(sender="member")
            )
            self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 4)
        self.api.get_group_member_info.assert_awaited_once()
        self.api.review_join_request.assert_not_awaited()
        self.api.send_group_text.assert_not_awaited()
        self.now += 119
        await self.plugin._handle_review_interaction(
            "p", self.interaction(sender="member")
        )
        self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 4)
        self.api.get_group_member_info.assert_awaited_once()
        self.now += 1
        await self.plugin._handle_review_interaction(
            "p", self.interaction(sender="member")
        )
        self.assertEqual(self.api.get_group_member_info.await_count, 2)

    async def test_new_plugin_admin_bypasses_cached_denial(self):
        await self.plugin._handle_review_interaction(
            "p", self.interaction(sender="member")
        )
        self.plugin.storage.add_group_admin("g", "member")
        self.now += 2
        await self.plugin._handle_review_interaction(
            "p", self.interaction(sender="member")
        )
        self.api.get_group_member_info.assert_awaited_once()
        self.api.review_join_request.assert_awaited_once()

    async def test_same_user_cooldown_covers_different_requests(self):
        self.api.get_group_member_info.side_effect = TimeoutError()
        await self.plugin._handle_review_interaction(
            "p", self.interaction(sender="member")
        )
        self.token = "b" * 32
        self.plugin.storage.put_pending(
            "notice-2", {**self.pending, "callback_token": self.token}
        )
        await self.plugin._handle_review_interaction(
            "p", self.interaction(sender="member")
        )
        self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 2)
        self.api.get_group_member_info.assert_awaited_once()

    async def test_more_than_twenty_users_can_be_checked_in_one_minute(self):
        for i in range(25):
            await self.plugin._handle_review_interaction(
                "p", self.interaction(sender=f"member-{i}")
            )
        self.assertEqual(self.api.get_group_member_info.await_count, 25)
        self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 4)
        self.api.send_group_text.assert_not_awaited()
        await self.plugin._handle_review_interaction("p", self.interaction())
        self.api.review_join_request.assert_awaited_once()

    async def test_lookup_concurrency_is_bounded_and_cancelled_work_releases_slots(
        self,
    ):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def slow_lookup(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                entered.set()
            await release.wait()
            return {"member_openid": args[1], "member_role": "member"}

        self.api.get_group_member_info.side_effect = slow_lookup
        tasks = []
        try:
            for i in range(3):
                self.token = f"{i:032x}"
                self.plugin.storage.put_pending(
                    f"notice-{i}",
                    {
                        **self.pending,
                        "callback_token": self.token,
                        "join_request_id": f"request-{i}",
                    },
                )
                callback = self.interaction(sender=f"member-{i}")
                if i < 2:
                    tasks.append(
                        asyncio.create_task(
                            self.plugin._handle_review_interaction("p", callback)
                        )
                    )
                else:
                    await asyncio.wait_for(entered.wait(), 1)
                    await self.plugin._handle_review_interaction("p", callback)
            self.assertEqual(self.api.get_group_member_info.await_count, 2)
            self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 2)
            tasks[0].cancel()
            with self.assertRaises(asyncio.CancelledError):
                await tasks[0]
            self.assertEqual(self.plugin._callback_guard._active_lookups, 1)
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(self.plugin._callback_guard._active_lookups, 0)
        self.assertFalse(self.plugin._reviews_inflight)

    async def test_repeated_lookup_and_ack_errors_log_once_per_minute(self):
        self.api.get_group_member_info.side_effect = TimeoutError()
        self.api.acknowledge_interaction.side_effect = RuntimeError("ack failed")
        with patch("astrbot_plugin_qq_group_admin.main.logger.exception") as log:
            for i in range(4):
                await self.plugin._handle_review_interaction(
                    "p", self.interaction(sender=f"member-{i}")
                )
            self.assertEqual(log.call_count, 2)
            self.now += 60
            await self.plugin._handle_review_interaction(
                "p", self.interaction(sender="member-0")
            )
            self.assertEqual(log.call_count, 4)
        self.assertEqual(self.plugin._callback_guard._active_lookups, 0)

    async def test_cross_scope_private_and_mismatched_buttons_are_denied(self):
        cases = [
            ("other-platform", self.interaction()),
            ("p", self.interaction(group_openid="other-group")),
            ("p", self.interaction(chat_type=2)),
            ("p", self.interaction(type=12)),
        ]
        mismatch = self.interaction()
        mismatch.data.resolved.button_id = "qqga-decline-assigned"
        cases.append(("p", mismatch))
        for platform, callback in cases:
            await self.plugin._handle_review_interaction(platform, callback)
            self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 4)
        self.api.review_join_request.assert_not_awaited()

    async def test_feature_off_and_whitelist_are_enforced(self):
        for config in (
            {"enable_join_reply_review": False},
            {"enabled_group_umos": ["other"]},
        ):
            self.plugin.config = config
            await self.plugin._handle_review_interaction("p", self.interaction())
            self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 4)
        self.api.review_join_request.assert_not_awaited()

    async def test_expired_or_deleted_token_cannot_target_new_application(self):
        self.plugin.storage.data["pending"]["notice"]["stored_at"] = (
            datetime.now(timezone.utc) - timedelta(days=31)
        ).isoformat()
        self.plugin.storage.put_pending(
            "new-notice", {**self.pending, "callback_token": "b" * 32}
        )
        await self.plugin._handle_review_interaction("p", self.interaction())
        self.api.review_join_request.assert_not_awaited()
        self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 3)

    async def test_concurrent_clicks_ack_promptly_and_call_approval_once(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_review(*args, **kwargs):
            self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 0)
            entered.set()
            await release.wait()

        self.api.review_join_request.side_effect = slow_review
        first = asyncio.create_task(
            self.plugin._handle_review_interaction("p", self.interaction())
        )
        await entered.wait()
        await self.plugin._handle_review_interaction("p", self.interaction("decline"))
        self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 2)
        release.set()
        await first
        self.assertEqual(self.api.review_join_request.await_count, 1)

    async def test_send_failure_does_not_restore_completed_application(self):
        self.api.send_group_text.side_effect = RuntimeError("send failed")
        await self.plugin._handle_review_interaction("p", self.interaction())
        self.assertIsNone(self.plugin.storage.get_pending("notice"))
        self.assertFalse(self.plugin._reviews_inflight)

    async def test_approval_failure_keeps_mapping_for_explicit_retry(self):
        self.api.review_join_request.side_effect = RuntimeError("QQ API denied")
        await self.plugin._handle_review_interaction("p", self.interaction())
        self.assertIsNotNone(self.plugin.storage.get_pending("notice"))
        self.assertFalse(self.plugin._reviews_inflight)
        self.assertIn("审批失败", self.api.send_group_text.await_args.args[1])

    async def test_sdk_parser_routes_callbacks_and_preserves_other_plugins(self):
        original = AsyncMock()
        self.client.on_interaction_create = original
        await self.plugin._patch_platforms_once()
        self.assertTrue(self.client.intents & INTERACTION_INTENT)
        captured = []
        state = ConnectionState(lambda name, data: captured.append(data), api=None)
        state.parsers["interaction_create"](
            {
                "id": "outer-event",
                "d": {
                    "id": "interaction-id",
                    "type": 11,
                    "chat_type": 1,
                    "group_openid": "g",
                    "group_member_openid": "astr-admin",
                    "data": {
                        "resolved": {
                            "button_data": f"qqga:{self.token}:approve:assigned",
                            "button_id": "qqga-approve-assigned",
                        }
                    },
                },
            }
        )
        await self.client.on_interaction_create(captured[0])
        self.api.review_join_request.assert_awaited_once()
        original.assert_not_awaited()
        other = self.interaction()
        other.data.resolved.button_data = "another-plugin:data"
        await self.client.on_interaction_create(other)
        original.assert_awaited_once_with(other)
        await self.plugin.terminate()
        self.assertIs(self.client.on_interaction_create, original)

    async def test_replacing_platform_client_reinstalls_callback(self):
        await self.plugin._patch_platforms_once()
        old = self.client
        self.platform.client = NS(_connection=None, intents=0, _active_websockets=set())
        self.platform.meta = lambda: NS(name="qq_official_webhook", id="p")
        await self.plugin._patch_platforms_once()
        self.assertFalse(hasattr(old, "on_interaction_create"))
        await self.platform.client.on_interaction_create(self.interaction())
        self.api.review_join_request.assert_awaited_once()

    async def test_strict_mode_checks_all_five_tools_including_wrapped_context(self):
        self.plugin.config = {"llm_tool_settings": {"strict_llm_permissions": True}}
        self.plugin._mute = AsyncMock()
        self.plugin._review = AsyncMock()
        event = NS(context=NS(event=Event()))
        calls = [
            (self.plugin.mute_tool, ("target",)),
            (self.plugin.unmute_tool, ("target",)),
            (self.plugin.mute_status_tool, ()),
            (self.plugin.list_join_requests_tool, ()),
            (self.plugin.review_join_request_tool, ("applicant", "request", "approve")),
        ]
        for handler, args in calls:
            self.assertIn("唤醒人没有", await handler(event, *args))
        self.plugin._mute.assert_not_awaited()
        self.plugin._review.assert_not_awaited()
        self.api.get_mute_status.assert_not_awaited()
        self.api.list_join_requests.assert_not_awaited()

    async def test_strict_mode_allows_supported_admin_roles_and_defaults_off(self):
        self.plugin._mute = AsyncMock()
        self.plugin.storage.add_group_admin("g", "plugin-admin")
        self.plugin.config = {"strict_llm_permissions": True}
        for event in (
            Event(astr_admin=True),
            Event(sender="plugin-admin"),
            Event(role="owner"),
            Event(role="admin"),
        ):
            self.assertIn("已成功", await self.plugin.mute_tool(event, "target"))
        self.plugin.config = {}
        self.assertIn("已成功", await self.plugin.mute_tool(Event(), "target"))
        schema = json.loads(
            (Path(__file__).parents[1] / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(
            schema["llm_tool_settings"]["items"]["strict_llm_permissions"]["default"]
        )

    async def test_quote_requires_saved_id_and_checks_actual_sender(self):
        self.plugin._review = AsyncMock()
        for reply in (
            Reply(id="", message_str="#1 新的入群申请"),
            Reply(id="forged", message_str="新的入群申请"),
        ):
            event = Event(astr_admin=True, reply=reply)
            await self.plugin.reply_review(event)
            self.assertIn("审批按钮", event.sent[0])
            self.assertTrue(event.stopped)
        event = Event(reply=Reply(id="notice"))
        await self.plugin.reply_review(event)
        self.assertIn("没有本群群管权限", event.sent[0])
        self.plugin._review.assert_not_awaited()
        event = Event(astr_admin=True, reply=Reply(id="notice"))
        await self.plugin.reply_review(event)
        self.assertEqual(event.sent, ["已同意入群申请。"])
        self.plugin._review.assert_awaited_once()

    async def test_raw_qq_quote_fields_work_without_reply_component(self):
        self.plugin._review = AsyncMock()
        for raw in (
            {"message_reference": {"message_id": "notice"}},
            NS(message_reference=NS(message_id="notice")),
            {"message_type": 103, "msg_elements": [{"id": "notice"}]},
            NS(
                raw_data={
                    "message_type": 103,
                    "msg_elements": [{"message_id": "notice"}],
                }
            ),
            NS(message_type=103, msg_elements=[NS(id="notice")]),
        ):
            self.plugin.storage.put_pending("notice", self.pending)
            event = Event(astr_admin=True, text="/拒绝 未完成验证")
            event.message_obj.raw_message = raw
            await self.plugin.reply_review(event)
            self.plugin._review.assert_awaited_with(
                event, "g", "applicant", "request", False, "未完成验证"
            )
            self.assertEqual(event.sent, ["已拒绝入群申请。 理由：未完成验证"])
            self.assertTrue(event.stopped)

    async def test_empty_reply_component_can_use_original_qq_reference_id(self):
        event = Event(astr_admin=True, reply=Reply(id=""))
        event.message_obj.raw_message = NS(
            raw_data={
                "message_type": 103,
                "msg_elements": [{"id": "notice"}],
            }
        )
        await self.plugin.reply_review(event)
        self.assertEqual(event.sent, ["已同意入群申请。"])
        self.api.review_join_request.assert_awaited_once()

    async def test_quote_without_recognizable_id_never_guesses_application(self):
        event = Event(astr_admin=True)
        event.message_obj.raw_message = {
            "message_type": 103,
            "msg_elements": [{"content": "# 📨 入群申请 notice request applicant"}],
        }
        await self.plugin.reply_review(event)
        self.assertIn("没有提供原消息 ID", event.sent[0])
        self.assertTrue(event.stopped)
        self.api.review_join_request.assert_not_awaited()

    async def test_ordinary_messages_and_disabled_scopes_are_not_consumed(self):
        for event, config in (
            (Event(astr_admin=True), {}),
            (Event(reply=Reply(id="notice"), text="这个申请是什么情况？"), {}),
            (Event(reply=Reply(id="notice")), {"enable_join_reply_review": False}),
            (Event(reply=Reply(id="notice")), {"enabled_group_umos": ["other"]}),
        ):
            self.plugin.config = config
            await self.plugin.reply_review(event)
            self.assertFalse(event.stopped)
            self.assertEqual(event.sent, [])
        self.api.review_join_request.assert_not_awaited()

    async def test_reference_index_is_saved_from_notification_response_and_survives_reload(
        self,
    ):
        for fallback in (False, True):
            for message_id in ("sent-message", ""):
                for object_response in (False, True):
                    with self.subTest(
                        fallback=fallback,
                        message_id=message_id,
                        object_response=object_response,
                    ):
                        self.plugin.storage.remove_pending("notice")
                        response = {
                            "id": message_id,
                            "ext_info": {"ref_idx": "REFIDX_a+/b=="},
                        }
                        if object_response:
                            response = NS(
                                id=message_id, ext_info=NS(ref_idx="REFIDX_a+/b==")
                            )
                        self.api.send_group_markdown = AsyncMock(return_value=response)
                        self.api.send_group_text = AsyncMock(return_value=response)
                        if fallback:
                            self.api.send_group_markdown.side_effect = RuntimeError(
                                "markdown unavailable"
                            )
                        await self.plugin._handle_join_request_event("p", self.pending)
                        sent = (
                            self.api.send_group_text
                            if fallback
                            else self.api.send_group_markdown
                        )
                        self.assertNotIn("申请 ID", sent.await_args.args[1])
                        self.assertNotIn("申请 ID", format_request(self.pending))
                        self.plugin.storage = PluginStorage(self.plugin.storage.path)
                        event = Event(astr_admin=True)
                        event.message_obj.raw_message = {
                            "message_scene": {
                                "ext": [
                                    "msg_idx=REFIDX_current",
                                    "ref_msg_idx=REFIDX_a+/b==",
                                ]
                            }
                        }
                        await self.plugin.reply_review(event)
                        self.assertEqual(event.sent, ["已同意入群申请。"])
                        self.assertTrue(event.stopped)
                        self.assertEqual(self.plugin.storage.data["pending"], {})

    async def test_quote_reference_index_forms_work_without_reply_component(self):
        for raw in (
            {"message_scene": {"ext": ["ref_msg_idx=REFIDX_notice=="]}},
            NS(message_scene=NS(ext=["ref_msg_idx=REFIDX_notice=="])),
            NS(raw_data={"message_scene": {"ext": ["ref_msg_idx=REFIDX_notice=="]}}),
            {"message_type": 103, "msg_elements": [{"msg_idx": "REFIDX_notice=="}]},
            NS(
                raw_data={
                    "message_type": 103,
                    "msg_elements": [NS(msg_idx="REFIDX_notice==")],
                }
            ),
            {"message_reference": {"message_id": "REFIDX_notice=="}},
        ):
            self.plugin.storage.put_pending(
                "notice", {**self.pending, "ref_idx": "REFIDX_notice=="}
            )
            event = Event(astr_admin=True, text="拒绝 理由")
            event.message_obj.raw_message = raw
            await self.plugin.reply_review(event)
            self.assertEqual(event.sent, ["已拒绝入群申请。 理由：理由"])
            self.assertTrue(event.stopped)
        self.assertEqual(self.api.review_join_request.await_count, 6)

    async def test_current_message_index_and_quoted_body_never_locate_application(self):
        self.plugin.storage.bind_pending_message("notice", "notice", "REFIDX_notice==")
        cases = (
            (None, {"message_scene": {"ext": ["msg_idx=REFIDX_notice=="]}}),
            (Reply(id="", message_str="申请 ID：request"), {}),
            (Reply(id=""), {"message_scene": {"ext": ["msg_idx=REFIDX_notice=="]}}),
            (
                None,
                {
                    "message_type": 103,
                    "msg_elements": [{"content": "申请 ID：request"}],
                },
            ),
            (
                None,
                {
                    "message_type": 103,
                    "msg_elements": [
                        {"msg_elements": [{"msg_idx": "REFIDX_notice=="}]}
                    ],
                },
            ),
            (None, {"message_scene": {"ext": ["ref_msg_idx=TMP_unmatched"]}}),
        )
        for index, (reply, raw) in enumerate(cases):
            event = Event(astr_admin=True, reply=reply)
            event.message_obj.raw_message = raw
            await self.plugin.reply_review(event)
            self.assertEqual(event.stopped, index != 0)
        self.api.review_join_request.assert_not_awaited()

    async def test_ref_lookup_is_scoped_and_rechecks_permission_and_retention(self):
        self.plugin.storage.remove_pending("notice")
        item = {**self.pending, "ref_idx": "REFIDX_notice=="}
        self.plugin.storage.put_pending(
            "foreign-platform", {**item, "platform_id": "other"}
        )
        self.plugin.storage.put_pending(
            "foreign-group", {**item, "group_openid": "other"}
        )
        for case in ("foreign_only", "denied", "expired", "success"):
            if case != "foreign_only":
                self.plugin.storage.put_pending("notice", item)
            if case == "expired":
                self.plugin.storage.data["pending"]["notice"]["stored_at"] = (
                    datetime.now(timezone.utc) - timedelta(days=31)
                ).isoformat()
            event = Event(
                astr_admin=case != "denied", reply=Reply(id="REFIDX_notice==")
            )
            await self.plugin.reply_review(event)
            self.assertTrue(event.stopped)
            if case != "success":
                self.api.review_join_request.assert_not_awaited()
        self.api.review_join_request.assert_awaited_once()
        self.assertIsNotNone(self.plugin.storage.get_pending("foreign-platform"))
        self.assertIsNotNone(self.plugin.storage.get_pending("foreign-group"))

    async def test_conflicting_reference_indices_do_not_select_either_request(self):
        self.plugin.storage.bind_pending_message("notice", "notice", "REFIDX_first")
        self.plugin.storage.put_pending(
            "second",
            {
                **self.pending,
                "ref_idx": "REFIDX_second",
                "join_request_id": "request-2",
            },
        )
        for reply in (None, Reply(id="notice")):
            event = Event(astr_admin=True, reply=reply)
            event.message_obj.raw_message = {
                "message_type": 103,
                "message_scene": {"ext": ["ref_msg_idx=REFIDX_first"]},
                "msg_elements": [{"msg_idx": "REFIDX_second"}],
            }
            await self.plugin.reply_review(event)
            self.assertIn("未唯一匹配", event.sent[0])
            self.assertTrue(event.stopped)
        self.api.review_join_request.assert_not_awaited()

    async def test_reply_send_failure_still_stops_event_and_does_not_restore_request(
        self,
    ):
        event = Event(astr_admin=True, reply=Reply(id="notice"))
        event.send = AsyncMock(side_effect=RuntimeError("send failed"))
        with self.assertRaisesRegex(RuntimeError, "send failed"):
            await self.plugin.reply_review(event)
        self.assertTrue(event.stopped)
        self.assertIsNone(self.plugin.storage.get_pending("notice"))
        self.api.review_join_request.assert_awaited_once()

    async def test_real_astrbot_pipeline_never_calls_llm_for_quote_review(self):
        from astrbot.core.pipeline.process_stage.stage import ProcessStage
        from astrbot.core.pipeline.process_stage.method.star_request import (
            StarRequestSubStage,
        )
        from astrbot.core.platform.astr_message_event import AstrMessageEvent
        from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
        from astrbot.core.platform.message_type import MessageType
        from astrbot.core.platform.platform_metadata import PlatformMetadata

        stage = ProcessStage()
        stage.ctx = NS(astrbot_config={"provider_settings": {"enable": True}})
        stage.star_request_sub_stage = StarRequestSubStage()
        stage.agent_sub_stage = NS(
            process=Mock(side_effect=AssertionError("LLM must not run"))
        )
        handler = NS(
            handler_full_name="reply_test.reply_review",
            handler_module_path="reply_test",
            handler_name="reply_review",
            handler=self.plugin.reply_review,
        )
        for platform in ("qq_official", "qq_official_webhook"):
            for case in (
                "success",
                "ref_idx",
                "missing_id",
                "unknown_id",
                "expired",
                "denied",
                "cross_group",
                "api_error",
            ):
                with self.subTest(platform=platform, case=case):
                    self.plugin.storage.put_pending("notice", self.pending)
                    self.api.review_join_request.reset_mock(side_effect=True)
                    reply_id = {
                        "missing_id": "",
                        "ref_idx": "",
                        "unknown_id": "unknown",
                    }.get(case, "notice")
                    message = AstrBotMessage()
                    message.type = MessageType.GROUP_MESSAGE
                    message.self_id, message.group_id, message.message_id = (
                        "bot",
                        "g",
                        "incoming",
                    )
                    message.sender = MessageMember("sender", "tester")
                    message.message = [Reply(id=reply_id), Plain(text="同意")]
                    message.raw_message = {"author": {"member_role": "member"}}
                    if case == "ref_idx":
                        message.raw_message["message_scene"] = {
                            "ext": ["ref_msg_idx=REFIDX_notice=="]
                        }
                        self.plugin.storage.bind_pending_message(
                            "notice", "notice", "REFIDX_notice=="
                        )
                    event = AstrMessageEvent(
                        "同意", message, PlatformMetadata(platform, "test", "p"), "g"
                    )
                    event.role = "member" if case == "denied" else "admin"
                    event.is_at_or_wake_command = True
                    event.set_extra("activated_handlers", [handler])
                    if case == "expired":
                        self.plugin.storage.data["pending"]["notice"]["stored_at"] = (
                            datetime.now(timezone.utc) - timedelta(days=31)
                        ).isoformat()
                    elif case == "cross_group":
                        self.plugin.storage.data["pending"]["notice"][
                            "group_openid"
                        ] = "other"
                    elif case == "api_error":
                        self.api.review_join_request.side_effect = RuntimeError(
                            "API failure"
                        )

                    async def send(result):
                        self.assertFalse(
                            event.is_stopped(), "Reply must be sent before STOP"
                        )
                        self.assertTrue(result.chain)

                    event.send = AsyncMock(side_effect=send)
                    with patch.dict(
                        "astrbot.core.pipeline.process_stage.method.star_request.star_map",
                        {
                            "reply_test": NS(name="reply-test"),
                        },
                    ):
                        async for _ in stage.process(event):
                            pass
                    event.send.assert_awaited_once()
                    self.assertTrue(event.is_stopped())
                    if case in {"success", "ref_idx", "api_error"}:
                        self.api.review_join_request.assert_awaited_once()
                    else:
                        self.api.review_join_request.assert_not_awaited()
        stage.agent_sub_stage.process.assert_not_called()

    async def test_ack_api_uses_new_domain_and_encoded_interaction_id(self):
        api = QQGroupManageAPI(NS(api=NS(_http=NS(request=AsyncMock()))))
        await api.acknowledge_interaction("id/unsafe", 5)
        call = api.client.api._http.request.await_args
        self.assertIn("api.bot.qq.com/interactions/id%2Funsafe", call.args[0].url)
        self.assertEqual(call.kwargs["json"], {"code": 5})

    async def test_member_info_api_encodes_group_and_member_ids(self):
        api = QQGroupManageAPI(NS(api=NS(_http=NS(request=AsyncMock()))))
        await api.get_group_member_info("group/unsafe", "member/unsafe")
        route = api.client.api._http.request.await_args.args[0]
        self.assertEqual(route.method, "GET")
        self.assertIn(
            "api.bot.qq.com/v2/groups/group%2Funsafe/members/member%2Funsafe", route.url
        )

    async def new_buttons(self):
        self.api.send_group_markdown = AsyncMock(return_value={"id": "notice"})
        await self.plugin._handle_join_request_event(
            "p", {k: v for k, v in self.pending.items() if k != "callback_token"}
        )
        keyboard = self.api.send_group_markdown.await_args.kwargs["keyboard"]
        return [
            button for row in keyboard["content"]["rows"] for button in row["buttons"]
        ]

    def button_interaction(self, button, sender="astr-admin"):
        callback = self.interaction(sender=sender)
        callback.data.resolved.button_id = button["id"]
        callback.data.resolved.button_data = button["action"]["data"]
        return callback

    async def test_new_buttons_use_qq_permissions_and_independent_persistent_bindings(
        self,
    ):
        self.plugin.storage.add_group_admin("g", "plugin-admin")
        self.plugin.storage.add_group_admin("other-group", "other-admin")
        buttons = await self.new_buttons()
        self.assertEqual(len(buttons), 4)
        self.assertEqual(len({b["action"]["data"].split(":")[1] for b in buttons}), 4)
        self.plugin.storage = PluginStorage(self.plugin.storage.path)
        for button in buttons:
            _, token, action, audience = button["action"]["data"].split(":")
            _, pending = self.plugin.storage.find_pending_by_token(token)
            self.assertEqual(
                pending["review_callbacks"][token],
                {
                    "action": action,
                    "audience": audience,
                },
            )
            self.assertEqual(button["action"]["type"], 1)
            self.assertEqual(
                button["action"]["permission"],
                {"type": 1}
                if audience == "native"
                else {
                    "type": 0,
                    "specify_user_ids": ["astr-admin", "plugin-admin"],
                },
            )

    async def test_no_assigned_admins_omits_second_row(self):
        self.plugin.context.get_config = lambda umo: {"admins_id": []}
        buttons = await self.new_buttons()
        self.assertEqual(len(buttons), 2)
        self.assertTrue(all(b["action"]["permission"] == {"type": 1} for b in buttons))
        self.assertEqual(
            len(self.plugin.storage.get_pending("notice")["review_callbacks"]), 2
        )

    async def test_native_buttons_do_not_require_member_lookup(self):
        # This simulates a callback delivered by QQ after its type=1 permission
        # check; QQ's rejection of ordinary members requires a live group test.
        self.api.get_group_member_info.side_effect = RuntimeError("no API permission")
        for index, sender in ((0, "qq-owner"), (1, "qq-admin")):
            buttons = await self.new_buttons()
            self.plugin.storage = PluginStorage(self.plugin.storage.path)
            self.plugin._callback_guard.remember_denied("p", "g", sender)
            await self.plugin._handle_review_interaction(
                "p", self.button_interaction(buttons[index], sender)
            )
            self.api.review_join_request.assert_awaited_with(
                "g", "applicant", "request", approve=index == 0
            )
            for button in buttons:
                self.assertIsNone(
                    self.plugin.storage.find_pending_by_token(
                        button["action"]["data"].split(":")[1]
                    )
                )
        self.api.get_group_member_info.assert_not_awaited()

    async def test_assigned_buttons_recheck_current_admin_lists(self):
        self.plugin.storage.add_group_admin("g", "plugin-admin")
        for sender in ("astr-admin", "plugin-admin"):
            buttons = await self.new_buttons()
            await self.plugin._handle_review_interaction(
                "p", self.button_interaction(buttons[2], sender)
            )
        self.assertEqual(self.api.review_join_request.await_count, 2)
        buttons = await self.new_buttons()
        self.plugin.storage.remove_group_admin("g", "plugin-admin")
        self.plugin.context.get_config = lambda umo: {"admins_id": []}
        for sender in ("astr-admin", "plugin-admin", "member", "other-group-admin"):
            await self.plugin._handle_review_interaction(
                "p", self.button_interaction(buttons[2], sender)
            )
            self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 5)
        self.assertEqual(self.api.review_join_request.await_count, 2)
        self.api.get_group_member_info.assert_not_awaited()

    async def test_new_button_operation_and_audience_cannot_be_changed(self):
        buttons = await self.new_buttons()
        for index, action, audience in (
            (2, "approve", "native"),
            (0, "decline", "native"),
            (0, "approve", "shared"),
        ):
            callback = self.button_interaction(buttons[index])
            token = callback.data.resolved.button_data.split(":")[1]
            callback.data.resolved.button_data = f"qqga:{token}:{action}:{audience}"
            callback.data.resolved.button_id = f"qqga-{action}-{audience}"
            await self.plugin._handle_review_interaction("p", callback)
            self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 4)
        self.api.review_join_request.assert_not_awaited()
        self.api.get_group_member_info.assert_not_awaited()

    async def test_native_buttons_still_enforce_scope_expiry_and_feature_switch(self):
        buttons = await self.new_buttons()
        for platform, extra in (
            ("other", {}),
            ("p", {"group_openid": "other"}),
            ("p", {"chat_type": 2}),
        ):
            callback = self.button_interaction(buttons[0], "qq-admin")
            for name, value in extra.items():
                setattr(callback, name, value)
            await self.plugin._handle_review_interaction(platform, callback)
            self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 4)
        self.plugin.config = {"enable_join_reply_review": False}
        await self.plugin._handle_review_interaction(
            "p", self.button_interaction(buttons[0])
        )
        self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 4)
        self.plugin.config = {}
        self.plugin.storage.data["pending"]["notice"]["stored_at"] = (
            datetime.now(timezone.utc) - timedelta(days=31)
        ).isoformat()
        await self.plugin._handle_review_interaction(
            "p", self.button_interaction(buttons[0])
        )
        self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 3)
        self.api.review_join_request.assert_not_awaited()

    async def test_both_rows_reply_and_tool_share_one_request_lock(self):
        buttons = await self.new_buttons()
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_review(*args, **kwargs):
            entered.set()
            await release.wait()

        self.api.review_join_request.side_effect = slow_review
        first = asyncio.create_task(
            self.plugin._handle_review_interaction(
                "p", self.button_interaction(buttons[0], "qq-admin")
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await self.plugin._handle_review_interaction(
                "p", self.button_interaction(buttons[3])
            )
            self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 2)
            event = Event(astr_admin=True, reply=Reply(id="notice"))
            await self.plugin.reply_review(event)
            self.assertIn("正在处理中", event.sent[0])
            result = await self.plugin.review_join_request_tool(
                Event(astr_admin=True), "applicant", "request", "decline"
            )
            self.assertIn("正在处理中", result)
        finally:
            release.set()
            await first
        self.api.review_join_request.assert_awaited_once()
        self.assertFalse(self.plugin._reviews_inflight)

    async def test_reply_first_blocks_button_and_success_invalidates_all_notifications(
        self,
    ):
        buttons = await self.new_buttons()
        pending = self.plugin.storage.get_pending("notice")
        self.plugin.storage.put_pending("duplicate", pending)
        self.plugin.storage.put_pending(
            "other-platform", {**pending, "platform_id": "other"}
        )
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_review(*args, **kwargs):
            entered.set()
            await release.wait()

        async def reply():
            await self.plugin.reply_review(
                Event(astr_admin=True, reply=Reply(id="notice"))
            )

        self.api.review_join_request.side_effect = slow_review
        first = asyncio.create_task(reply())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await self.plugin._handle_review_interaction(
                "p", self.button_interaction(buttons[0])
            )
            self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 2)
        finally:
            release.set()
            await first
        self.api.review_join_request.assert_awaited_once()
        self.assertIsNone(self.plugin.storage.get_pending("notice"))
        self.assertIsNone(self.plugin.storage.get_pending("duplicate"))
        self.assertIsNotNone(self.plugin.storage.get_pending("other-platform"))

    async def test_new_callback_failure_or_cancellation_releases_request_lock(self):
        buttons = await self.new_buttons()
        for error in (RuntimeError("approval failed"), asyncio.CancelledError()):
            self.now += 2
            self.api.review_join_request.side_effect = error
            try:
                await self.plugin._handle_review_interaction(
                    "p", self.button_interaction(buttons[0])
                )
            except asyncio.CancelledError:
                pass
            self.assertFalse(self.plugin._reviews_inflight)
            self.assertIsNotNone(self.plugin.storage.get_pending("notice"))

    def test_buttons_are_callbacks_and_notification_markdown_is_escaped(self):
        callbacks = {
            f"{i:032x}": {"action": action, "audience": audience}
            for i, (audience, action) in enumerate(
                (
                    (audience, action)
                    for audience in ("native", "assigned")
                    for action in ("approve", "decline")
                )
            )
        }
        rows = review_keyboard(callbacks, ["astr-admin"])["content"]["rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [b["render_data"]["label"] for b in rows[0]["buttons"]],
            ["群管同意", "群管拒绝"],
        )
        for row, permission in zip(
            rows, ({"type": 1}, {"type": 0, "specify_user_ids": ["astr-admin"]})
        ):
            for button in row["buttons"]:
                self.assertEqual(button["action"]["type"], 1)
                self.assertEqual(button["action"]["permission"], permission)
                self.assertNotIn("enter", button["action"])
        text = format_request(
            {
                "username": "[假按钮](mqqapi://x)",
                "verify_info": {"verify_message": "<qqbot-at-everyone />"},
            },
            markdown=True,
        )
        self.assertTrue(text.startswith("# 📨 入群申请"))
        self.assertNotIn("[假按钮](", text)
        self.assertNotIn("<qqbot-at-everyone", text)


if __name__ == "__main__":
    unittest.main()
