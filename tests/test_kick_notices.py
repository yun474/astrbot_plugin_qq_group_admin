"""移出昵称、通知、退群关联和回退禁言的回归测试。"""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from astrbot_plugin_qq_group_admin.kick_notices import event_member_name, notice_text
from astrbot_plugin_qq_group_admin.member_removal import RemovalError
from astrbot_plugin_qq_group_admin.storage import PluginStorage
import test_kick_menu


def denied():
    error = RuntimeError("移出权限未开启")
    error.qq_code = 40103012
    return error


class NoticeTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_kick_menu.KickMenuTests.setUp
    event = test_kick_menu.KickMenuTests.event

    async def test_name_is_saved_before_removal_and_does_not_use_group_card(self):
        self.api.get_group_member_info = AsyncMock(
            return_value={"username": "用户昵称", "member_nickname": "群名片"}
        )
        self.plugin.context.get_platform_inst.return_value.appid = "app-id"

        async def remove(group, members):
            saved = self.plugin.storage.removal_record("p", "g", "target", "name-test")
            self.assertEqual(saved["name"], "用户昵称")
            self.assertEqual(saved["phase"], "submitted")
            return {"remove_members_result": "success"}

        self.api.remove_members.side_effect = remove
        result = await self.plugin.remover.remove("p", "g", "target", "name-test")
        self.assertEqual(result["name"], "用户昵称")
        self.assertIn("/app-id/target/640", result["avatar"])

    def test_event_names_use_username_and_support_dictionary_mentions(self):
        event = self.event()
        event.message_obj.raw_message = {
            "author": {"username": "用户名字", "member_nickname": "本群名片"},
            "mentions": [
                {
                    "member_openid": "target",
                    "username": "目标名字",
                    "member_nickname": "目标名片",
                }
            ],
        }
        self.assertEqual(event_member_name(event, "member"), "用户名字")
        self.assertEqual(event_member_name(event, "target"), "目标名字")

    def test_single_notice_has_avatar_and_operator_but_batch_is_compact(self):
        first = {
            "name": "小明",
            "avatar": "https://q.qlogo.cn/qqapp/app/id/640",
            "status": "success",
        }
        single = notice_text([first], "群管指令", operator="owner")
        self.assertLess(single.index("小明"), single.index("![头像"))
        self.assertIn('<qqbot-at-user id="owner" />', single)
        batch = notice_text(
            [first, {"name": "小红", "status": "success"}], "群管指令", operator="owner"
        )
        self.assertIn("小明、小红", batch)
        self.assertIn("成功 2 人", batch)
        self.assertNotIn("![头像", batch)
        self.assertNotIn("---", batch)

    def test_member_names_are_escaped_and_function_notice_omits_operator(self):
        text = notice_text(
            [{"name": '<qqbot-at-user id="bad" />_[x]', "status": "unknown"}],
            "关键词踢人",
        )
        self.assertNotIn('<qqbot-at-user id="bad"', text)
        self.assertNotIn("处理人", text)
        self.assertIn("结果待确认", text)

    async def test_keyword_failure_falls_back_to_configured_mute_once(self):
        self.settings.update(enable_keyword_kick=True, kick_keywords=["广告"])
        self.api.remove_members.side_effect = denied()
        event = self.event(text="广告")
        await self.plugin.keyword_mute(event)
        await self.plugin.keyword_mute(event)
        self.api.remove_members.assert_awaited_once()
        self.api.mute_member.assert_awaited_once()
        saved = self.plugin.storage.removal_record(
            "p", "g", "member", "keyword:message-1"
        )
        self.assertEqual(saved["fallback_result"]["status"], "muted")
        self.assertEqual(saved["fallback_result"]["duration"], "2分30秒")
        self.api.send_group_markdown.assert_not_awaited()

    async def test_keyword_notice_can_be_enabled(self):
        self.settings.update(
            enable_keyword_kick=True,
            enable_keyword_kick_notice=True,
            kick_keywords=["广告"],
        )
        await self.plugin.keyword_mute(self.event(text="广告"))
        content = self.api.send_group_markdown.await_args.args[1]
        self.assertIn("关键词踢人", content)
        self.assertNotIn("处理人", content)

    async def test_unknown_result_does_not_trigger_fallback_mute(self):
        self.api.remove_members.side_effect = TimeoutError()
        with self.assertRaises(RemovalError) as caught:
            await self.plugin.remover.remove(
                "p", "g", "target", "unknown", fallback_key="keyword_mute_duration"
            )
        self.assertFalse(caught.exception.confirmed)
        self.api.mute_member.assert_not_awaited()

    async def test_manual_failure_keeps_manual_behavior(self):
        self.api.remove_members.side_effect = denied()
        with self.assertRaises(RemovalError):
            await self.plugin.remover.remove("p", "g", "target", "manual")
        self.api.mute_member.assert_not_awaited()

    async def test_fallback_mute_failure_is_reported(self):
        self.api.remove_members.side_effect = denied()
        self.api.mute_member.side_effect = RuntimeError("禁言失败")
        result = await self.plugin.remover.remove(
            "p", "g", "target", "double-failure", fallback_key="keyword_mute_duration"
        )
        self.assertEqual(result["status"], "mute_failed")
        self.assertTrue(result["mute_attempted"])
        self.assertEqual(result["mute_error"], "禁言失败")

    async def test_late_and_duplicate_departure_notices_are_merged(self):
        await self.plugin.remover.remove("p", "g", "target", "depart")
        self.plugin.storage = PluginStorage(self.plugin.storage.path)
        for _ in range(2):
            await self.plugin._handle_member_event(
                "p", "member_leave", {"group_openid": "g", "member_openid": "target"}
            )
        self.api.send_group_text.assert_not_awaited()
        self.api.send_group_markdown.assert_not_awaited()

    async def test_new_join_does_not_consume_next_ordinary_departure(self):
        await self.plugin.remover.remove("p", "g", "target", "old")
        self.plugin.storage.reset_removal_departure("p", "g", "target")
        await self.plugin._handle_member_event(
            "p", "member_leave", {"group_openid": "g", "member_openid": "target"}
        )
        self.api.send_group_text.assert_awaited_once()

    async def test_departure_while_request_is_running_is_merged(self):
        async def remove(*args):
            await self.plugin._handle_member_event(
                "p", "member_leave", {"group_openid": "g", "member_openid": "target"}
            )
            return {"remove_members_result": "success"}

        self.api.remove_members.side_effect = remove
        await self.plugin.remover.remove("p", "g", "target", "during")
        self.api.send_group_text.assert_not_awaited()

    async def test_leaving_and_rejoining_during_name_query_stops_old_operation(self):
        async def get_info(*args):
            self.assertFalse(
                self.plugin.storage.consume_removal_departure("p", "g", "target")
            )
            self.plugin.storage.reset_removal_departure("p", "g", "target")
            return {"username": "新一轮成员"}

        self.api.get_group_member_info = AsyncMock(side_effect=get_info)
        with self.assertRaisesRegex(RemovalError, "成员已离群"):
            await self.plugin.remover.remove(
                "p", "g", "target", "old-epoch", fallback_key="keyword_mute_duration"
            )
        self.api.remove_members.assert_not_awaited()
        self.api.mute_member.assert_not_awaited()

    async def test_scope_or_rule_change_during_name_query_stops_request(self):
        allowed = True

        async def get_info(*args):
            nonlocal allowed
            allowed = False
            return {"username": "小明"}

        self.api.get_group_member_info = AsyncMock(side_effect=get_info)
        with self.assertRaises(RemovalError):
            await self.plugin.remover.remove(
                "p",
                "g",
                "target",
                "revoked",
                can_execute=lambda: allowed,
                fallback_key="keyword_mute_duration",
            )
        self.api.remove_members.assert_not_awaited()
        self.api.mute_member.assert_not_awaited()

    async def test_rejoining_during_final_scope_check_stops_old_operation(self):
        checks = 0

        async def enabled(*args, **kwargs):
            nonlocal checks
            checks += 1
            if checks == 2:
                self.plugin.storage.consume_removal_departure("p", "g", "target")
                self.plugin.storage.reset_removal_departure("p", "g", "target")
            return True

        self.plugin._sdk_group_enabled = enabled
        with self.assertRaisesRegex(RemovalError, "成员已离群"):
            await self.plugin.remover.remove(
                "p", "g", "target", "final-check", fallback_key="keyword_mute_duration"
            )
        self.api.remove_members.assert_not_awaited()
        self.api.mute_member.assert_not_awaited()

    async def test_fallback_intent_save_failure_does_not_send_mute(self):
        self.api.remove_members.side_effect = denied()
        save = self.plugin.storage.save

        def fail_intent_save():
            record = self.plugin.storage.removal_record("p", "g", "target", "disk")
            if record.get("fallback_result", {}).get("status") == "mute_unknown":
                raise OSError("磁盘写入失败")
            save()

        with patch.object(self.plugin.storage, "save", side_effect=fail_intent_save):
            result = await self.plugin.remover.remove(
                "p", "g", "target", "disk", fallback_key="keyword_mute_duration"
            )
        self.assertEqual(result["status"], "mute_failed")
        self.assertFalse(result.get("mute_attempted", False))
        self.api.mute_member.assert_not_awaited()

    async def test_markdown_send_failure_uses_plain_text(self):
        self.api.send_group_markdown.side_effect = RuntimeError("发送失败")
        sent = await self.plugin._send_kick_notice(
            "p",
            "g",
            [{"name": "小明", "status": "success"}],
            "LLM 踢人工具",
            event=self.event(),
        )
        self.assertTrue(sent)
        self.assertIn("LLM 踢人工具", self.api.send_group_text.await_args.args[1])
        self.assertNotIn("**", self.api.send_group_text.await_args.args[1])


class VerifyFallbackTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_kick_menu.KickVerifyTests.setUp
    record = test_kick_menu.KickVerifyTests.record
    join = test_kick_menu.KickVerifyTests.join
    message = test_kick_menu.KickVerifyTests.message

    async def fail(self):
        self.settings["verify_fail_action"] = "移出群聊"
        self.api.remove_members.side_effect = denied()
        await self.join()
        async with self.verifier._lock(("p", "g", "new")):
            await self.verifier._fail(
                self.plugin.context.get_platform_inst("p"), self.record()
            )

    async def test_fallback_mute_is_released_by_verification_release(self):
        await self.fail()
        self.assertEqual(self.record()["fail_action"], "禁言")
        self.api.mute_member.assert_awaited_once()
        await self.verifier.release(self.message("验证放行", sender="admin"), ["new"])
        self.assertEqual(self.api.mute_member.await_args.kwargs["op"], "del")

    async def test_restart_reads_persisted_fallback_when_record_write_was_lost(self):
        await self.fail()
        self.record()["fail_action"] = "移出群聊"
        self.plugin.storage.put_verification(self.record())
        self.plugin.storage = PluginStorage(self.plugin.storage.path)
        await self.verifier.restart(self.message("重新验证", sender="admin"), ["new"])
        self.assertEqual(self.api.mute_member.await_args.kwargs["op"], "del")

    async def test_cancelled_fallback_request_can_be_released_after_reload(self):
        self.settings["verify_fail_action"] = "移出群聊"
        self.api.remove_members.side_effect = denied()
        entered = asyncio.Event()

        async def mute(*args, **kwargs):
            entered.set()
            await asyncio.Future()

        self.api.mute_member.side_effect = mute
        await self.join()

        async def fail():
            async with self.verifier._lock(("p", "g", "new")):
                await self.verifier._fail(
                    self.plugin.context.get_platform_inst("p"), self.record()
                )

        request = asyncio.create_task(fail())
        await asyncio.wait_for(entered.wait(), 1)
        await self.plugin.remover.close()
        await asyncio.gather(request, return_exceptions=True)
        self.plugin.storage = PluginStorage(self.plugin.storage.path)
        self.api.mute_member.side_effect = None
        await self.verifier.release(self.message("验证放行", sender="admin"), ["new"])
        self.assertEqual(self.api.mute_member.await_args.kwargs["op"], "del")


class ApprovalReloadTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_kick_menu.KickReloadTests.setUp

    async def test_approval_request_is_finished_before_unload_returns(self):
        entered, releasing = asyncio.Event(), asyncio.Event()

        async def review(*args, **kwargs):
            entered.set()
            await releasing.wait()
            return {}

        self.api.review_join_request.side_effect = review
        self.plugin.storage.add_group_admin("g", "revoked")
        event = test_kick_menu.KickMenuTests.event(self)
        request = asyncio.create_task(
            self.plugin._review(event, "g", "target", "request", True, "")
        )
        await asyncio.wait_for(entered.wait(), 1)
        await self.plugin.terminate()
        await asyncio.gather(request, return_exceptions=True)
        self.assertTrue(request.cancelled())
        new_storage = PluginStorage(self.plugin.storage.path)
        new_storage.remove_group_admin("g", "revoked")
        releasing.set()
        await asyncio.sleep(0)
        self.assertEqual(PluginStorage(new_storage.path).group_admins("g"), [])
