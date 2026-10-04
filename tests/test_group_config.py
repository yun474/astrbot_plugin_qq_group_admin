from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from astrbot.api import AstrBotConfig
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot_plugin_qq_group_admin.group_config import GroupConfig, SCHEMA
from astrbot_plugin_qq_group_admin.main import QQGroupAdminPlugin
from astrbot_plugin_qq_group_admin.settings_menu import word_page
from astrbot_plugin_qq_group_admin.storage import PluginStorage
import test_keywords


class GroupConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "plugin.json"
        self.path.write_text(
            json.dumps(
                {
                    "config_layout_version": 1,
                    "scope_settings": {"enable_per_group_feature_settings": True},
                    "keyword_settings": {
                        "join_whitelist_words": ["old white"],
                        "mute_keywords": ["global bad"],
                        "keyword_mute_duration": "12分",
                    },
                    "command_settings": {"default_mute_duration": "8分"},
                    "join_request_settings": {"pending_retention_days": 60},
                }
            ),
            encoding="utf-8",
        )
        self.config = AstrBotConfig(str(self.path), schema=SCHEMA)
        self.storage = PluginStorage(self.root / "state.json")
        self.context = NS(get_config=lambda umo: {})
        self.store = GroupConfig(self.config)
        self.plugin = object.__new__(QQGroupAdminPlugin)
        self.plugin.config = self.config
        self.plugin.storage = self.storage
        self.plugin.context = self.context
        self.plugin._reviews_inflight = set()

    def migrate(self):
        self.plugin._migrate_config_layout()
        return self.store.migrate(self.storage, self.context)

    def event(self, group="g", text="群管功能", sender="member"):
        event = test_keywords.KeywordTests.event(text, group=group, sender=sender)
        event.role = "admin"
        return event

    async def command(self, event, name="", action="", value=""):
        return [
            r
            async for r in self.plugin.group_feature_settings(
                event, name, action, value
            )
        ]

    def test_real_framework_load_migrates_old_config_and_overrides_without_loss(self):
        self.storage.set_group_feature_override(
            "p:GroupMessage:g", "enable_keyword_mute", True
        )
        self.storage.add_group_admin("g", "admin")
        self.storage.put_pending(
            "old-card",
            {"platform_id": "p", "group_openid": "g", "join_request_id": "r"},
        )
        before_state = self.storage.path.read_bytes()
        self.assertEqual(self.migrate(), [])
        profile = self.store.profile("p:GroupMessage:g")
        self.assertEqual(profile["keyword_settings"]["mute_keywords"], ["global bad"])
        self.assertEqual(profile["command_settings"]["default_mute_duration"], "8分")
        self.assertTrue(profile["keyword_settings"]["enable_keyword_mute"])
        self.assertEqual(self.plugin._config("pending_retention_days"), 60)
        self.assertEqual(self.storage.path.read_bytes(), before_state)
        backup = self.storage.path.with_name("group-config-before-v2.json")
        original_backup = backup.read_bytes()
        reloaded = AstrBotConfig(str(self.path), schema=SCHEMA)
        self.assertEqual(reloaded["group_management"], self.config["group_management"])
        reloaded["group_management"]["groups"] = []
        reloaded.save_config()
        GroupConfig(reloaded).migrate(self.storage, self.context)
        self.assertEqual(reloaded["group_management"]["groups"], [])
        self.assertEqual(backup.read_bytes(), original_backup)

    def test_flat_v0_migration_keeps_nondefault_values(self):
        self.path.write_text(
            json.dumps({"enable_mute_command": False, "default_mute_duration": "19分"}),
            encoding="utf-8",
        )
        self.plugin.config = AstrBotConfig(str(self.path), schema=SCHEMA)
        self.plugin._migrate_config_layout()
        GroupConfig(self.plugin.config).migrate(self.storage, self.context)
        self.assertFalse(self.plugin._config("enable_mute_command"))
        self.assertEqual(self.plugin._config("default_mute_duration"), "19分")

    def test_ambiguous_legacy_user_ids_are_preserved_for_confirmation(self):
        self.storage.set_group_feature_override(
            "p:GroupMessage:user", "enable_keyword_mute", True
        )
        self.storage.set_group_feature_override(
            "p:GroupMessage:known-group", "enable_keyword_mute", True
        )
        self.storage.put_pending(
            "old",
            {"platform_id": "p", "group_openid": "known-group", "join_request_id": "r"},
        )
        self.context.get_config = lambda umo: {
            "platform_settings": {"unique_session": True}
        }
        self.assertEqual(self.migrate(), ["p:GroupMessage:user"])
        self.assertIsNone(self.store.profile("p:GroupMessage:user"))
        self.assertTrue(self.store.profile("p:GroupMessage:known-group")["confirmed"])
        suspect = self.store.data["groups"][0]
        self.assertEqual(suspect["umo"], "p:GroupMessage:user")
        self.assertTrue(suspect["migration_note"])
        suspect["umo"], suspect["confirmed"] = "p:GroupMessage:actual-group", True
        self.assertTrue(
            self.plugin._feature_setting(
                "enable_keyword_mute", "p:GroupMessage:actual-group", False
            )
        )

    async def test_same_user_in_two_groups_and_different_users_in_one_group(self):
        self.migrate()
        stage = WakingCheckStage()
        await stage.initialize(
            NS(
                astrbot_config={
                    "wake_prefix": [""],
                    "platform_settings": {"unique_session": True},
                    "admins_id": ["member", "other"],
                }
            )
        )
        events = [
            self.event("g"),
            self.event("other-group"),
            self.event("g", sender="other"),
        ]
        with (
            patch(
                "astrbot.core.pipeline.waking_check.stage.star_handlers_registry.get_handlers_by_event_type",
                return_value=[],
            ),
            patch(
                "astrbot.core.pipeline.waking_check.stage.SessionPluginManager.filter_handlers_by_session",
                new=AsyncMock(return_value=[]),
            ),
        ):
            for event in events:
                await stage.process(event)
        await self.command(events[0], "入群关键词审批", "开启")
        self.assertTrue(
            self.plugin._event_feature_setting(
                events[0], "enable_join_keyword_review", False
            )
        )
        self.assertFalse(
            self.plugin._event_feature_setting(
                events[1], "enable_join_keyword_review", False
            )
        )
        self.assertTrue(
            self.plugin._event_feature_setting(
                events[2], "enable_join_keyword_review", False
            )
        )
        self.assertTrue(
            self.plugin._feature_setting(
                "enable_join_keyword_review", "p:GroupMessage:g", False
            )
        )
        self.assertEqual(self.store.data["groups"][0]["umo"], "p:GroupMessage:g")

    async def test_word_edits_are_saved_to_dashboard_config_and_reload(self):
        self.migrate()
        event = self.event(text="群管功能 违禁词 添加 two words")
        await self.command(event, "违禁词", "添加", "two")
        reloaded = AstrBotConfig(str(self.path), schema=SCHEMA)
        words = GroupConfig(reloaded).profile("p:GroupMessage:g")["keyword_settings"][
            "mute_keywords"
        ]
        self.assertEqual(words, ["global bad", "two words"])
        self.assertEqual(len(self.store.data["groups"]), 1)
        await self.command(self.event(), "违禁词", "添加", "TWO WORDS")
        self.assertEqual(
            len(self.store.data["groups"][0]["keyword_settings"]["mute_keywords"]), 2
        )
        await self.command(self.event(), "违禁词", "删除", "two words")
        self.assertEqual(
            self.plugin._event_setting(event, "mute_keywords"), ["global bad"]
        )
        await self.command(self.event(), "违禁词", "清空", "确认")
        self.assertEqual(self.plugin._event_setting(event, "mute_keywords"), [])

    async def test_global_mode_ignores_but_keeps_group_profiles(self):
        self.migrate()
        await self.command(self.event(), "违禁词时长", "设置", "2小时")
        self.store.data["enabled"] = False
        self.assertEqual(
            self.plugin._event_setting(self.event(), "keyword_mute_duration"), "12分"
        )
        await self.command(self.event(), "违禁词时长", "设置", "5分")
        self.assertEqual(
            self.plugin._event_setting(self.event(), "keyword_mute_duration"), "5分"
        )
        self.store.data["enabled"] = True
        self.assertEqual(
            self.plugin._event_setting(self.event(), "keyword_mute_duration"), "2小时"
        )
        self.assertEqual(
            self.plugin._event_setting(
                self.event("new-group"), "keyword_mute_duration"
            ),
            "5分",
        )

    async def test_native_group_admin_can_read_but_cannot_modify_global_keywords(self):
        self.migrate()
        self.store.data["enabled"] = False
        event = self.event()
        event.role = "member"
        event.message_obj.raw_message = {"author": {"member_role": "admin"}}
        before = deepcopy(dict(self.config))
        results = await self.command(event, "违禁词", "查看")
        self.assertIn("global bad", str(results))
        await self.command(event, "违禁词", "添加", "forbidden-change")
        self.assertEqual(self.config, before)

    async def test_authorized_settings_commands_are_not_muted_but_regular_messages_are(
        self,
    ):
        self.migrate()
        self.store.set_value(
            "p:GroupMessage:g", "keyword_settings", "enable_keyword_mute", True
        )
        event = self.event(text="群管功能 违禁词 删除 global bad")
        event.is_at_or_wake_command = True
        self.plugin._mute = AsyncMock()
        with patch.object(
            self.plugin, "_sdk_group_enabled", new=AsyncMock(return_value=True)
        ):
            await self.plugin.keyword_mute(event)
            self.assertFalse(event.is_stopped())
            await self.plugin.keyword_mute(self.event(text="global bad"))
        self.plugin._mute.assert_awaited_once()

    async def test_group_values_reach_mute_and_join_api(self):
        self.migrate()
        for key, value in {
            "enable_keyword_mute": True,
            "mute_keywords": ["group bad"],
            "keyword_mute_duration": "2分",
            "enable_join_keyword_review": True,
            "join_whitelist_words": ["yes"],
            "join_blacklist_words": ["no"],
            "join_keyword_priority": "白词优先",
        }.items():
            self.store.set_value("p:GroupMessage:g", "keyword_settings", key, value)
        self.plugin.context.get_platform_inst = lambda pid: NS(client=object())
        api = NS(review_join_request=AsyncMock())
        self.plugin._mute = AsyncMock()
        with (
            patch.object(
                self.plugin, "_sdk_group_enabled", new=AsyncMock(return_value=True)
            ),
            patch(
                "astrbot_plugin_qq_group_admin.main.QQGroupManageAPI", return_value=api
            ),
        ):
            await self.plugin.keyword_mute(self.event(text="group bad"))
            await self.plugin._handle_join_request_event(
                "p", test_keywords.KeywordTests.application("yes no")
            )
        self.assertEqual(self.plugin._mute.await_args.args[3], 120)
        api.review_join_request.assert_awaited_once_with(
            "g", "applicant", "request", approve=True
        )

    def test_write_failure_rolls_back_live_settings_and_migration(self):
        before = deepcopy(dict(self.config))
        with patch.object(
            AstrBotConfig, "save_config", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                self.migrate()
        self.assertEqual(self.config, before)
        self.migrate()
        before = deepcopy(dict(self.config))
        with patch.object(
            AstrBotConfig, "save_config", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                self.store.set_value(
                    "p:GroupMessage:g", "keyword_settings", "mute_keywords", ["new"]
                )
        self.assertEqual(self.config, before)

    async def test_invalid_keyword_values_do_not_create_profiles(self):
        self.migrate()
        for name, action, value in [
            ("违禁词时长", "设置", "0"),
            ("违禁词时长", "设置", "31天"),
            ("白黑词优先级", "设置", "随机"),
            ("违禁词", "清空", ""),
            ("违禁词", "添加", ""),
        ]:
            await self.command(self.event(), name, action, value)
        self.assertEqual(self.store.data["groups"], [])

    def test_progressive_schema_and_word_list_pagination(self):
        meta = SCHEMA["group_management"]["items"]
        self.assertEqual(meta["global_settings"]["condition"], {"enabled": False})
        self.assertEqual(meta["groups"]["condition"], {"enabled": True})
        template = meta["groups"]["templates"]["group"]
        self.assertEqual(template["display_item"], "umo")
        self.assertIn("keyword_settings", template["items"])
        self.assertNotIn(
            "pending_retention_days",
            template["items"]["join_request_settings"]["items"],
        )
        page = word_page("违禁词", [f"word-{i}" for i in range(25)], 2)
        self.assertIn("word\\-10", page)
        self.assertNotIn("word\\-20", page)
        self.assertIn("上一页", page)
        self.assertIn("下一页", page)

    def test_old_join_words_remain_literal_after_migration_and_reload(self):
        words = [" a.b ", "C++", "[广告]", r"\d+", ""]
        self.config["keyword_settings"]["join_whitelist_words"] = words.copy()
        self.storage.set_group_feature_override(
            "p:GroupMessage:g", "enable_join_keyword_review", True
        )
        self.migrate()
        reloaded = AstrBotConfig(str(self.path), schema=SCHEMA)
        store = GroupConfig(reloaded)
        for profile in (
            store.data["global_settings"],
            store.profile("p:GroupMessage:g"),
        ):
            self.assertEqual(profile["keyword_settings"]["join_whitelist_words"], words)
            self.assertEqual(
                profile["keyword_settings"]["join_keyword_match_mode"], "包含匹配"
            )

    async def test_group_match_mode_is_saved_and_does_not_change_other_groups(self):
        self.migrate()
        await self.command(self.event(), "进群匹配模式", "设置", "正则匹配")
        reloaded = AstrBotConfig(str(self.path), schema=SCHEMA)
        self.plugin.config = reloaded
        self.assertEqual(
            self.plugin._event_setting(self.event(), "join_keyword_match_mode"),
            "正则匹配",
        )
        self.assertEqual(
            self.plugin._event_setting(
                self.event(group="other"), "join_keyword_match_mode"
            ),
            "包含匹配",
        )
        self.assertEqual(
            GroupConfig(reloaded).profile("p:GroupMessage:g")["keyword_settings"][
                "join_whitelist_words"
            ],
            ["old white"],
        )

    async def test_group_recall_switch_is_saved_independently(self):
        self.migrate()
        await self.command(self.event(), "违禁词撤回", "开启")
        reloaded = AstrBotConfig(str(self.path), schema=SCHEMA)
        profile = GroupConfig(reloaded).profile("p:GroupMessage:g")
        self.assertTrue(profile["keyword_settings"]["enable_keyword_recall"])
        self.assertFalse(
            self.plugin._event_feature_setting(
                self.event("other"), "enable_keyword_recall", False
            )
        )

    def test_dashboard_validator_accepts_migrated_and_chat_written_profiles(self):
        from astrbot.dashboard.services.config_service import validate_config

        self.migrate()
        self.store.set_value(
            "p:GroupMessage:g", "keyword_settings", "mute_keywords", ["new word"]
        )
        errors, checked = validate_config(
            deepcopy(dict(self.config)), SCHEMA, is_core=False
        )
        self.assertEqual(errors, [])
        self.assertEqual(checked["group_management"], self.config["group_management"])

    def test_dashboard_edit_is_used_after_framework_reload(self):
        self.migrate()
        self.store.set_value(
            "p:GroupMessage:g", "keyword_settings", "mute_keywords", ["chat word"]
        )
        edited = json.loads(self.path.read_text(encoding="utf-8-sig"))
        edited["group_management"]["groups"][0]["keyword_settings"]["mute_keywords"] = [
            "dashboard word"
        ]
        self.config.save_config(edited)
        self.plugin.config = AstrBotConfig(str(self.path), schema=SCHEMA)
        self.assertEqual(
            self.plugin._event_setting(self.event(), "mute_keywords"),
            ["dashboard word"],
        )

    def test_profiles_are_independent_and_duplicates_do_not_choose_arbitrarily(self):
        self.migrate()
        self.store.set_value(
            "p:GroupMessage:a", "keyword_settings", "mute_keywords", ["a"]
        )
        self.store.set_value(
            "p:GroupMessage:b", "keyword_settings", "mute_keywords", ["b"]
        )
        self.assertEqual(
            self.plugin._event_setting(self.event("a"), "mute_keywords"), ["a"]
        )
        self.assertEqual(
            self.plugin._event_setting(self.event("b"), "mute_keywords"), ["b"]
        )
        self.assertEqual(self.plugin._config("mute_keywords"), ["global bad"])
        self.store.data["groups"].append(deepcopy(self.store.data["groups"][0]))
        with self.assertRaisesRegex(ValueError, "UMO 重复"):
            self.store.profile("p:GroupMessage:a")


if __name__ == "__main__":
    unittest.main()
