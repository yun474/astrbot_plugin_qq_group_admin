import unittest
from unittest.mock import patch

from astrbot_plugin_qq_group_admin.callback_guard import ReviewCallbackGuard


class CallbackGuardTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        clock = patch(
            "astrbot_plugin_qq_group_admin.callback_guard.monotonic",
            side_effect=lambda: self.now,
        )
        clock.start()
        self.addCleanup(clock.stop)
        self.guard = ReviewCallbackGuard()

    def test_cooldown_spans_groups_but_denial_is_scoped_to_group_and_platform(self):
        self.assertIsNone(self.guard.check_click("p", "g", "u", assigned_admin=False))
        self.guard.remember_denied("p", "g", "u")
        self.assertEqual(self.guard.check_click("p", "g", "u", assigned_admin=False), 4)
        self.assertEqual(
            self.guard.check_click("p", "g2", "u", assigned_admin=False), 2
        )
        self.assertIsNone(self.guard.check_click("p2", "g", "u", assigned_admin=False))
        self.now += 2
        self.assertIsNone(self.guard.check_click("p", "g2", "u", assigned_admin=False))
        self.assertEqual(self.guard.check_click("p", "g", "u", assigned_admin=False), 4)

    def test_many_users_do_not_grow_caches_without_bound(self):
        for i in range(3000):
            self.guard.check_click("p", "g", str(i), assigned_admin=False)
            self.guard.remember_denied("p", "g", str(i))
        self.assertLessEqual(len(self.guard._cooldowns), 2048)
        self.assertLessEqual(len(self.guard._denied), 2048)
        self.now += 30
        self.guard.check_click("p", "g", "new-user", assigned_admin=False)
        self.assertEqual(len(self.guard._cooldowns), 1)
        self.assertFalse(self.guard._denied)

    def test_lookup_budget_uses_a_rolling_window_and_recovers(self):
        for _ in range(10):
            self.assertTrue(self.guard.start_lookup())
            self.guard.finish_lookup()
        self.now += 30
        for _ in range(10):
            self.assertTrue(self.guard.start_lookup())
            self.guard.finish_lookup()
        self.assertFalse(self.guard.start_lookup())
        self.now += 29
        self.assertFalse(self.guard.start_lookup())
        self.now += 1
        for _ in range(10):
            self.assertTrue(self.guard.start_lookup())
            self.guard.finish_lookup()
        self.assertFalse(self.guard.start_lookup())


if __name__ == "__main__":
    unittest.main()
