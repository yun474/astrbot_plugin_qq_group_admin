import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_qq_group_admin.storage import PluginStorage


class StorageRecoveryTests(unittest.TestCase):
    def test_failed_admin_batch_leaves_live_and_persisted_permissions_unchanged(self):
        for add in (False, True):
            for existing_group in (False, True):
                with (
                    self.subTest(add=add, existing_group=existing_group),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    path = Path(directory) / "state.json"
                    storage = PluginStorage(path)
                    if existing_group:
                        storage.update_group_admins("g", ["a", "b"], add=True)
                    storage.save()
                    before = json.loads(path.read_text(encoding="utf-8"))
                    with patch.object(
                        Path, "replace", side_effect=OSError("disk full")
                    ):
                        if add or existing_group:
                            with self.assertRaises(OSError):
                                storage.update_group_admins(
                                    "g", ["a", "b", "c"], add=add
                                )
                        else:
                            self.assertEqual(
                                storage.update_group_admins("g", ["a"], add=False), 0
                            )
                    self.assertEqual(storage.data, before)
                    self.assertEqual(PluginStorage(path).data, before)

    def test_completed_markers_are_scoped_persisted_and_expire(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            storage = PluginStorage(path, retention_days=30)
            item = {"platform_id": "p", "group_openid": "g", "join_request_id": "r"}
            storage.put_pending("notice", item)
            storage.put_pending("duplicate", item)
            storage.remove_reviewed_request("p", "g", "r")
            reloaded = PluginStorage(path)
            self.assertEqual(reloaded.data["pending"], {})
            self.assertTrue(reloaded.is_reviewed("p", "g", "r"))
            for key in (("other", "g", "r"), ("p", "other", "r"), ("p", "g", "new")):
                self.assertFalse(reloaded.is_reviewed(*key))
            reloaded.data["reviewed"][json.dumps(["p", "g", "r"])] = (
                datetime.now(timezone.utc) - timedelta(days=31)
            ).isoformat()
            reloaded.prune()
            self.assertFalse(PluginStorage(path).is_reviewed("p", "g", "r"))

    def test_corrupt_files_are_preserved_before_new_writes(self):
        for content in (
            b'{"group_admins":',
            b"\xff",
            b"[]",
            b'{"pending":null}',
            b'{"pending":{"notice":null}}',
            b'{"group_admins":{"g":"admin"}}',
            b'{"group_feature_overrides":{"g":false}}',
            b'{"pending":{"notice":{"ref_idx":[]}}}',
            b'{"pending":{"notice":{"review_callbacks":null}}}',
        ):
            with (
                self.subTest(content=content),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "state.json"
                path.write_bytes(content)
                storage = PluginStorage(path)
                backups = list(path.parent.glob("state.corrupt-*.json"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_bytes(), content)
                self.assertEqual(storage.group_admins("g"), [])
                storage.add_group_admin("g", "new-admin")
                self.assertEqual(PluginStorage(path).group_admins("g"), ["new-admin"])
                self.assertEqual(backups[0].read_bytes(), content)

    def test_read_failure_does_not_replace_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            content = b'{"group_admins":{"g":["admin"]}}'
            path.write_bytes(content)
            with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(OSError, "原文件未修改"):
                    PluginStorage(path)
            self.assertEqual(path.read_bytes(), content)

    def test_failed_preservation_aborts_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_bytes(b"invalid json")
            with patch.object(Path, "replace", side_effect=PermissionError("denied")):
                with self.assertRaises(PermissionError):
                    PluginStorage(path)
            self.assertEqual(path.read_bytes(), b"invalid json")

    def test_valid_legacy_state_keeps_admins_and_defaults_missing_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"group_admins": {"g": ["admin"]}}))
            storage = PluginStorage(path)
            self.assertEqual(storage.group_admins("g"), ["admin"])
            self.assertEqual(storage.data["pending"], {})
            self.assertIsNone(
                storage.group_feature_override("g", "enable_mute_command")
            )
            self.assertEqual(list(path.parent.glob("state.corrupt-*.json")), [])
