import asyncio
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from astrbot_plugin_qq_group_admin.api import QQGroupManageAPI
from astrbot_plugin_qq_group_admin.storage import PluginStorage
from botpy.errors import ServerError
from http_fakes import make_http
import test_keywords


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_failures_propagate_without_retry(self):
        for error in (TimeoutError(), ConnectionResetError(), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                http = make_http(error=error)
                api = QQGroupManageAPI(NS(api=NS(_http=http)))
                with self.assertRaises(type(error)):
                    await api.review_join_request("g", "u", "r", approve=True)
                http._session.request.assert_called_once()

    async def test_empty_success_response_is_valid(self):
        for status in (200, 204):
            with self.subTest(status=status):
                http = make_http(status=status, empty=True)
                api = QQGroupManageAPI(NS(api=NS(_http=http)))
                self.assertIsNone(await api.recall_group_message("g", "message"))

    async def test_http_failure_raises_even_when_body_is_empty(self):
        http = make_http(status=503, empty=True)
        with self.assertRaises(ServerError):
            await QQGroupManageAPI(NS(api=NS(_http=http))).get_mute_status("g")

    async def test_authenticated_session_sandbox_and_query_are_preserved(self):
        http = make_http(data={"list": []}, sandbox=True)
        api = QQGroupManageAPI(NS(api=NS(_http=http)))
        self.assertEqual(
            await api.list_join_requests("g/1", cursor="a+b", limit=12), {"list": []}
        )
        http.check_session.assert_awaited_once()
        kwargs = http._session.request.call_args.kwargs
        self.assertEqual(
            kwargs["url"],
            "https://sandbox.api.bot.qq.com/v2/groups/g%2F1/join_request_list?cursor=a%2Bb&limit=12",
        )
        self.assertEqual(kwargs["headers"], http._headers)
        self.assertEqual(kwargs["timeout"].total, 5)


class JoinDeliveryTests(unittest.IsolatedAsyncioTestCase):
    event = staticmethod(test_keywords.KeywordTests.event)
    application = staticmethod(test_keywords.KeywordTests.application)

    def setUp(self):
        test_keywords.KeywordTests.setUp(self)

    async def test_real_transport_timeout_keeps_application_pending_for_manual_review(
        self,
    ):
        http = make_http(error=TimeoutError())
        api = QQGroupManageAPI(NS(api=NS(_http=http)))
        api.send_group_markdown = AsyncMock(return_value={"id": "manual-card"})
        with patch(
            "astrbot_plugin_qq_group_admin.main.QQGroupManageAPI", return_value=api
        ):
            await self.plugin._handle_join_request_event("p", self.application("同好"))
        self.assertFalse(self.plugin.storage.is_reviewed("p", "g", "request"))
        self.assertIsNotNone(self.plugin.storage.get_pending("manual-card"))
        self.assertFalse(self.plugin._reviews_inflight)
        api.send_group_markdown.assert_awaited_once()

    async def test_duplicate_delivery_keeps_one_card_and_both_review_indexes_after_reload(
        self,
    ):
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"id": "first-card"}

        self.api.send_group_markdown.side_effect = send
        item = self.application("路过")
        task = asyncio.create_task(self.plugin._handle_join_request_event("p", item))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await self.plugin._handle_join_request_event("p", item)
        finally:
            release.set()
            await task
        self.plugin.storage = PluginStorage(self.plugin.storage.path)
        await self.plugin._handle_join_request_event("p", item)
        self.api.send_group_markdown.assert_awaited_once()
        button = self.api.send_group_markdown.await_args.kwargs["keyboard"]["content"][
            "rows"
        ][0]["buttons"][0]
        token = button["action"]["data"].split(":")[1]
        by_token = self.plugin.storage.find_pending_by_token(token)
        by_quote = self.plugin.storage.find_pending_by_quote({"first-card"}, "p", "g")
        self.assertIsNotNone(by_token)
        self.assertEqual(by_token, by_quote)

    async def test_failed_or_cancelled_send_allows_later_delivery(self):
        for error in (RuntimeError("send failed"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                item = self.application("路过")
                item["join_request_id"] = type(error).__name__
                self.api.send_group_markdown.side_effect = error
                self.api.send_group_text.side_effect = RuntimeError("fallback failed")
                if isinstance(error, asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        await self.plugin._handle_join_request_event("p", item)
                else:
                    await self.plugin._handle_join_request_event("p", item)
                self.assertIsNone(
                    self.plugin.storage.find_pending_by_join_request_id(
                        item["join_request_id"], "g", "p"
                    )
                )
                self.api.send_group_markdown.side_effect = None
                self.api.send_group_markdown.return_value = {
                    "id": item["join_request_id"]
                }
                await self.plugin._handle_join_request_event("p", item)
                self.assertIsNotNone(
                    self.plugin.storage.get_pending(item["join_request_id"])
                )

    async def test_failed_reservation_does_not_block_retry(self):
        item = self.application("路过")
        with patch.object(
            self.plugin.storage, "save", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                await self.plugin._handle_join_request_event("p", item)
        self.assertEqual(self.plugin.storage.data["pending"], {})
        self.api.send_group_markdown.assert_not_awaited()
        await self.plugin._handle_join_request_event("p", item)
        self.api.send_group_markdown.assert_awaited_once()
