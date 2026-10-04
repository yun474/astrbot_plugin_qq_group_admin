import asyncio
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from astrbot_plugin_qq_group_admin.storage import PluginStorage
import test_keywords


class HotReloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        test_keywords.KeywordTests.setUp(self)
        self.plugin._patched = {}
        self.plugin._patch_task = None
        self.plugin._sdk_event_tasks = set()
        self.plugin._terminating = False
        self.plugin._parser_state_class = None
        self.plugin._owned_parser_methods = {}
        self.client = NS(_connection=NS(parser={}))
        self.platform = NS(
            client=self.client, meta=lambda: NS(name="qq_official_webhook", id="p")
        )
        self.plugin.context.platform_manager = NS(platform_insts=[self.platform])
        self.plugin.context.get_platform_inst.return_value = self.platform
        self.addAsyncCleanup(self.plugin.terminate)

    async def test_unload_waits_for_send_cleanup_before_new_storage_is_loaded(self):
        entered, cancelling, release_cleanup = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )

        async def send(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelling.set()
                await release_cleanup.wait()
                raise

        self.api.send_group_markdown.side_effect = send
        self.plugin.storage.add_group_admin("g", "revoked-admin")
        await self.plugin._patch_platforms_once()
        dispatch = asyncio.create_task(
            self.client.on_group_join_request(
                test_keywords.KeywordTests.application("路过")
            )
        )
        await asyncio.wait_for(entered.wait(), 1)
        unloading = asyncio.create_task(self.plugin.terminate())
        try:
            await asyncio.wait_for(cancelling.wait(), 1)
            self.assertFalse(unloading.done())
        finally:
            release_cleanup.set()
            await asyncio.wait_for(unloading, 1)
            await asyncio.gather(dispatch, return_exceptions=True)
        self.assertTrue(dispatch.cancelled())
        self.assertFalse(self.plugin._sdk_event_tasks)
        new_storage = PluginStorage(self.plugin.storage.path)
        self.assertEqual(new_storage.data["pending"], {})
        new_storage.remove_group_admin("g", "revoked-admin")
        await asyncio.sleep(0)
        self.assertEqual(PluginStorage(new_storage.path).group_admins("g"), [])
        self.api.send_group_text.assert_not_awaited()

    async def test_unload_cancels_all_four_sdk_event_types(self):
        entered = asyncio.Event()
        count = 0

        async def block(*args):
            nonlocal count
            count += 1
            if count == 4:
                entered.set()
            await asyncio.Future()

        with (
            patch.object(self.plugin, "_handle_join_request_event", side_effect=block),
            patch.object(self.plugin, "_handle_member_event", side_effect=block),
            patch.object(self.plugin, "_handle_review_interaction", side_effect=block),
        ):
            await self.plugin._patch_platforms_once()
            tasks = [
                asyncio.create_task(getattr(self.client, name)({}))
                for name in (
                    "on_group_join_request",
                    "on_group_member_add",
                    "on_group_member_remove",
                    "on_interaction_create",
                )
            ]
            await asyncio.wait_for(entered.wait(), 1)
            await self.plugin.terminate()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(all(task.cancelled() for task in tasks))
        self.assertFalse(self.plugin._sdk_event_tasks)

    async def test_queued_old_dispatch_and_late_hooks_cannot_restart_plugin_work(self):
        with patch.object(self.plugin, "_handle_join_request_event") as handler:
            await self.plugin._patch_platforms_once()
            old_handler = self.client.on_group_join_request
            await self.plugin.terminate()
            await old_handler({})
            await self.plugin.on_plugin_loaded(None)
            handler.assert_not_awaited()
        self.assertFalse(self.plugin._patched)
        self.assertIsNone(self.plugin._patch_task)
        self.assertFalse(hasattr(self.client, "on_group_join_request"))

    async def test_original_handler_work_is_not_cancelled_with_our_plugin(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def original(event):
            entered.set()
            await release.wait()

        self.client.on_interaction_create = original
        with patch.object(
            self.plugin, "_handle_review_interaction", return_value=False
        ):
            await self.plugin._patch_platforms_once()
            dispatch = asyncio.create_task(self.client.on_interaction_create({}))
            await asyncio.wait_for(entered.wait(), 1)
            try:
                await self.plugin.terminate()
                self.assertFalse(dispatch.done())
                self.assertIs(self.client.on_interaction_create, original)
            finally:
                release.set()
                await asyncio.wait_for(dispatch, 1)

    async def test_suspended_platform_hook_does_not_reinstall_parsers_after_unload(
        self,
    ):
        entered, release = asyncio.Event(), asyncio.Event()

        async def reconnect(*args):
            entered.set()
            await release.wait()

        self.platform.meta = lambda: NS(name="qq_official", id="p")
        with patch.object(self.plugin, "_ensure_group_member_intent", reconnect):
            hook = asyncio.create_task(self.plugin.on_platform_loaded())
            await asyncio.wait_for(entered.wait(), 1)
            try:
                await self.plugin.terminate()
            finally:
                release.set()
                await asyncio.wait_for(hook, 1)
        self.assertFalse(self.plugin._patched)
        self.assertEqual(self.client._connection.parser, {})

    async def test_failed_patch_task_does_not_skip_unload_cleanup(self):
        await self.plugin._patch_platforms_once()
        self.plugin._patch_task = asyncio.create_task(
            AsyncMock(side_effect=RuntimeError("patch failed"))()
        )
        await asyncio.sleep(0)
        await self.plugin.terminate()
        self.assertFalse(self.plugin._patched)
        self.assertEqual(self.client._connection.parser, {})
        self.assertIsNone(self.plugin._patch_task)
