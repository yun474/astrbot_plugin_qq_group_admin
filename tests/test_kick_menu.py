"""分类蓝字、踢人授权、异常去重与验证移出的回归测试。"""

import asyncio
import json
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlparse
import re

from astrbot.api.message_components import At, Plain
from astrbot.core.star.filter.command import CommandFilter
from astrbot_plugin_qq_group_admin.api import QQGroupManageAPI
from astrbot_plugin_qq_group_admin.group_config import GroupConfig, SCHEMA
from astrbot_plugin_qq_group_admin.member_removal import MemberRemover
from astrbot_plugin_qq_group_admin.settings_menu import PAGES
from astrbot_plugin_qq_group_admin.storage import PluginStorage
from http_fakes import make_http
import test_group_config
import test_join_verify
import test_keywords
import test_hot_reload


def result_text(result):
    return "".join(part.text for part in result.chain if isinstance(part, Plain))


class KickMenuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        test_keywords.KeywordTests.setUp(self)
        self.api.remove_members = AsyncMock(
            return_value={"remove_members_result": "success"}
        )
        patcher = patch(
            "astrbot_plugin_qq_group_admin.member_removal.QQGroupManageAPI",
            new=Mock(return_value=self.api),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.plugin.remover = MemberRemover(self.plugin)

    def event(self, text="群管功能", role="admin", targets=()):
        event = test_keywords.KeywordTests.event(text)
        event.message_obj.message_id = "message-1"
        event.message_obj.raw_message = {"author": {"member_role": role}}
        event.message_obj.message.extend(At(qq=target) for target in targets)
        return event

    async def command(self, event, name="", action="", value=""):
        return [
            "".join(part.text for part in result.chain if isinstance(part, Plain))
            async for result in self.plugin.group_feature_settings(
                event, name, action, value
            )
        ]

    async def test_all_categories_are_readable_in_global_mode_without_changing_config(
        self,
    ):
        before = json.dumps(self.plugin.config, sort_keys=True)
        home = "".join(await self.command(self.event()))
        self.assertLessEqual(len(home.splitlines()), 12)
        self.assertNotIn("违禁词时长", home)
        self.assertNotIn("群 UMO", home)
        for page in PAGES:
            result = "".join(await self.command(self.event(), page))
            self.assertIn(page, result)
            self.assertIn("返回首页", result)
            self.assertNotIn("填入", result)
            self.assertNotIn("补全", result)
        self.assertEqual(before, json.dumps(self.plugin.config, sort_keys=True))

    async def test_plugin_admin_entries_only_appear_in_permission_page(self):
        member = "".join(await self.command(self.event(), "成员管理"))
        permissions = "".join(await self.command(self.event(), "群信息与权限"))
        self.assertNotIn("添加群管", member)
        for label in (
            "群管列表",
            "添加群管",
            "删除群管",
            "群主/管理员可踢人",
            "插件群管可踢人",
        ):
            self.assertIn(label, permissions)

    async def test_home_blue_links_dispatch_to_category_pages(self):
        home = "".join(await self.command(self.event()))
        for url in re.findall(r"\]\((mqqapi://[^)]+)\)", home):
            query = parse_qs(urlparse(url).query)
            self.assertEqual(query["enter"], ["false"])
            command, page = query["command"][0].split(maxsplit=1)
            self.assertEqual(command, "群管功能")
            event = self.event(text=query["command"][0])
            event.is_at_or_wake_command = True
            command_filter = CommandFilter(
                "群管功能",
                handler_md=NS(handler=type(self.plugin).group_feature_settings),
            )
            self.assertTrue(command_filter.filter(event, {}))
            params = event.get_extra("parsed_params")
            results = [
                result
                async for result in self.plugin.group_feature_settings(event, **params)
            ]
            self.assertIn(
                page,
                "".join(
                    part.text for part in results[0].chain if isinstance(part, Plain)
                ),
            )

    async def test_successful_old_command_does_not_clear_new_join_verification(self):
        event = self.event(targets=["a"])
        _ = [r async for r in self.plugin.kick_command(event)]
        record = {
            "platform_id": "p",
            "group_openid": "g",
            "member_openid": "a",
            "token": "new-join",
        }
        self.plugin.storage.put_verification(record)
        _ = [r async for r in self.plugin.kick_command(event)]
        self.assertEqual(
            self.plugin.storage.verification("p", "g", "a")["token"], "new-join"
        )
        self.api.remove_members.assert_awaited_once()

    async def test_failure_to_save_before_request_can_be_retried_without_side_effect(
        self,
    ):
        remover = self.plugin.remover
        with patch.object(
            self.plugin.storage, "save", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                await remover.remove("p", "g", "a", "disk-failure")
        self.api.remove_members.assert_not_awaited()
        self.assertEqual(
            self.plugin.storage.removal_result("p", "g", "a", "disk-failure"), ""
        )
        await remover.remove("p", "g", "a", "disk-failure")
        self.api.remove_members.assert_awaited_once()

    async def test_plugin_admin_cannot_enable_automatic_kick_to_bypass_permission(self):
        self.plugin.storage.add_group_admin("g", "member")
        self.plugin.config["scope_settings"] = {
            "enable_per_group_feature_settings": True
        }
        event = self.event(role="member")
        for name, action, value in (
            ("违禁词踢人", "开启", ""),
            ("踢人词", "添加", "广告"),
            ("验证失败处理", "设置", "移出群聊"),
        ):
            result = "".join(await self.command(event, name, action, value))
            self.assertIn("需要本群踢人权限", result)

    async def test_kick_aliases_reach_real_command_filter(self):
        command_filter = CommandFilter(
            "踢出",
            alias={"踢人"},
            handler_md=NS(handler=type(self.plugin).kick_command),
        )
        for name in ("踢出", "踢人"):
            event = self.event(text=name, targets=[name])
            event.message_obj.message_id = name
            event.is_at_or_wake_command = True
            self.assertTrue(command_filter.filter(event, {}))
            results = [
                result_text(r)
                async for r in self.plugin.kick_command(
                    event, **event.get_extra("parsed_params")
                )
            ]
            self.assertIn("已移出群聊", "".join(results))

    def test_kick_permission_switches_are_independent(self):
        self.plugin.storage.add_group_admin("g", "assigned")
        self.plugin.config["permission_settings"] = {
            "allow_native_admin_kick": False,
            "allow_plugin_admin_kick": True,
        }
        self.assertFalse(self.plugin._can_kick(self.event(role="owner")))
        self.assertFalse(self.plugin._can_kick(self.event(role="admin")))
        assigned = self.event(role="member")
        assigned.message_obj.sender.user_id = "assigned"
        self.assertTrue(self.plugin._can_kick(assigned))
        self.plugin.config["permission_settings"].update(
            allow_native_admin_kick=True, allow_plugin_admin_kick=False
        )
        self.assertTrue(self.plugin._can_kick(self.event(role="owner")))
        self.assertTrue(self.plugin._can_kick(self.event(role="admin")))
        self.assertFalse(self.plugin._can_kick(assigned))
        assigned.role = "admin"
        self.assertTrue(self.plugin._can_kick(assigned))

    async def test_plugin_admin_cannot_grant_itself_kick_permission(self):
        self.plugin.storage.add_group_admin("g", "member")
        self.plugin.config["scope_settings"] = {
            "enable_per_group_feature_settings": True
        }
        result = "".join(
            await self.command(self.event(role="member"), "插件群管可踢人", "开启")
        )
        self.assertIn("仅 AstrBot 管理员", result)
        self.assertFalse(
            self.plugin._event_feature_setting(
                self.event(), "allow_plugin_admin_kick", False
            )
        )

    async def test_command_denies_unauthorized_user_and_disabled_feature(self):
        result = [
            result_text(r)
            async for r in self.plugin.kick_command(
                self.event(role="member", targets=["target"])
            )
        ]
        self.assertIn("没有本群踢人权限", "".join(result))
        self.plugin.config["command_settings"] = {"enable_kick_command": False}
        result = [
            result_text(r)
            async for r in self.plugin.kick_command(self.event(targets=["target"]))
        ]
        self.assertIn("已关闭", "".join(result))
        self.api.remove_members.assert_not_awaited()

    async def test_command_handles_multiple_members_and_deduplicates_delivery(self):
        event = self.event(targets=["a", "b"])
        first = [result_text(r) async for r in self.plugin.kick_command(event)]
        second = [result_text(r) async for r in self.plugin.kick_command(event)]
        self.assertIn("成功 2 人", "".join(first))
        self.assertIn("成功 2 人", "".join(second))
        self.assertEqual(self.api.remove_members.await_count, 2)

    async def test_more_than_ten_targets_are_rejected_without_silent_truncation(self):
        event = self.event(targets=[f"m{i}" for i in range(11)])
        result = [result_text(r) async for r in self.plugin.kick_command(event)]
        self.assertIn("最多移出 10", "".join(result))
        self.api.remove_members.assert_not_awaited()

    async def test_transport_error_is_persisted_and_not_retried_after_reload(self):
        self.api.remove_members.side_effect = TimeoutError()
        event = self.event(targets=["a"])
        first = [result_text(r) async for r in self.plugin.kick_command(event)]
        self.assertIn("结果待确认", "".join(first))
        self.plugin.storage = PluginStorage(self.plugin.storage.path)
        self.plugin.remover = MemberRemover(self.plugin)
        second = [result_text(r) async for r in self.plugin.kick_command(event)]
        self.assertIn("结果待确认", "".join(second))
        self.api.remove_members.assert_awaited_once()

    async def test_clear_api_permission_failure_has_actionable_feedback(self):
        error = RuntimeError("permission denied")
        error.qq_code = 40103012
        self.api.remove_members.side_effect = error
        result = [
            result_text(r)
            async for r in self.plugin.kick_command(self.event(targets=["a"]))
        ]
        self.assertIn("请群主", "".join(result))

    async def test_rate_limit_prevents_the_thirty_first_request(self):
        remover = self.plugin.remover
        remover._requests.extend([time.monotonic()] * 30)
        with self.assertRaisesRegex(RuntimeError, "30 次/分钟"):
            await remover.remove("p", "g", "a", "rate")
        self.api.remove_members.assert_not_awaited()

    async def test_disabled_group_is_checked_before_network_request(self):
        self.plugin._sdk_group_enabled = AsyncMock(return_value=False)
        with self.assertRaisesRegex(ValueError, "未启用"):
            await self.plugin.remover.remove("p", "g", "a", "disabled")
        self.api.remove_members.assert_not_awaited()

    async def test_keyword_kick_uses_independent_words_and_takes_priority_over_mute(
        self,
    ):
        self.settings.update(
            enable_keyword_kick=True, kick_keywords=["广告"], enable_keyword_recall=True
        )
        self.api.recall_group_message = AsyncMock()
        event = self.event(text="违禁 广告")
        await self.plugin.keyword_mute(event)
        self.assertTrue(event.is_stopped())
        self.api.remove_members.assert_awaited_once_with("g", ["member"])
        self.api.recall_group_message.assert_awaited_once()
        self.api.mute_member.assert_not_awaited()

    async def test_keyword_kick_only_mode_does_not_moderate_other_word_lists(self):
        self.settings.update(
            enable_keyword_kick=True, kick_keywords=["广告"], enable_keyword_mute=False
        )
        event = self.event(text="违禁内容")
        await self.plugin.keyword_mute(event)
        self.assertFalse(event.is_stopped())
        self.api.remove_members.assert_not_awaited()

    async def test_llm_kick_always_respects_kick_authorization(self):
        self.plugin.config["llm_tool_settings"] = {
            "enable_kick_tool": True,
            "strict_llm_permissions": False,
        }
        self.assertIn(
            "没有本群踢人权限",
            await self.plugin.kick_tool(self.event(role="member"), "a"),
        )
        self.assertIn(
            "已成功", await self.plugin.kick_tool(self.event(role="admin"), "a")
        )
        self.api.remove_members.assert_awaited_once()

    async def test_manual_and_llm_kick_honor_isolated_session_whitelist(self):
        config = self.plugin.context.get_config.return_value
        config["platform_settings"] = {
            "unique_session": True,
            "enable_id_white_list": True,
            "id_whitelist": ["p:GroupMessage:member_g"],
        }
        self.plugin.config["llm_tool_settings"] = {"enable_kick_tool": True}
        self.assertFalse(await self.plugin._sdk_group_enabled("p:GroupMessage:g"))
        event = self.event(targets=["manual-target"])
        results = [
            result_text(result) async for result in self.plugin.kick_command(event)
        ]
        self.assertIn("已移出群聊", "".join(results))
        self.assertIn("已成功", await self.plugin.kick_tool(event, "tool-target"))
        self.assertEqual(self.api.remove_members.await_count, 2)
        with self.assertRaisesRegex(ValueError, "未启用"):
            await self.plugin.remover.remove(
                "p", "g", "automatic-target", "verify:token"
            )

    async def test_manual_kick_honors_framework_admin_whitelist_exemption(self):
        config = self.plugin.context.get_config.return_value
        config["admins_id"] = ["member"]
        config["platform_settings"] = {
            "unique_session": True,
            "enable_id_white_list": True,
            "id_whitelist": ["other-group"],
            "wl_ignore_admin_on_group": True,
        }
        event = self.event(targets=["target"])
        event.role = "admin"
        results = [
            result_text(result) async for result in self.plugin.kick_command(event)
        ]
        self.assertIn("已移出群聊", "".join(results))
        self.api.remove_members.assert_awaited_once()

    async def test_queued_kick_rechecks_authorization_and_feature_before_request(self):
        for revoked in ("native", "assigned", "command", "tool"):
            with self.subTest(revoked=revoked):
                self.plugin.config["permission_settings"] = {
                    "allow_native_admin_kick": True,
                    "allow_plugin_admin_kick": True,
                }
                self.plugin.config["command_settings"] = {"enable_kick_command": True}
                self.plugin.config["llm_tool_settings"] = {"enable_kick_tool": True}
                self.plugin.storage.add_group_admin("g", "member")
                lock, waiting = asyncio.Lock(), asyncio.Event()
                await lock.acquire()

                def member_lock(key):
                    waiting.set()
                    return lock

                self.plugin.verifier = NS(_lock=member_lock)
                event = self.event(
                    role="member" if revoked == "assigned" else "admin",
                    targets=["target"],
                )
                event.message_obj.message_id = revoked

                async def run():
                    if revoked == "tool":
                        return await self.plugin.kick_tool(event, "target")
                    return "".join(
                        [result_text(r) async for r in self.plugin.kick_command(event)]
                    )

                task = asyncio.create_task(run())
                try:
                    await asyncio.wait_for(waiting.wait(), 1)
                    if revoked == "native":
                        self.plugin.config["permission_settings"][
                            "allow_native_admin_kick"
                        ] = False
                        self.plugin.storage.remove_group_admin("g", "member")
                    elif revoked == "assigned":
                        self.plugin.storage.remove_group_admin("g", "member")
                    elif revoked == "command":
                        self.plugin.config["command_settings"][
                            "enable_kick_command"
                        ] = False
                    else:
                        self.plugin.config["llm_tool_settings"]["enable_kick_tool"] = (
                            False
                        )
                finally:
                    lock.release()
                result = await asyncio.wait_for(task, 1)
                self.assertIn("当前踢人权限或功能规则已关闭", result)
                self.api.remove_members.assert_not_awaited()

    async def test_queued_keyword_kick_rechecks_current_rules(self):
        for changed in ("switch", "words"):
            self.settings.update(enable_keyword_kick=True, kick_keywords=["广告"])
            lock, waiting = asyncio.Lock(), asyncio.Event()
            await lock.acquire()

            def member_lock(key):
                waiting.set()
                return lock

            self.plugin.verifier = NS(_lock=member_lock)
            event = self.event(text="广告")
            event.message_obj.message_id = changed
            task = asyncio.create_task(self.plugin.keyword_mute(event))
            try:
                await asyncio.wait_for(waiting.wait(), 1)
                if changed == "switch":
                    self.settings["enable_keyword_kick"] = False
                else:
                    self.settings["kick_keywords"] = []
            finally:
                lock.release()
            await asyncio.wait_for(task, 1)
            self.api.remove_members.assert_not_awaited()

    async def test_success_response_must_explicitly_confirm_removal(self):
        self.api.remove_members.return_value = {}
        with self.assertRaisesRegex(RuntimeError, "未返回成功"):
            await self.plugin.remover.remove("p", "g", "a", "empty")

    async def test_simultaneous_requests_for_same_member_only_submit_once(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def remove(*args):
            entered.set()
            await release.wait()
            return {"remove_members_result": "success"}

        self.api.remove_members.side_effect = remove
        remover = self.plugin.remover
        task = asyncio.create_task(remover.remove("p", "g", "a", "first"))
        await entered.wait()
        with self.assertRaisesRegex(ValueError, "正在移出"):
            await remover.remove("p", "g", "a", "second")
        release.set()
        await task
        self.api.remove_members.assert_awaited_once()

    async def test_real_sdk_request_uses_official_path_and_preserves_error_code(self):
        http = make_http(data={"remove_members_result": "success"})
        api = QQGroupManageAPI(NS(api=NS(_http=http)))
        await api.remove_members("g/a", ["a", "a", "b"])
        kwargs = http._session.request.call_args.kwargs
        self.assertTrue(kwargs["url"].endswith("/v2/groups/g%2Fa/batch_remove_members"))
        self.assertEqual(kwargs["json"], {"member_openids": ["a", "b"]})
        for members in ([], [str(i) for i in range(21)]):
            with self.assertRaises(ValueError):
                await api.remove_members("g", members)
        http = make_http(status=403, data={"code": 40103011, "message": "denied"})
        api = QQGroupManageAPI(NS(api=NS(_http=http)))
        with self.assertRaises(RuntimeError) as caught:
            await api.remove_members("g", ["a"])
        self.assertEqual(caught.exception.qq_code, 40103011)


class KickReloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        test_hot_reload.HotReloadTests.setUp(self)
        self.plugin.remover = MemberRemover(self.plugin)
        self.api.remove_members = AsyncMock()
        patcher = patch(
            "astrbot_plugin_qq_group_admin.member_removal.QQGroupManageAPI",
            return_value=self.api,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_unload_waits_for_manual_kick_cancellation_before_loading_new_state(
        self,
    ):
        entered, cancelling, release_cleanup = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )

        async def request(*args):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelling.set()
                await release_cleanup.wait()
                raise

        self.api.remove_members.side_effect = request
        self.plugin.storage.add_group_admin("g", "revoked-admin")
        event = test_keywords.KeywordTests.event("踢出")
        event.message_obj.raw_message = {"author": {"member_role": "admin"}}
        event.message_obj.message.append(At(qq="target"))
        event.message_obj.message_id = "manual-request"

        async def run_command():
            return [r async for r in self.plugin.kick_command(event)]

        command = asyncio.create_task(run_command())
        await asyncio.wait_for(entered.wait(), 1)
        unloading = asyncio.create_task(self.plugin.terminate())
        try:
            await asyncio.wait_for(cancelling.wait(), 1)
            self.assertFalse(unloading.done())
        finally:
            release_cleanup.set()
            await asyncio.wait_for(unloading, 1)
            await asyncio.gather(command, return_exceptions=True)
        self.assertTrue(command.cancelled())
        self.assertFalse(self.plugin.remover._tasks)
        new_storage = PluginStorage(self.plugin.storage.path)
        new_storage.remove_group_admin("g", "revoked-admin")
        await asyncio.sleep(0)
        self.assertEqual(PluginStorage(new_storage.path).group_admins("g"), [])
        self.assertEqual(
            new_storage.removal_result("p", "g", "target", "command:manual-request"),
            "unknown",
        )

    async def test_retired_instance_rejects_late_removal(self):
        await self.plugin.terminate()
        with self.assertRaisesRegex(RuntimeError, "正在重载"):
            await self.plugin.remover.remove("p", "g", "target", "late-request")
        self.api.remove_members.assert_not_awaited()


class KickConfigTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_group_config.GroupConfigTests.setUp
    migrate = test_group_config.GroupConfigTests.migrate
    event = test_group_config.GroupConfigTests.event
    command = test_group_config.GroupConfigTests.command

    def test_webui_permissions_match_in_global_and_group_schemas(self):
        legacy = SCHEMA["permission_settings"]["items"]
        management = SCHEMA["group_management"]["items"]
        global_items = management["global_settings"]["items"]["permission_settings"][
            "items"
        ]
        group_items = management["groups"]["templates"]["group"]["items"][
            "permission_settings"
        ]["items"]
        for key in ("allow_native_admin_kick", "allow_plugin_admin_kick"):
            self.assertEqual(legacy[key], global_items[key])
            self.assertEqual(legacy[key], group_items[key])

    def test_existing_group_profiles_receive_kick_defaults_independently(self):
        self.migrate()
        profile = self.store.snapshot("p:GroupMessage:g")
        for key in ("allow_native_admin_kick", "allow_plugin_admin_kick"):
            del profile["permission_settings"][key]
        self.store.data["groups"].append(profile)
        self.store.data["global_settings"]["permission_settings"].update(
            allow_native_admin_kick=False, allow_plugin_admin_kick=True
        )
        self.store.migrate(self.storage, self.context)
        permissions = self.store.profile("p:GroupMessage:g")["permission_settings"]
        self.assertTrue(permissions["allow_native_admin_kick"])
        self.assertFalse(permissions["allow_plugin_admin_kick"])

    async def test_both_kick_permissions_save_to_webui_group_profile(self):
        self.migrate()
        await self.command(self.event(), "群主/管理员可踢人", "关闭")
        await self.command(self.event(), "插件群管可踢人", "开启")
        self.config = type(self.config)(str(self.path), schema=SCHEMA)
        profile = GroupConfig(self.config).profile("p:GroupMessage:g")
        self.assertFalse(profile["permission_settings"]["allow_native_admin_kick"])
        self.assertTrue(profile["permission_settings"]["allow_plugin_admin_kick"])
        self.assertTrue(
            self.plugin._event_feature_setting(
                self.event("other"), "allow_native_admin_kick"
            )
        )
        self.assertFalse(
            self.plugin._event_feature_setting(
                self.event("other"), "allow_plugin_admin_kick", False
            )
        )

    async def test_verification_action_and_numeric_settings_save_and_validate(self):
        self.migrate()
        for name, value in (
            ("验证失败处理", "移出群聊"),
            ("验证超时处理", "移出群聊"),
            ("验证答错次数", "4"),
            ("验证限时", "0"),
        ):
            await self.command(self.event(), name, "设置", value)
        values = GroupConfig(self.config).profile("p:GroupMessage:g")["verify_settings"]
        self.assertEqual(values["verify_fail_action"], "移出群聊")
        self.assertEqual(values["verify_timeout_action"], "移出群聊")
        self.assertEqual(values["verify_max_failures"], 4)
        self.assertEqual(values["verify_timeout_minutes"], 0)
        await self.command(self.event(), "验证答错次数", "设置", "0")
        self.assertEqual(values["verify_max_failures"], 4)


class KickVerifyTests(unittest.IsolatedAsyncioTestCase):
    record = test_join_verify.JoinVerifyFlowTests.record
    join = test_join_verify.JoinVerifyFlowTests.join
    message = test_join_verify.JoinVerifyFlowTests.message

    def setUp(self):
        test_join_verify.JoinVerifyFlowTests.setUp(self)
        self.api.remove_members = AsyncMock(
            return_value={"remove_members_result": "success"}
        )
        patcher = patch(
            "astrbot_plugin_qq_group_admin.member_removal.QQGroupManageAPI",
            new=Mock(return_value=self.api),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.plugin.remover = MemberRemover(self.plugin)

    async def test_wrong_answer_limit_kicks_and_invalidates_verification(self):
        self.settings.update(verify_mode="自定义问题", verify_fail_action="移出群聊")
        await self.join()
        await self.verifier.guard(self.message("wrong"))
        await self.verifier.guard(self.message("wrong"))
        self.assertIsNone(self.record())
        self.api.remove_members.assert_awaited_once_with("g", ["new"])
        self.api.mute_member.assert_not_awaited()

    async def test_verification_disabled_during_scope_check_does_not_kick(self):
        self.settings["verify_fail_action"] = "移出群聊"
        await self.join()

        async def scope_check(*args, **kwargs):
            self.settings["enable_join_verify"] = False
            return True

        self.plugin._sdk_group_enabled = scope_check
        async with self.verifier._lock(("p", "g", "new")):
            await self.verifier._fail(
                self.plugin.context.get_platform_inst("p"), self.record()
            )
        self.api.remove_members.assert_not_awaited()
        self.assertEqual(self.record()["status"], "failed")

    async def test_timeout_can_kick_independently_of_wrong_answer_action(self):
        self.settings.update(
            verify_fail_action="禁言", verify_timeout_action="移出群聊"
        )
        await self.join()
        self.record()["deadline"] = time.time() - 1
        await self.verifier.check_timeouts()
        await self.verifier.check_timeouts()
        self.assertIsNone(self.record())
        self.api.remove_members.assert_awaited_once()
        self.api.mute_member.assert_not_awaited()

    async def test_timeout_mute_is_independent_of_wrong_answer_kick(self):
        self.settings.update(
            verify_fail_action="移出群聊", verify_timeout_action="按失败禁言"
        )
        await self.join()
        self.record()["deadline"] = time.time() - 1
        await self.verifier.check_timeouts()
        self.assertEqual(self.record()["fail_action"], "禁言")
        self.api.mute_member.assert_awaited_once()
        self.api.remove_members.assert_not_awaited()

    async def test_failed_kick_keeps_record_and_release_does_not_unmute(self):
        self.settings.update(verify_fail_action="移出群聊")
        self.api.remove_members.side_effect = TimeoutError()
        await self.join()
        async with self.verifier._lock(("p", "g", "new")):
            await self.verifier._fail(
                self.plugin.context.get_platform_inst("p"), self.record()
            )
        self.assertEqual(self.record()["status"], "failed")
        await self.verifier.release(self.message("验证放行", sender="admin"), ["new"])
        self.api.mute_member.assert_not_awaited()
