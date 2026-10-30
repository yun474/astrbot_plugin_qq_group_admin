import asyncio
import json
import random
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from astrbot_plugin_qq_group_admin.join_verify import (
    make_arithmetic,
    parse_custom_questions,
)
from astrbot_plugin_qq_group_admin.storage import PluginStorage
from astrbot_plugin_qq_group_admin.verify_flow import JoinVerifier
import test_hot_reload
import test_keywords

JOIN = {"group_openid": "g", "member_openid": "new"}


class ArithmeticTests(unittest.TestCase):
    def test_questions_are_correct_and_options_are_distinct(self):
        rng = random.Random(7)
        symbols = {"+": "加", "-": "减", "×": "乘", "÷": "除"}
        for _ in range(2000):
            question, options, correct = make_arithmetic(["加", "减", "乘", "除"], 20, rng)
            a, symbol, b = question.removesuffix(" = ?").split()
            a, b = int(a), int(b)
            op = symbols[symbol]
            expected = {"加": a + b, "减": a - b, "乘": a * b}.get(op)
            if op == "除":
                self.assertEqual(a % b, 0)
                expected = a // b
            self.assertGreaterEqual(expected, 0)
            self.assertEqual(options[correct], str(expected))
            self.assertEqual(len(set(options)), 4)
            self.assertTrue(all(int(value) >= 0 for value in options))

    def test_unknown_operators_fall_back_to_all(self):
        question, _, _ = make_arithmetic(["乘"], 5, random.Random(1))
        self.assertIn("×", question)
        question, _, _ = make_arithmetic(["幂"], 5, random.Random(1))
        self.assertRegex(question, r"[+\-×÷]")

    def test_custom_question_parsing_skips_invalid_lines(self):
        questions, invalid = parse_custom_questions(
            ["本群主题？ => ^(天文|astronomy)$", "缺少分隔符", "坏正则 => (", " ", "空答案 =>"]
        )
        self.assertEqual(questions, [("本群主题？", "^(天文|astronomy)$")])
        self.assertEqual(invalid, ["缺少分隔符", "坏正则 => (", "空答案 =>"])


class JoinVerifyFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        test_keywords.KeywordTests.setUp(self)
        self.settings = {
            "enable_join_verify": True,
            "verify_mode": "算术题",
            "verify_max_failures": 2,
            "verify_fail_mute_duration": "1天",
            "verify_timeout_minutes": 10,
            "verify_timeout_action": "按失败禁言",
            "custom_questions": ["答案是多少？ => ^42$"],
        }
        self.plugin.config["verify_settings"] = self.settings
        self.api.recall_group_message = AsyncMock()
        self.api.acknowledge_interaction = AsyncMock()
        patcher = patch(
            "astrbot_plugin_qq_group_admin.verify_flow.QQGroupManageAPI",
            new=Mock(return_value=self.api),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.plugin.verifier = JoinVerifier(self.plugin)
        self.verifier = self.plugin.verifier

    def record(self):
        return self.plugin.storage.verification("p", "g", "new")

    async def join(self):
        record = await self.verifier.prepare("p", JOIN)
        if record is not None:
            await self.verifier.deliver("p", record)

    def message(self, text, sender="new"):
        event = test_keywords.KeywordTests.event(text, sender=sender)
        event.message_obj.message_id = "m1"
        return event

    def click(self, index, token=None, sender="new"):
        token = token or self.record()["token"]
        return NS(
            id="interaction",
            event_id="interaction-event",
            group_openid="g",
            group_member_openid=sender,
            type=11,
            chat_type=1,
            data=NS(
                resolved=NS(button_data=f"qqgv:{token}:{index}", button_id=f"qqgv-{index}")
            ),
        )

    def acks(self):
        return [call.args[1] for call in self.api.acknowledge_interaction.await_args_list]

    async def test_join_sends_buttons_only_for_new_member(self):
        await self.join()
        record = self.record()
        self.assertEqual((record["status"], record["mode"]), ("pending", "arithmetic"))
        self.assertGreater(record["deadline"], time.time())
        kwargs = self.api.send_group_markdown.await_args.kwargs
        buttons = kwargs["keyboard"]["content"]["rows"][0]["buttons"]
        self.assertEqual(len(buttons), 4)
        self.assertEqual(
            buttons[0]["action"]["permission"], {"type": 0, "specify_user_ids": ["new"]}
        )
        self.assertIn('<qqbot-at-user id="new" />', self.api.send_group_markdown.await_args.args[1])

        # 重复的进群事件不重复出题。
        await self.join()
        self.assertEqual(self.api.send_group_markdown.await_count, 1)

    async def test_disabled_or_denied_group_creates_nothing(self):
        self.settings["enable_join_verify"] = False
        await self.join()
        self.settings["enable_join_verify"] = True
        self.service_enabled.return_value = False
        await self.join()
        self.assertIsNone(self.record())
        self.api.send_group_markdown.assert_not_awaited()

    async def test_markdown_failure_switches_to_typed_answer(self):
        self.api.send_group_markdown.side_effect = RuntimeError("no markdown")
        await self.join()
        record = self.record()
        self.assertTrue(record["text_answer"])
        self.assertIn("发送答案数字", self.api.send_group_text.await_args.args[1])
        event = self.message(record["options"][record["answer"]])
        await self.plugin.join_verify_guard(event)
        self.assertIsNone(self.record())
        self.assertIn("验证通过", event.send.await_args.args[0].chain[0].text)
        self.api.recall_group_message.assert_not_awaited()

    async def test_unverified_message_is_recalled_with_rate_limited_reminder(self):
        await self.join()
        first = self.message("大家好")
        await self.plugin.join_verify_guard(first)
        self.assertTrue(first.is_stopped())
        first.send.assert_awaited_once()
        self.assertIn("请先完成进群验证", first.send.await_args.args[0].chain[0].text)
        second = self.message("在吗")
        await self.plugin.join_verify_guard(second)
        second.send.assert_not_awaited()
        self.assertEqual(self.api.recall_group_message.await_count, 2)
        self.api.recall_group_message.assert_awaited_with("g", "m1")
        self.assertEqual(self.record()["failures"], 0)

        other = self.message("普通成员", sender="member")
        await self.plugin.join_verify_guard(other)
        self.assertFalse(other.is_stopped())
        self.assertEqual(self.api.recall_group_message.await_count, 2)

    async def test_custom_answers_count_failures_then_mute(self):
        self.settings["verify_mode"] = "自定义问题"
        await self.join()
        self.assertEqual(self.record()["mode"], "custom")
        self.assertIn("答案是多少？", self.api.send_group_markdown.await_args.args[1])

        wrong = self.message("41")
        await self.plugin.join_verify_guard(wrong)
        self.assertEqual(self.record()["failures"], 1)
        self.assertIn("还剩 1 次", wrong.send.await_args.args[0].chain[0].text)
        self.api.mute_member.assert_not_awaited()

        await self.plugin.join_verify_guard(self.message("40"))
        self.assertEqual(self.record()["status"], "failed")
        kwargs = self.api.mute_member.await_args.kwargs
        self.assertEqual(kwargs["op"], "add")
        self.assertEqual(self.api.recall_group_message.await_count, 2)

        # 验证失败后再答对也不放行，只提醒等待管理员。
        later = self.message("42")
        await self.plugin.join_verify_guard(later)
        self.assertEqual(self.record()["status"], "failed")
        self.assertIn("等待管理员处理", later.send.await_args.args[0].chain[0].text)

    async def test_custom_correct_answer_passes(self):
        self.settings["verify_mode"] = "自定义问题"
        await self.join()
        event = self.message(" 42 ")
        await self.plugin.join_verify_guard(event)
        self.assertIsNone(self.record())
        self.api.recall_group_message.assert_not_awaited()
        self.assertTrue(event.is_stopped())

    async def test_without_valid_custom_questions_uses_arithmetic(self):
        self.settings.update(verify_mode="自定义问题", custom_questions=["坏正则 => ("])
        await self.join()
        self.assertEqual(self.record()["mode"], "arithmetic")

    async def test_buttons_require_owner_and_change_question_on_wrong_answer(self):
        self.assertFalse(
            await self.verifier.handle_interaction(
                "p", NS(id="x", data=NS(resolved=NS(button_data="qqga:abc")))
            )
        )
        await self.join()
        record = self.record()
        first_token, correct = record["token"], record["answer"]
        wrong = (correct + 1) % 4

        self.assertTrue(await self.verifier.handle_interaction("p", self.click(wrong, sender="other")))
        self.assertTrue(await self.verifier.handle_interaction("p", self.click(wrong)))
        self.assertEqual(self.record()["failures"], 1)
        self.assertNotEqual(self.record()["token"], first_token)
        self.assertEqual(
            self.api.send_group_markdown.await_args.kwargs["event_id"], "interaction-event"
        )
        self.assertIn("选错了，还剩 1 次", self.api.send_group_markdown.await_args.args[1])

        # 旧题目上的按钮已失效。
        await self.verifier.handle_interaction("p", self.click(correct, token=first_token))
        self.assertIsNotNone(self.record())

        await self.verifier.handle_interaction("p", self.click(self.record()["answer"]))
        self.assertIsNone(self.record())
        self.assertEqual(self.acks(), [4, 0, 3, 0])

    async def test_wrong_buttons_reach_limit_and_mute(self):
        await self.join()
        for _ in range(2):
            await self.verifier.handle_interaction(
                "p", self.click((self.record()["answer"] + 1) % 4)
            )
        self.assertEqual(self.record()["status"], "failed")
        self.api.mute_member.assert_awaited_once()
        await self.verifier.handle_interaction("p", self.click(self.record()["answer"]))
        self.assertEqual(self.acks()[-1], 3)

    async def test_timeout_actions_run_once(self):
        await self.join()
        self.record()["deadline"] = time.time() - 1
        self.settings["verify_timeout_action"] = "提醒一次"
        sent = self.api.send_group_markdown.await_count
        await self.verifier.check_timeouts()
        await self.verifier.check_timeouts()
        self.assertEqual(self.api.send_group_markdown.await_count, sent + 1)
        self.assertIn("验证时间已到", self.api.send_group_markdown.await_args.args[1])
        self.assertEqual(self.record()["status"], "pending")

        self.plugin.storage.remove_verification("p", "g", "new")
        await self.join()
        self.record()["deadline"] = time.time() - 1
        self.settings["verify_timeout_action"] = "按失败禁言"
        await self.verifier.check_timeouts()
        self.assertEqual(self.record()["status"], "failed")
        self.api.mute_member.assert_awaited_once()

    async def test_zero_timeout_never_expires(self):
        self.settings["verify_timeout_minutes"] = 0
        await self.join()
        self.assertEqual(self.record()["deadline"], 0)
        await self.verifier.check_timeouts()
        self.assertEqual(self.record()["status"], "pending")

    async def test_admin_release_restart_and_listing(self):
        await self.join()
        admin = self.message("验证列表", sender="admin")
        self.assertIn("new：待验证，已答错 0 次，剩余 10 分钟", self.verifier.listing(admin))

        self.record()["status"] = "failed"
        self.assertIn("已放行 1 名成员", await self.verifier.release(admin, ["new", "nobody"]))
        self.assertIsNone(self.record())
        self.assertEqual(self.api.mute_member.await_args.kwargs["op"], "del")
        self.assertIn("没有待处理", await self.verifier.release(admin, ["new"]))

        sent = self.api.send_group_markdown.await_count
        self.assertIn("1 名成员重新验证", await self.verifier.restart(admin, ["new"]))
        self.assertEqual(self.record()["status"], "pending")
        self.assertEqual(self.api.send_group_markdown.await_count, sent + 1)

        self.settings["enable_join_verify"] = False
        self.assertIn("未开启", await self.verifier.restart(admin, ["new"]))

    async def test_member_leave_clears_record(self):
        await self.join()
        await self.verifier.clear("p", JOIN)
        self.assertIsNone(self.record())

    async def test_verify_mode_menu_validates_choice(self):
        event = self.message("群管功能", sender="admin")
        with self.assertRaises(ValueError):
            self.plugin._keyword_config_command(event, "验证模式", "设置", "猜谜")
        self.assertEqual(
            self.plugin._keyword_config_command(event, "验证模式", "设置", "自定义问题"),
            "验证模式已保存。",
        )
        self.assertEqual(self.settings["verify_mode"], "自定义问题")

    # ---------- 审查回归：对应 review 报告 1-5 ----------

    async def test_release_waits_for_inflight_failing_click(self):
        await self.join()
        self.record()["failures"] = 1
        entered, gate = asyncio.Event(), asyncio.Event()

        async def ack(interaction_id, code):
            if code == 0:
                entered.set()
                await gate.wait()

        self.api.acknowledge_interaction.side_effect = ack
        admin = self.message("验证放行", sender="admin")
        click = asyncio.create_task(
            self.verifier.handle_interaction("p", self.click((self.record()["answer"] + 1) % 4))
        )
        await entered.wait()
        release = asyncio.create_task(self.verifier.release(admin, ["new"]))
        await asyncio.sleep(0)
        self.assertFalse(release.done())
        gate.set()
        await click
        self.assertIn("已放行 1 名成员", await release)
        self.assertIsNone(self.record())
        ops = [call.kwargs["op"] for call in self.api.mute_member.await_args_list]
        self.assertEqual(ops, ["add", "del"])

    async def test_timeout_mute_finishes_before_release_unmutes(self):
        await self.join()
        self.record()["deadline"] = time.time() - 1
        entered, gate = asyncio.Event(), asyncio.Event()

        async def mute(group, member, *, op, mute_expire_at=""):
            if op == "add":
                entered.set()
                await gate.wait()

        self.api.mute_member.side_effect = mute
        timeout = asyncio.create_task(self.verifier.check_timeouts())
        await entered.wait()
        release = asyncio.create_task(
            self.verifier.release(self.message("验证放行", sender="admin"), ["new"])
        )
        await asyncio.sleep(0)
        gate.set()
        await timeout
        await release
        self.assertIsNone(self.record())
        ops = [call.kwargs["op"] for call in self.api.mute_member.await_args_list]
        self.assertEqual(ops, ["add", "del"])

    async def test_markdown_fallback_does_not_resurrect_removed_record(self):
        entered, gate = asyncio.Event(), asyncio.Event()

        async def markdown(*args, **kwargs):
            entered.set()
            await gate.wait()
            raise RuntimeError("no markdown")

        self.api.send_group_markdown.side_effect = markdown
        record = await self.verifier.prepare("p", JOIN)
        deliver = asyncio.create_task(self.verifier.deliver("p", record))
        await entered.wait()
        leave = asyncio.create_task(self.verifier.clear("p", JOIN))
        await asyncio.sleep(0)
        gate.set()
        await deliver
        await leave
        self.assertIsNone(self.record())

    async def test_undelivered_question_is_not_punished_and_is_resent_on_message(self):
        self.api.send_group_markdown.side_effect = RuntimeError("no markdown")
        self.api.send_group_text.side_effect = RuntimeError("no active message")
        await self.join()
        record = self.record()
        self.assertEqual((record["delivered"], record["deadline"]), (False, 0))
        self.assertTrue(record["text_answer"])
        await self.verifier.check_timeouts()
        self.api.mute_member.assert_not_awaited()
        self.assertIn("题目未送达", self.verifier.listing(self.message("x", sender="admin")))

        # 补发也失败：仍不计时、不计次数，下次发言再补发。
        await self.plugin.join_verify_guard(self.message("大家好"))
        self.assertEqual((self.record()["delivered"], self.record()["deadline"]), (False, 0))
        self.assertEqual(self.record()["failures"], 0)
        await self.verifier.check_timeouts()
        self.api.mute_member.assert_not_awaited()

        # 补发拿到消息 ID 才算送达，从此刻开始计时。
        self.api.send_group_text.side_effect = None
        await self.plugin.join_verify_guard(self.message("在吗"))
        args, kwargs = self.api.send_group_text.await_args
        self.assertIn(record["question"], args[1])
        self.assertEqual(kwargs["msg_id"], "m1")
        self.assertEqual(self.record()["failures"], 0)
        self.assertTrue(self.record()["delivered"])
        self.assertGreater(self.record()["deadline"], time.time())

        await self.plugin.join_verify_guard(self.message("不知道"))
        self.assertEqual(self.record()["failures"], 1)

    async def test_failed_replacement_question_pauses_timer(self):
        await self.join()
        self.api.send_group_markdown.side_effect = RuntimeError("no markdown")
        self.api.send_group_text.side_effect = RuntimeError("no reply")
        await self.verifier.handle_interaction("p", self.click((self.record()["answer"] + 1) % 4))
        record = self.record()
        self.assertEqual(record["failures"], 1)
        self.assertEqual((record["delivered"], record["deadline"]), (False, 0))
        self.assertTrue(record["text_answer"])

        self.api.send_group_text.side_effect = None
        await self.plugin.join_verify_guard(self.message("怎么没题"))
        self.assertTrue(self.record()["delivered"])
        await self.plugin.join_verify_guard(self.message(record["options"][record["answer"]]))
        self.assertIsNone(self.record())

    async def test_interrupted_delivery_resends_answerable_text_question(self):
        # 只登记、没发题，相当于发题途中被热重载打断，或欢迎消息还在发送。
        record = await self.verifier.prepare("p", JOIN)
        await self.plugin.join_verify_guard(self.message("hello"))
        text = self.api.send_group_text.await_args.args[1]
        self.assertIn("发送答案数字", text)
        self.assertNotIn("点击下方按钮", text)
        self.assertTrue(self.record()["text_answer"])

        # 已经补发过的题目不再由正式发题流程重复发送。
        await self.verifier.deliver("p", record)
        self.api.send_group_markdown.assert_not_awaited()
        self.assertEqual(self.api.send_group_text.await_count, 1)

        await self.plugin.join_verify_guard(self.message(record["options"][record["answer"]]))
        self.assertIsNone(self.record())

    async def test_failed_member_stays_blocked_after_retention(self):
        await self.join()
        self.record().update(status="failed", joined_at=time.time() - 40 * 86400)
        self.plugin.storage.retention_days = 1
        await self.verifier.check_timeouts()
        event = self.message("禁言到期了")
        await self.plugin.join_verify_guard(event)
        self.assertTrue(event.is_stopped())
        self.api.recall_group_message.assert_awaited_once()


class JoinWelcomeOrderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        test_hot_reload.HotReloadTests.setUp(self)
        self.plugin.config["verify_settings"] = {"enable_join_verify": True}
        self.api.recall_group_message = AsyncMock()
        patcher = patch(
            "astrbot_plugin_qq_group_admin.verify_flow.QQGroupManageAPI",
            new=Mock(return_value=self.api),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_member_is_guarded_while_welcome_is_sending(self):
        entered, gate = asyncio.Event(), asyncio.Event()
        calls = []

        async def markdown(group, content, **kwargs):
            calls.append(content)
            if len(calls) == 1:
                entered.set()
                await gate.wait()  # 第一条是欢迎消息

        self.api.send_group_markdown.side_effect = markdown
        await self.plugin._patch_platforms_once()
        join = asyncio.create_task(self.client.on_group_member_add(dict(JOIN)))
        await asyncio.wait_for(entered.wait(), 1)
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(self.plugin.storage.verification("p", "g", "new"))
        event = test_keywords.KeywordTests.event("广告链接", sender="new")
        event.message_obj.message_id = "m1"
        await self.plugin.join_verify_guard(event)
        self.assertTrue(event.is_stopped())
        # 发言时已补发文字题，欢迎发完后不再重复发按钮题。
        self.assertIn("发送答案数字", self.api.send_group_text.await_args.args[1])
        gate.set()
        await join
        self.assertEqual(len(calls), 1)
        self.assertIn("欢迎", calls[0])
        self.assertTrue(self.plugin.storage.verification("p", "g", "new")["delivered"])

    async def test_question_follows_welcome_when_member_is_silent(self):
        calls = []

        async def markdown(group, content, **kwargs):
            calls.append(content)

        self.api.send_group_markdown.side_effect = markdown
        await self.plugin._patch_platforms_once()
        await self.client.on_group_member_add(dict(JOIN))
        self.assertIn("欢迎", calls[0])
        self.assertIn("进群验证", calls[1])


class VerifyStorageTests(unittest.TestCase):
    def test_old_state_loads_and_active_records_are_not_pruned(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"group_admins": {}}), encoding="utf-8")
            storage = PluginStorage(path, retention_days=1)
            self.assertEqual(storage.verifications(), [])
            # 验证失败的成员要等管理员处理，保留期过了也不能自动放行。
            storage.put_verification(
                {
                    "platform_id": "p",
                    "group_openid": "g",
                    "member_openid": "old",
                    "status": "failed",
                    "joined_at": time.time() - 40 * 86400,
                }
            )
            storage.prune()
            reloaded = PluginStorage(path, retention_days=1)
            self.assertEqual(
                [record["member_openid"] for record in reloaded.verifications()], ["old"]
            )


if __name__ == "__main__":
    unittest.main()
