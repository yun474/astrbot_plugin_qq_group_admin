from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.message_components import At, Reply
from astrbot.api.star import Context, Star, StarTools, register

from .api import QQGroupManageAPI
from .storage import PluginStorage


PLUGIN_NAME = "astrbot_plugin_qq_group_admin"
QQ_PLATFORMS = {"qq_official", "qq_official_webhook"}
ACTION_RE = re.compile(r"^(同意|通过|拒绝|驳回)(?:\s+(.+))?$", re.S)
TIME_PART_RE = re.compile(r"(\d+)\s*(天|日|小时|时|分钟|分|秒|s|m|h|d)", re.I)


def parse_duration(value: str) -> int:
    text = value.strip().lower()
    if text in {"0", "解除", "解禁", "取消"}:
        return 0
    if text.isdigit():
        return int(text) * 60
    units = {
        "天": 86400, "日": 86400, "d": 86400,
        "小时": 3600, "时": 3600, "h": 3600,
        "分钟": 60, "分": 60, "m": 60,
        "秒": 1, "s": 1,
    }
    matches = list(TIME_PART_RE.finditer(text))
    if not matches or "".join(match.group(0) for match in matches).replace(" ", "") != text.replace(" ", ""):
        raise ValueError("时间格式错误，可用 30秒、10分、2小时、1天2小时；纯数字按分钟")
    return sum(int(match.group(1)) * units[match.group(2).lower()] for match in matches)


def format_request(item: dict[str, Any], index: int | None = None) -> str:
    prefix = f"#{index} " if index is not None else ""
    lines = [
        f"{prefix}新的入群申请",
        f"昵称：{item.get('username') or '未提供'}",
        f"成员 OpenID：{item.get('member_openid') or '未提供'}",
        f"申请 ID：{item.get('join_request_id') or '未提供'}",
        f"申请时间：{item.get('apply_at') or '未提供'}",
        f"来源：{item.get('apply_source') or '未提供'}",
    ]
    if item.get("invited_by"):
        lines.append(f"邀请人：{item['invited_by']}")
    if item.get("risk_tips"):
        lines.append(f"风险提示：{item['risk_tips']}")
    verify = item.get("verify_info") or {}
    if verify.get("method"):
        lines.append(f"验证方式：{verify['method']}")
    if verify.get("verify_message"):
        lines.append(f"验证消息：{verify['verify_message']}")
    qa_list = verify.get("review_qa_list") or []
    for qa_index, qa in enumerate(qa_list, 1):
        lines.append(
            f"问答 {qa_index}：{qa.get('question') or '（无问题）'} / {qa.get('answer') or '（未回答）'}"
        )
    if item.get("auto_approved"):
        lines.append(f"自动审批策略：{item['auto_approved'].get('strategy_id') or '已自动通过'}")
    return "\n".join(lines)


