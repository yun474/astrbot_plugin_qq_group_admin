import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from astrbot.api.message_components import Plain, Reply
from astrbot_plugin_qq_group_admin.api import QQGroupManageAPI
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
    def __init__(self, sender="member", role="member", astr_admin=False, reply=None):
        self.sender, self.astr_admin, self.reply = sender, astr_admin, reply
        self.message_obj = NS(raw_message={"author": {"member_role": role}})
        self.unified_msg_origin = "p:GroupMessage:g"

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
        return ([self.reply] if self.reply else []) + [Plain(text="同意")]

    def stop_event(self):
        pass

    def plain_result(self, text):
        return text


class InteractionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plugin = object.__new__(QQGroupAdminPlugin)
        self.plugin.config = {}
        self.plugin.storage = PluginStorage(Path(self.tmp.name) / "state.json")
        self.plugin._review_callbacks_inflight = set()
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
        await self.plugin._handle_review_interaction("p", callback)
        self.assertEqual(self.api.review_join_request.await_count, 1)
        self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 3)

    async def test_shared_callback_queries_native_admin_and_owner_roles(self):
        for role in ("admin", "owner"):
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
            self.api.get_group_member_info.return_value = result
            await self.plugin._handle_review_interaction(
                "p", self.interaction(sender="member")
            )
            self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 4)
        for error in (RuntimeError("11253: no API permission"), TimeoutError()):
            self.api.get_group_member_info.side_effect = error
            await self.plugin._handle_review_interaction(
                "p", self.interaction(sender="member")
            )
            self.api.acknowledge_interaction.assert_awaited_with("interaction-id", 4)
        self.api.review_join_request.assert_not_awaited()
        self.assertIsNotNone(self.plugin.storage.get_pending("notice"))
        self.assertFalse(self.plugin._review_callbacks_inflight)

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
        self.assertFalse(self.plugin._review_callbacks_inflight)

    async def test_approval_failure_keeps_mapping_for_explicit_retry(self):
        self.api.review_join_request.side_effect = RuntimeError("QQ API denied")
        await self.plugin._handle_review_interaction("p", self.interaction())
        self.assertIsNotNone(self.plugin.storage.get_pending("notice"))
        self.assertFalse(self.plugin._review_callbacks_inflight)
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
            result = [
                x
                async for x in self.plugin.reply_review(
                    Event(astr_admin=True, reply=reply)
                )
            ]
            self.assertEqual(result, [])
        denied = [
            x async for x in self.plugin.reply_review(Event(reply=Reply(id="notice")))
        ]
        self.assertIn("没有本群群管权限", denied[0])
        self.plugin._review.assert_not_awaited()
        awaitable = self.plugin.reply_review(
            Event(astr_admin=True, reply=Reply(id="notice"))
        )
        self.assertEqual([x async for x in awaitable], ["已同意入群申请。"])
        self.plugin._review.assert_awaited_once()

    async def test_ack_api_uses_new_domain_and_encoded_interaction_id(self):
        api = QQGroupManageAPI(NS(api=NS(_http=NS(request=AsyncMock()))))
        await api.acknowledge_interaction("id/unsafe", 4)
        call = api.client.api._http.request.await_args
        self.assertIn("api.bot.qq.com/interactions/id%2Funsafe", call.args[0].url)
        self.assertEqual(call.kwargs["json"], {"code": 4})

    async def test_member_info_api_encodes_group_and_member_ids(self):
        api = QQGroupManageAPI(NS(api=NS(_http=NS(request=AsyncMock()))))
        await api.get_group_member_info("group/unsafe", "member/unsafe")
        route = api.client.api._http.request.await_args.args[0]
        self.assertEqual(route.method, "GET")
        self.assertIn(
            "api.bot.qq.com/v2/groups/group%2Funsafe/members/member%2Funsafe", route.url
        )

    def test_buttons_are_callbacks_and_notification_markdown_is_escaped(self):
        rows = review_keyboard(self.token)["content"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            [b["render_data"]["label"] for b in rows[0]["buttons"]], ["同意", "拒绝"]
        )
        for row in rows:
            for button in row["buttons"]:
                self.assertEqual(button["action"]["type"], 1)
                self.assertEqual(button["action"]["permission"], {"type": 2})
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
