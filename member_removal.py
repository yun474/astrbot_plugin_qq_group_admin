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
    ) -> None:
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
            )
        )
        self._tasks.add(task)
        try:
            await task
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
    ) -> None:
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
                await self._remove(
                    platform_id, group, member, operation_id, actor_id, can_execute
                )
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
    ) -> None:
        storage = self.plugin.storage
        previous = storage.removal_result(platform_id, group, member, operation_id)
        if previous == "success":
            return
        if previous == "unknown":
            raise RuntimeError("上次移出请求结果待确认，请核对群成员状态")
        if previous == "failed":
            raise RuntimeError("上次移出请求已失败，请重新发送指令")
        # 全插件共用滚动窗口，单成员请求也计入官方的 30 QPM 限制。
        async with self._rate_lock:
            now = time.monotonic()
            while self._requests and self._requests[0] <= now - 60:
                self._requests.popleft()
            if len(self._requests) >= 30:
                raise RuntimeError("移出成员请求已达 30 次/分钟，请稍后重试")
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
            # 请求前落盘，进程中断或断线后，同一触发来源不会再次提交踢人。
            storage.put_removal(platform_id, group, member, operation_id, "unknown")
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
            if message:
                storage.put_removal(platform_id, group, member, operation_id, "failed")
                raise RuntimeError(message) from exc
            raise RuntimeError(
                f"移出请求结果待确认，请核对群成员状态（{exc}）"
            ) from exc
        if (
            not isinstance(result, dict)
            or result.get("remove_members_result") != "success"
        ):
            raise RuntimeError("移出接口未返回成功结果，请核对群成员状态")
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