@register(
    PLUGIN_NAME,
    "yun474",
    "QQ 官方机器人群管理：禁言、入群申请审批、分群管理员与 LLM 工具",
    "2.1.0",
)
class QQGroupAdminPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.storage = PluginStorage(
            data_dir / "state.json",
            int(config.get("pending_retention_days", 30)),
        )
        self._patched: dict[str, dict[str, Any]] = {}
        self._patch_task: asyncio.Task | None = None

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        self._patch_task = asyncio.create_task(self._patch_platforms_until_ready())

    @filter.on_platform_loaded()
    async def on_platform_loaded(self) -> None:
        await self._patch_platforms_once()

    async def _patch_platforms_until_ready(self) -> None:
        for _ in range(60):
            await self._patch_platforms_once()
            await asyncio.sleep(2)

    async def _patch_platforms_once(self) -> None:
        for platform in self.context.platform_manager.platform_insts:
            try:
                meta = platform.meta()
            except Exception:
                continue
            if meta.name not in QQ_PLATFORMS:
                continue
            client = getattr(platform, "client", None)
            if client is None:
                continue
            platform_id = meta.id
            if platform_id not in self._patched:
                old_handlers = {
                    name: getattr(client, name, None)
                    for name in (
                        "on_group_join_request",
                        "on_group_member_add",
                        "on_group_member_remove",
                    )
                }

                async def handler(data: dict[str, Any], pid: str = platform_id) -> None:
                    await self._handle_join_request_event(pid, data)

                async def member_add_handler(
                    data: dict[str, Any],
                    pid: str = platform_id,
                    original: Any = old_handlers["on_group_member_add"],
                ) -> None:
                    await self._handle_member_event(pid, "member_join", data)
                    if original is not None:
                        result = original(data)
                        if hasattr(result, "__await__"):
                            await result

                async def member_remove_handler(
                    data: dict[str, Any],
                    pid: str = platform_id,
                    original: Any = old_handlers["on_group_member_remove"],
                ) -> None:
                    await self._handle_member_event(pid, "member_leave", data)
                    if original is not None:
                        result = original(data)
                        if hasattr(result, "__await__"):
                            await result

                setattr(client, "on_group_join_request", handler)
                setattr(client, "on_group_member_add", member_add_handler)
                setattr(client, "on_group_member_remove", member_remove_handler)
                if meta.name == "qq_official":
                    intents = getattr(client, "intents", 0)
                    if isinstance(intents, int) and not intents & (1 << 24):
                        client.intents = intents | (1 << 24)
                        logger.info("[%s] 已启用 GROUP_MEMBER Intents（1 << 24）", PLUGIN_NAME)
                self._patched[platform_id] = {
                    "client": client,
                    "old_handlers": old_handlers,
                    "connections": set(),
                }
            patch_state = self._patched[platform_id]
            connections = [getattr(client, "_connection", None)]
            webhook_helper = getattr(platform, "webhook_helper", None)
            connections.append(getattr(webhook_helper, "_connection", None))
            for connection in connections:
                if connection is None or id(connection) in patch_state["connections"]:
                    continue

                def join_request_parser(payload: dict[str, Any], c: Any = client) -> None:
                    data = dict(payload.get("d", {}) or {})
                    data["_event_id"] = str(payload.get("id") or "")
                    c.ws_dispatch("group_join_request", data)

                def member_add_parser(payload: dict[str, Any], c: Any = client) -> None:
                    data = dict(payload.get("d", {}) or {})
                    data["_event_id"] = str(payload.get("id") or "")
                    c.ws_dispatch("group_member_add", data)

                def member_remove_parser(payload: dict[str, Any], c: Any = client) -> None:
                    data = dict(payload.get("d", {}) or {})
                    data["_event_id"] = str(payload.get("id") or "")
                    c.ws_dispatch("group_member_remove", data)

                connection.parser["group_join_request"] = join_request_parser
                connection.parser["group_member_add"] = member_add_parser
                connection.parser["group_member_remove"] = member_remove_parser
                patch_state["connections"].add(id(connection))
                logger.info(
                    "[%s] 已接入 QQ 入群申请与成员进退群事件", PLUGIN_NAME
                )

    async def _handle_member_event(
        self,
        platform_id: str,
        notice_type: str,
        item: dict[str, Any],
    ) -> None:
        enabled_key = (
            "enable_member_join_notice"
            if notice_type == "member_join"
            else "enable_member_leave_notice"
        )
        if not self.config.get(enabled_key, True):
            return
        group_openid = str(item.get("group_openid") or "")
        member_openid = str(item.get("member_openid") or "")
        if not group_openid:
            logger.warning("[%s] 成员事件缺少 group_openid: %r", PLUGIN_NAME, item)
            return
        template_key = (
            "member_join_message"
            if notice_type == "member_join"
            else "member_leave_message"
        )
        default = (
            "欢迎 {member_at} 加入群聊！"
            if notice_type == "member_join"
            else "有成员退出了群聊。"
        )
        template = str(self.config.get(template_key, default) or "").strip()
        if not template:
            return
        can_at = notice_type == "member_join"
        content = render_member_notice(template, member_openid, can_at=can_at)
        platform = self.context.get_platform_inst(platform_id)
        if platform is None:
            return
        api = QQGroupManageAPI(platform.client)
        event_id = str(item.get("_event_id") or "") if can_at else ""
        try:
            if can_at and "<qqbot-at-user" in content:
                await api.send_group_markdown(
                    group_openid,
                    content,
                    event_id=event_id,
                )
            else:
                await api.send_group_text(
                    group_openid,
                    content,
                    event_id=event_id,
                )
        except Exception:
            logger.exception("[%s] 成员进退群通知发送失败", PLUGIN_NAME)

    async def _handle_join_request_event(
        self, platform_id: str, item: dict[str, Any]
    ) -> None:
        if not self.config.get("enable_join_notice", True):
            return
        group_openid = str(item.get("group_openid") or "")
        if not group_openid:
            logger.warning("[%s] 入群申请缺少 group_openid: %r", PLUGIN_NAME, item)
            return
        platform = self.context.get_platform_inst(platform_id)
        if platform is None:
            return
        content = format_request(item)
        if self.config.get("enable_join_reply_review", True):
            content += "\n\n群管可回复本消息：同意 / 拒绝 [理由]"
        try:
            result = await QQGroupManageAPI(platform.client).send_group_text(
                group_openid,
                content,
                event_id=str(item.get("_event_id") or ""),
            )
            message_id = self._response_id(result)
            if message_id:
                stored = dict(item)
                stored["platform_id"] = platform_id
                stored["group_openid"] = group_openid
                self.storage.put_pending(message_id, stored)
        except Exception:
            logger.exception("[%s] 转发入群申请失败", PLUGIN_NAME)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=10)
    async def reply_review(self, event: AstrMessageEvent) -> None:
        if not self._is_qq_group(event) or not self.config.get("enable_join_reply_review", True):
            return
        action_match = ACTION_RE.match(event.get_message_str().strip())
        if not action_match:
            return
        reply = next((part for part in event.get_messages() if isinstance(part, Reply)), None)
        if reply is None or not reply.id:
            return
        pending = self.storage.get_pending(str(reply.id))
        if not pending:
            return
        event.stop_event()
        if pending.get("group_openid") != event.get_group_id():
            yield event.plain_result("这条申请不属于当前群，不能跨群审批。")
            return
        if not self._can_manage(event):
            yield event.plain_result("你不是本群群管，也不是 AstrBot 管理员。")
            return
        approve = action_match.group(1) in {"同意", "通过"}
        reason = (action_match.group(2) or "").strip()
        try:
            await self._review(
                event,
                str(pending["group_openid"]),
                str(pending["member_openid"]),
                str(pending["join_request_id"]),
                approve,
                reason,
            )
        except Exception as exc:
            yield event.plain_result(f"审批失败：{exc}")
            return
        self.storage.remove_pending(str(reply.id))
        result = "已同意入群申请。" if approve else f"已拒绝入群申请。{(' 理由：' + reason) if reason else ''}"
        yield event.plain_result(result)

    @filter.command("禁言")
    async def mute_command(self, event: AstrMessageEvent, time: str = "") -> None:
        """禁言被艾特的成员，时间支持 30秒、10分、2小时、1天。"""
        if not self.config.get("enable_mute_command", True):
            yield event.plain_result("禁言指令已在插件配置中关闭。")
            return
        if not self._is_qq_group(event):
            yield event.plain_result("该指令仅支持 QQ 官方机器人群聊。")
            return
        if not self._can_manage(event):
            yield event.plain_result("你不是本群群管，也不是 AstrBot 管理员。")
            return
        targets = self._mentioned_members(event)
        if not targets:
            yield event.plain_result("请艾特要禁言的成员，例如：/禁言 @用户 [时间]")
            return
        try:
            time = extract_mute_duration(
                event.get_message_str(),
                time,
                str(self.config.get("default_mute_duration", "10分") or "10分"),
            )
            seconds = parse_duration(time)
            self._validate_duration(seconds)
            for member_openid in targets:
                await self._mute(event, event.get_group_id(), member_openid, seconds)
        except Exception as exc:
            yield event.plain_result(f"禁言失败：{exc}")
            return
        if seconds == 0:
            yield event.plain_result(f"已解除 {len(targets)} 名成员的禁言。")
        else:
            yield event.plain_result(f"已禁言 {len(targets)} 名成员，时长 {time}。")

    @filter.command("添加群管")
    async def add_group_admin(self, event: AstrMessageEvent) -> None:
        if not self.config.get("enable_group_admin_commands", True):
            return
        if not self._is_qq_group(event):
            yield event.plain_result("该指令仅支持 QQ 官方机器人群聊。")
            return
        if not event.is_admin():
            yield event.plain_result("只有 AstrBot 管理员能添加分群群管。")
            return
        targets = self._mentioned_members(event)
        if not targets:
            yield event.plain_result("请艾特要添加的群管。")
            return
        added = sum(self.storage.add_group_admin(event.get_group_id(), item) for item in targets)
        yield event.plain_result(f"已添加 {added} 名本群群管。")

    @filter.command("删除群管", alias={"移除群管"})
    async def remove_group_admin(self, event: AstrMessageEvent) -> None:
        if not self.config.get("enable_group_admin_commands", True):
            return
        if not self._is_qq_group(event):
            yield event.plain_result("该指令仅支持 QQ 官方机器人群聊。")
            return
        if not event.is_admin():
            yield event.plain_result("只有 AstrBot 管理员能删除分群群管。")
            return
        targets = self._mentioned_members(event)
        if not targets:
            yield event.plain_result("请艾特要删除的群管。")
            return
        removed = sum(self.storage.remove_group_admin(event.get_group_id(), item) for item in targets)
        yield event.plain_result(f"已删除 {removed} 名本群群管。")

    @filter.command("群管列表")
    async def list_group_admins(self, event: AstrMessageEvent) -> None:
        if not self.config.get("enable_group_admin_commands", True):
            return
        if not self._is_qq_group(event):
            yield event.plain_result("该指令仅支持 QQ 官方机器人群聊。")
            return
        admins = self.storage.group_admins(event.get_group_id())
        content = "本群群管：\n" + ("\n".join(f"- {item}" for item in admins) if admins else "（暂无）")
        content += "\nAstrBot 管理员默认全局拥有群管权限。"
        yield event.plain_result(content)

    @filter.command("群管帮助")
    async def group_admin_help(self, event: AstrMessageEvent) -> None:
        if not self.config.get("enable_group_admin_commands", True):
            return
        default_duration = str(
            self.config.get("default_mute_duration", "10分") or "10分"
        )
        yield event.plain_result(
            "QQ 群管帮助\n"
            "/禁言 @用户 [时间] - 禁言成员；不填时间默认 "
            f"{default_duration}\n"
            "/禁言 @用户 解除 - 解除禁言\n"
            "/添加群管 @用户 - 添加本群群管（仅 AstrBot 管理员）\n"
            "/删除群管 @用户 - 删除本群群管（仅 AstrBot 管理员）\n"
            "/群管列表 - 查看本群群管\n"
            "/群管帮助 - 显示本帮助\n\n"
            "时间支持：30秒、10分、2小时、1天2小时；纯数字按分钟。\n"
            "入群申请：群管回复申请通知发送“同意”或“拒绝 [理由]”即可审批。\n"
            "AstrBot 管理员默认在所有群拥有群管权限。"
        )

    @filter.llm_tool(name="qq_group_mute_member")
    async def mute_tool(
        self,
        event: AstrMessageEvent,
        member_openid: str,
        duration: str = "",
    ) -> MessageEventResult:
        """禁言或解禁当前 QQ 群的一名普通成员，仅群管可用。

        Args:
            member_openid(string): 被操作成员的群成员 OpenID
            duration(string): 可选禁言时长，如 30秒、10分、2小时、1天；省略时使用插件默认时长，填 0 或 解除表示解禁
        """
        if not self.config.get("enable_mute_tool", True):
            return event.plain_result("QQ 群禁言工具已关闭。")
        if not self._is_qq_group(event) or not self._can_manage(event):
            return event.plain_result("当前场景无权使用 QQ 群禁言工具。")
        try:
            duration = duration.strip() or str(
                self.config.get("default_mute_duration", "10分") or "10分"
            )
            seconds = parse_duration(duration)
            self._validate_duration(seconds)
            await self._mute(event, event.get_group_id(), member_openid, seconds)
        except Exception as exc:
            return event.plain_result(f"禁言操作失败：{exc}")
        return event.plain_result("解禁成功。" if seconds == 0 else f"禁言成功，时长 {duration}。")

    @filter.llm_tool(name="qq_group_list_join_requests")
    async def list_join_requests_tool(
        self,
        event: AstrMessageEvent,
        cursor: str = "",
        limit: int = 20,
    ) -> MessageEventResult:
        """拉取当前 QQ 群待处理的入群申请列表，仅群管可用。

        Args:
            cursor(string): 分页游标，第一页传空字符串
            limit(number): 拉取条数，范围 1 到 100
        """
        if not self.config.get("enable_join_list_tool", True):
            return event.plain_result("入群申请列表工具已关闭。")
        if not self._is_qq_group(event) or not self._can_manage(event):
            return event.plain_result("当前场景无权拉取入群申请。")
        platform = self._platform(event)
        try:
            result = await QQGroupManageAPI(platform.client).list_join_requests(
                event.get_group_id(),
                cursor=cursor,
                limit=limit or int(self.config.get("join_request_page_size", 20)),
            )
        except Exception as exc:
            return event.plain_result(f"拉取入群申请失败：{exc}")
        items = result.get("list", []) if isinstance(result, dict) else []
        if not items:
            return event.plain_result("当前没有待处理的入群申请。")
        text = "\n\n".join(format_request(item, index) for index, item in enumerate(items, 1))
        next_cursor = result.get("next_cursor", "") if isinstance(result, dict) else ""
        if next_cursor:
            text += f"\n\n下一页 cursor：{next_cursor}"
        return event.plain_result(text)

    @filter.llm_tool(name="qq_group_review_join_request")
    async def review_join_request_tool(
        self,
        event: AstrMessageEvent,
        member_openid: str,
        join_request_id: str,
        action: str,
        reject_reason: str = "",
    ) -> MessageEventResult:
        """同意或拒绝当前 QQ 群的某个入群申请，仅群管可用。

        Args:
            member_openid(string): 申请人的群成员 OpenID
            join_request_id(string): 入群申请 ID
            action(string): approve 表示同意，decline 表示拒绝
            reject_reason(string): 拒绝理由，同意时留空
        """
        if not self.config.get("enable_join_review_tool", True):
            return event.plain_result("入群申请审批工具已关闭。")
        if not self._is_qq_group(event) or not self._can_manage(event):
            return event.plain_result("当前场景无权审批入群申请。")
        action = action.strip().lower()
        if action not in {"approve", "decline"}:
            return event.plain_result("action 只能是 approve 或 decline。")
        try:
            await self._review(
                event,
                event.get_group_id(),
                member_openid,
                join_request_id,
                action == "approve",
                reject_reason,
            )
        except Exception as exc:
            return event.plain_result(f"审批失败：{exc}")
        return event.plain_result("已同意入群申请。" if action == "approve" else "已拒绝入群申请。")

    async def _mute(
        self,
        event: AstrMessageEvent,
        group_openid: str,
        member_openid: str,
        seconds: int,
    ) -> Any:
        api = QQGroupManageAPI(self._platform(event).client)
        if seconds == 0:
            return await api.mute_member(group_openid, member_openid, op="del")
        expire_at = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")
        return await api.mute_member(
            group_openid,
            member_openid,
            op="add",
            mute_expire_at=expire_at,
        )

    async def _review(
        self,
        event: AstrMessageEvent,
        group_openid: str,
        member_openid: str,
        join_request_id: str,
        approve: bool,
        reason: str,
    ) -> Any:
        return await QQGroupManageAPI(self._platform(event).client).review_join_request(
            group_openid,
            member_openid,
            join_request_id,
            approve=approve,
            reject_reason=reason,
        )

    def _platform(self, event: AstrMessageEvent) -> Any:
        platform = self.context.get_platform_inst(event.get_platform_id())
        if platform is None or not hasattr(platform, "client"):
            raise RuntimeError("找不到当前 QQ 官方平台实例")
        return platform

    def _is_qq_group(self, event: AstrMessageEvent) -> bool:
        return event.get_platform_name() in QQ_PLATFORMS and bool(event.get_group_id())

    def _can_manage(self, event: AstrMessageEvent) -> bool:
        return event.is_admin() or event.get_sender_id() in self.storage.group_admins(event.get_group_id())

    def _mentioned_members(self, event: AstrMessageEvent) -> list[str]:
        raw = getattr(event.message_obj, "raw_message", None)
        mentions = getattr(raw, "mentions", None) or []
        targets: list[str] = []
        for mention in mentions:
            if getattr(mention, "is_you", False):
                continue
            member_openid = getattr(mention, "member_openid", None) or getattr(mention, "id", None)
            if member_openid and str(member_openid) not in targets:
                targets.append(str(member_openid))
        # Fallback for adapters that preserve At components directly.
        for part in event.get_messages():
            if isinstance(part, At) and str(part.qq) not in {"qq_official", event.get_self_id(), "all"}:
                if str(part.qq) not in targets:
                    targets.append(str(part.qq))
        return targets[:10]

    def _validate_duration(self, seconds: int) -> None:
        maximum = max(1, int(self.config.get("max_mute_seconds", 2592000)))
        if seconds < 0 or seconds > maximum:
            raise ValueError(f"禁言时长必须在 0 到 {maximum} 秒之间")

    @staticmethod
    def _response_id(result: Any) -> str:
        if isinstance(result, dict):
            return str(result.get("id") or "")
        return str(getattr(result, "id", "") or "")

    async def terminate(self) -> None:
        if self._patch_task:
            self._patch_task.cancel()
        for state in self._patched.values():
            client = state["client"]
            for attr, old_handler in state["old_handlers"].items():
                if old_handler is None:
                    try:
                        delattr(client, attr)
                    except AttributeError:
                        pass
                else:
                    setattr(client, attr, old_handler)
        self._patched.clear()


def render_member_notice(template: str, member_openid: str, *, can_at: bool) -> str:
    """Render the only supported notice placeholder: {member_at}."""
    if can_at and member_openid:
        member_value = f'<qqbot-at-user id="{member_openid}" />'
    else:
        member_value = member_openid or "未知成员"
    return template.replace("{member_at}", member_value)


def extract_mute_duration(
    message_text: str,
    fallback: str,
    default_duration: str,
) -> str:
    """Extract an optional duration while discarding QQ mention markup."""
    text = re.sub(r"^/?禁言\s*", "", message_text.strip())
    text = re.sub(r"<@!?[^>]+>", "", text).strip()
    fallback = re.sub(r"<@!?[^>]+>", "", fallback).strip()
    return text or fallback or default_duration.strip() or "10分"
