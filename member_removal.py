"""手动指令、验证、关键词和 LLM 共用的成员移除执行器。"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

from astrbot.api import logger

from .api import QQGroupManageAPI
from .durations import parse_duration
from .kick_notices import member_avatar


class RemovalError(RuntimeError):
    """保留移出结果状态，明确失败时才允许回退禁言。"""

    def __init__(self, message: str, member: dict, *, confirmed: bool = False) -> None:
        super().__init__(message)
        self.member = member
        self.confirmed = confirmed


class MemberRemover:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._inflight: set[tuple[str, str, str]] = set()
        self._requests: deque[float] = deque()
        self._rate_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    async def remove(
        self,
        platform_id: str,
        group: str,
        member: str,
        operation_id: str,
        *,
        verification_locked: bool = False,
        actor_id: str = "",
        can_execute: Callable[[], bool] | None = None,
        name_hint: str = "",
        fallback_key: str = "",
    ) -> dict:
        """每次操作使用独立任务，卸载时可等待全部请求及其取消清理结束。"""
        if getattr(self.plugin, "_terminating", False):
            raise RuntimeError("插件正在重载，请稍后重试")
        task = asyncio.create_task(
            self._run(
                platform_id,
                group,
                member,
                operation_id,
                verification_locked=verification_locked,
                actor_id=actor_id,
                can_execute=can_execute,
                name_hint=name_hint,
                fallback_key=fallback_key,
            )
        )
        self._tasks.add(task)
        try:
            return await task
        finally:
            self._tasks.discard(task)

    async def close(self) -> None:
        """等待移除任务退出，避免重载后的旧实例继续保存存储快照。"""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(
        self,
        platform_id: str,
        group: str,
        member: str,
        operation_id: str,
        *,
        verification_locked: bool,
        actor_id: str,
        can_execute: Callable[[], bool] | None,
        name_hint: str,
        fallback_key: str,
    ) -> dict:
        """每名成员单独调用，便于准确汇总多人操作的结果。"""
        if not member or member in {"all", "qq_official"}:
            raise ValueError("请选择明确的群成员")
        key = (platform_id, group, member)
        if key in self._inflight:
            raise ValueError("该成员正在移出处理中")
        self._inflight.add(key)
        try:
            verifier = getattr(self.plugin, "verifier", None)
            lock = (
                verifier._lock(key)
                if verifier is not None and not verification_locked
                else nullcontext()
            )
            async with lock:
                try:
                    return await self._remove(
                        platform_id,
                        group,
                        member,
                        operation_id,
                        actor_id,
                        can_execute,
                        name_hint,
                    )
                except RemovalError as exc:
                    if not fallback_key or not exc.confirmed:
                        raise
                    umo = self.plugin._group_umo(platform_id, group)
                    if not await self.plugin._sdk_group_enabled(umo, actor_id=actor_id):
                        raise
                    # 成员已经离群或授权已撤销时，结束本次回退处理。
                    if (
                        getattr(self.plugin, "_terminating", False)
                        or (can_execute and not can_execute())
                        or self.plugin.storage.removal_record(
                            platform_id, group, member, operation_id
                        ).get("departure_seen")
                    ):
                        raise
                    result = {**exc.member, "error": str(exc)}
                    try:
                        duration = str(
                            self.plugin._group_setting(
                                fallback_key,
                                umo,
                                "1天"
                                if fallback_key == "verify_fail_mute_duration"
                                else "10分",
                            )
                        )
                        seconds = parse_duration(duration)
                        self.plugin._validate_duration(seconds, umo)
                        if seconds <= 0:
                            raise ValueError("回退禁言时长必须大于 0")
                        platform = self.plugin.context.get_platform_inst(platform_id)
                        result.update(status="mute_unknown", duration=duration)
                        # 发出禁言前保存恢复依据，重载后仍可通过验证放行解除禁言。
                        self.plugin.storage.put_removal(
                            platform_id,
                            group,
                            member,
                            operation_id,
                            "failed",
                            fallback_result={**result, "mute_attempted": True},
                        )
                        result["mute_attempted"] = True
                        await asyncio.wait_for(
                            self.plugin._mute_on_platform(
                                platform, group, member, seconds
                            ),
                            10,
                        )
                        result.update(status="muted", duration=duration)
                    except Exception as mute_error:
                        result.update(status="mute_failed", mute_error=str(mute_error))
                    try:
                        self.plugin.storage.put_removal(
                            platform_id,
                            group,
                            member,
                            operation_id,
                            "failed",
                            fallback_result=result,
                        )
                    except OSError:
                        logger.exception("[群管] 回退处理已结束，但保存结果失败")
                    return result
        finally:
            self._inflight.discard(key)

    async def _remove(
        self,
        platform_id: str,
        group: str,
        member: str,
        operation_id: str,
        actor_id: str,
        can_execute: Callable[[], bool] | None,
        name_hint: str,
    ) -> dict:
        storage = self.plugin.storage
        previous = storage.removal_result(platform_id, group, member, operation_id)
        if previous == "success":
            saved = storage.removal_record(platform_id, group, member, operation_id)
            return {
                **saved,
                "status": "success",
                "name": saved.get("name", name_hint or member),
            }
        if previous == "unknown":
            raise RemovalError(
                "上次移出请求结果待确认，请核对群成员状态",
                {"name": name_hint or member, "status": "unknown"},
            )
        if previous == "failed":
            saved = storage.removal_record(platform_id, group, member, operation_id)
            if saved.get("fallback_result"):
                return saved["fallback_result"]
            raise RemovalError(
                "上次移出请求已失败，请重新发送指令",
                {"name": name_hint or member, "status": "failed"},
            )
        # 全插件共用滚动窗口，单成员请求也计入官方的 30 QPM 限制。
        async with self._rate_lock:
            now = time.monotonic()
            while self._requests and self._requests[0] <= now - 60:
                self._requests.popleft()
            if len(self._requests) >= 30:
                snapshot = {"name": name_hint or member, "status": "failed"}
                storage.put_removal(
                    platform_id,
                    group,
                    member,
                    operation_id,
                    "failed",
                    name=snapshot["name"],
                )
                raise RemovalError(
                    "移出成员请求已达 30 次/分钟，请稍后重试", snapshot, confirmed=True
                )
            umo = self.plugin._group_umo(platform_id, group)
            if not await self.plugin._sdk_group_enabled(umo, actor_id=actor_id):
                raise ValueError("当前群未启用本插件")
            platform = self.plugin.context.get_platform_inst(platform_id)
            if platform is None or not hasattr(platform, "client"):
                raise RuntimeError("找不到当前 QQ 官方平台实例")
            if getattr(self.plugin, "_terminating", False):
                raise RuntimeError("插件正在重载，请稍后重试")
            # 等待验证锁和会话检查期间，授权或规则可能已经被撤销。
            if can_execute is not None and not can_execute():
                raise ValueError("当前踢人权限或功能规则已关闭")
            snapshot = {
                "name": name_hint or member,
                "avatar": member_avatar(
                    str(getattr(platform, "appid", "") or ""), member
                ),
            }
            storage.put_removal(
                platform_id,
                group,
                member,
                operation_id,
                "unknown",
                phase="preparing",
                **snapshot,
            )
            # 昵称在执行移出前读取，接口未开通时沿用事件中的用户昵称。
            try:
                info = await asyncio.wait_for(
                    QQGroupManageAPI(platform.client).get_group_member_info(
                        group, member
                    ),
                    3,
                )
                if (
                    isinstance(info, dict)
                    and isinstance(info.get("username"), str)
                    and info["username"].strip()
                ):
                    snapshot["name"] = info["username"].strip()
            except Exception:
                logger.debug("[群管] 成员昵称查询不可用，使用当前事件信息")
            enabled = await self.plugin._sdk_group_enabled(umo, actor_id=actor_id)
            if storage.removal_record(platform_id, group, member, operation_id).get(
                "departure_seen"
            ):
                storage.put_removal(
                    platform_id, group, member, operation_id, "failed", **snapshot
                )
                raise RemovalError(
                    "成员已离群，本次操作已结束", {**snapshot, "status": "failed"}
                )
            if (
                not enabled
                or getattr(self.plugin, "_terminating", False)
                or (can_execute is not None and not can_execute())
            ):
                storage.put_removal(
                    platform_id, group, member, operation_id, "failed", **snapshot
                )
                raise RemovalError(
                    "当前踢人权限或功能规则已关闭", {**snapshot, "status": "failed"}
                )
            # 请求前落盘，进程中断或断线后，同一触发来源不会再次提交踢人。
            storage.put_removal(
                platform_id,
                group,
                member,
                operation_id,
                "unknown",
                phase="submitted",
                **snapshot,
            )
            self._requests.append(time.monotonic())
        try:
            result = await asyncio.wait_for(
                QQGroupManageAPI(platform.client).remove_members(group, [member]), 10
            )
        except Exception as exc:
            messages = {
                "40103012": "请群主为机器人打开“移除群成员”权限",
                "40103011": "请在机器人 WEB 管理端打开“移除群成员”开关",
                "11253": "当前机器人没有此接口的访问权限",
            }
            message = messages.get(str(getattr(exc, "qq_code", "")))
            confirmed = bool(message) or 400 <= getattr(exc, "http_status", 0) < 500
            if confirmed:
                storage.put_removal(platform_id, group, member, operation_id, "failed")
                raise RemovalError(
                    message or str(exc),
                    {**snapshot, "status": "failed"},
                    confirmed=True,
                ) from exc
            raise RemovalError(
                f"移出请求结果待确认，请核对群成员状态（{exc}）",
                {**snapshot, "status": "unknown"},
            ) from exc
        if (
            not isinstance(result, dict)
            or result.get("remove_members_result") != "success"
        ):
            raise RemovalError(
                "移出接口未返回成功结果，请核对群成员状态",
                {**snapshot, "status": "unknown"},
            )
        # QQ 已执行成功，写盘失败仍保留内存中的完成标记，避免本进程重复提交。
        try:
            storage.put_removal(platform_id, group, member, operation_id, "success")
        except OSError:
            logger.exception("[群管] 移出成功，但保存操作结果失败")
        try:
            storage.remove_verification(platform_id, group, member)
        except OSError:
            logger.exception("[群管] 移出成功，但保存验证记录清理结果失败")
        logger.info(
            "[群管] 已移出成员，平台=%s 群=%s 成员=%s 来源=%s",
            platform_id,
            group,
            member,
            operation_id,
        )
        return {**snapshot, "status": "success"}
