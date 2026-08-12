import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_qq_group_admin.main import (
    format_request,
    parse_duration,
    render_member_notice,
)
from astrbot_plugin_qq_group_admin.api import QQBotRoute
from astrbot_plugin_qq_group_admin.storage import PluginStorage


class CoreTests(unittest.TestCase):
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
