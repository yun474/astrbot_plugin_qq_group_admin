import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_qq_group_admin.main import (
    extract_mute_duration,
    format_group_admin_help,
    format_mute_status,
    format_request,
    parse_duration,
    render_member_notice,
)
from astrbot_plugin_qq_group_admin.api import QQBotRoute
from astrbot_plugin_qq_group_admin.storage import PluginStorage


class CoreTests(unittest.TestCase):
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
            extract_mute_duration("禁言 <@member-1>", "<@member-1>", "20分"),
            "20分",
        )

    def test_explicit_duration_overrides_default(self) -> None:
        self.assertEqual(
            extract_mute_duration("禁言 <@member-1> 2小时", "", "20分"),
            "2小时",
        )

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
