from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import quote, urlencode

from botpy.http import Route


class QQBotRoute(Route):
    """Route using the QQ OpenAPI domain introduced on 2026-08-10."""

    DOMAIN = "api.bot.qq.com"
    SANDBOX_DOMAIN = "sandbox.api.bot.qq.com"


class QQGroupManageAPI:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        if query:
            clean_query = {k: v for k, v in query.items() if v not in (None, "")}
            if clean_query:
                path = f"{path}?{urlencode(clean_query)}"
        route = QQBotRoute(method, path)
        kwargs = {"json": payload} if payload is not None else {}
        return await self.client.api._http.request(route, **kwargs)

    async def mute_member(
        self,
        group_openid: str,
        member_openid: str,
        *,
        op: str,
        mute_expire_at: str = "",
    ) -> Any:
        member = {"op": op, "member_openid": member_openid}
        if mute_expire_at:
            member["mute_expire_at"] = mute_expire_at
        group_id = quote(group_openid, safe="")
        return await self._request(
            "POST",
            f"/v2/groups/{group_id}/restrict_chat_setting",
            payload={"members": [member]},
        )

    async def get_mute_status(self, group_openid: str) -> Any:
        group_id = quote(group_openid, safe="")
        return await self._request(
            "GET",
            f"/v2/groups/{group_id}/restrict_chat_setting",
        )

    async def list_join_requests(
        self,
        group_openid: str,
        *,
        cursor: str = "",
        limit: int = 20,
    ) -> Any:
        group_id = quote(group_openid, safe="")
        return await self._request(
            "GET",
            f"/v2/groups/{group_id}/join_request_list",
            query={"cursor": cursor, "limit": max(1, min(limit, 100))},
        )

    async def review_join_request(
        self,
        group_openid: str,
        member_openid: str,
        join_request_id: str,
        *,
        approve: bool,
        reject_reason: str = "",
    ) -> Any:
        group_id = quote(group_openid, safe="")
        member_id = quote(member_openid, safe="")
        payload: dict[str, Any] = {
            "op": "approve" if approve else "decline",
            "join_request_id": join_request_id,
        }
        if not approve and reject_reason:
            payload["reject_reason"] = reject_reason
        return await self._request(
            "POST",
            f"/v2/groups/{group_id}/approval_join_request/{member_id}",
            payload=payload,
        )

    async def send_group_text(
        self,
        group_openid: str,
        content: str,
        *,
        event_id: str = "",
    ) -> Any:
        group_id = quote(group_openid, safe="")
        payload: dict[str, Any] = {
            "content": content,
            "msg_type": 0,
            "msg_seq": secrets.randbelow(9999) + 1,
        }
        if event_id:
            payload["event_id"] = event_id
        return await self._request(
            "POST",
            f"/v2/groups/{group_id}/messages",
            payload=payload,
        )
    async def send_group_markdown(
        self,
        group_openid: str,
        content: str,
        *,
        event_id: str = "",
    ) -> Any:
        group_id = quote(group_openid, safe="")
        payload: dict[str, Any] = {
            "msg_type": 2,
            "markdown": {"content": content},
            "msg_seq": secrets.randbelow(9999) + 1,
        }
        if event_id:
            payload["event_id"] = event_id
        return await self._request(
            "POST",
            f"/v2/groups/{group_id}/messages",
            payload=payload,
        )
