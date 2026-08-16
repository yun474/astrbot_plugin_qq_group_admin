from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.star import Context, Star, StarTools, register

from .api import QQGroupManageAPI
from .storage import PluginStorage


PLUGIN_NAME = "astrbot_plugin_qq_group_admin"
QQ_PLATFORMS = {"qq_official", "qq_official_webhook"}
GROUP_MEMBER_INTENT = 1 << 24
GROUP_AND_C2C_INTENT = 1 << 25
LIFECYCLE_INTENTS = GROUP_MEMBER_INTENT | GROUP_AND_C2C_INTENT
LIFECYCLE_EVENTS = (
    "group_join_request",
    "group_member_add",
    "group_member_remove",
)
ACTION_RE = re.compile(r"^(同意|通过|拒绝|驳回)(?:\s+(.+))?$", re.S)
INDEX_ACTION_RE = re.compile(
    r"^/?(同意|通过|拒绝|驳回)\s+(\d+)(?:\s+(.+))?$",
    re.S,
)
JOIN_REQUEST_ID_RE = re.compile(r"申请\s*ID[：:]\s*([^\s]+)", re.I)
JOIN_REQUEST_INDEX_RE = re.compile(r"#(\d+)\s+新的入群申请")
APPLY_SOURCE_NAMES = {
    "self_apply": "自主申请",
    "search": "搜索群聊申请",
    "scan_qr_code": "扫描二维码申请",
    "group_card": "群分享卡片申请",
    "shared_card": "群分享卡片申请",
    "invited": "受邀加入",
    "invite": "受邀加入",
    "admin_invite": "管理员邀请",
}
TIME_PART_RE = re.compile(r"(\d+)\s*(天|日|小时|时|分钟|分|秒|s|m|h|d)", re.I)


@dataclass(frozen=True)
class FeatureDefinition:
    name: str
    key: str
    category: str
    default: bool = True
    inverted: bool = False


FEATURES = (
    FeatureDefinition("禁言指令", "enable_mute_command", "群内指令"),
    FeatureDefinition(
        "禁言成功提示",
        "silent_mute_success_notice",
        "群内指令",
        default=False,
        inverted=True,
    ),
    FeatureDefinition("禁言状态指令", "enable_mute_status_command", "群内指令"),
    FeatureDefinition("群管指令", "enable_group_admin_commands", "群内指令"),
    FeatureDefinition("入群申请通知", "enable_join_notice", "自动通知"),
    FeatureDefinition("入群申请审批", "enable_join_reply_review", "自动通知"),
    FeatureDefinition("成员进群通知", "enable_member_join_notice", "自动通知"),
    FeatureDefinition("成员退群通知", "enable_member_leave_notice", "自动通知"),
    FeatureDefinition("LLM禁言工具", "enable_mute_tool", "LLM 工具"),
    FeatureDefinition("LLM解禁工具", "enable_unmute_tool", "LLM 工具"),
    FeatureDefinition("LLM禁言状态工具", "enable_mute_status_tool", "LLM 工具"),
    FeatureDefinition("LLM申请列表工具", "enable_join_list_tool", "LLM 工具"),
    FeatureDefinition("LLM申请审批工具", "enable_join_review_tool", "LLM 工具"),
)
FEATURES_BY_NAME = {feature.name: feature for feature in FEATURES}

CONFIG_SECTIONS = {
    "enabled_group_umos": "scope_settings",
    "enable_per_group_feature_settings": "scope_settings",
    "allow_group_owner_manage_plugin_admins": "permission_settings",
    "allow_group_admin_manage_plugin_admins": "permission_settings",
    "enable_mute_command": "command_settings",
    "enable_mute_status_command": "command_settings",
    "enable_group_admin_commands": "command_settings",
    "silent_mute_success_notice": "command_settings",
    "default_mute_duration": "command_settings",
    "enable_join_notice": "join_request_settings",
    "enable_join_reply_review": "join_request_settings",
    "join_request_page_size": "join_request_settings",
    "pending_retention_days": "join_request_settings",
    "enable_member_join_notice": "member_notice_settings",
    "member_join_message": "member_notice_settings",
    "enable_member_leave_notice": "member_notice_settings",
    "member_leave_message": "member_notice_settings",
    "enable_mute_tool": "llm_tool_settings",
    "enable_unmute_tool": "llm_tool_settings",
    "enable_mute_status_tool": "llm_tool_settings",
    "enable_join_list_tool": "llm_tool_settings",
    "enable_join_review_tool": "llm_tool_settings",
    "max_mute_seconds": "limit_settings",
}
LEGACY_CONFIG_KEYS = tuple(
    key
    for key in CONFIG_SECTIONS
    if key
    not in {
        "enable_per_group_feature_settings",
        "allow_group_owner_manage_plugin_admins",
        "allow_group_admin_manage_plugin_admins",
    }
)


def format_feature_status(
    values: dict[str, bool],
    *,
    per_group: bool,
) -> str:
    lines = [
        "# 🛡️ 群管功能",
        "",
        f"当前开关模式：**{'分群' if per_group else '全局'}**",
    ]
    for category in dict.fromkeys(feature.category for feature in FEATURES):
        lines.extend(("", f"## {category}", ""))
        for feature in FEATURES:
            if feature.category != category:
                continue
            enabled = values[feature.name]
            status = "已开启" if enabled else "已关闭"
            next_action = "关闭" if enabled else "开启"
            command = quote(
                f"/群管功能 {feature.name} {next_action}",
                safe="",
            )
            status = (
                f"[{status}]"
                f"(mqqapi://aio/inlinecmd?command={command}"
                "&enter=false&reply=false)"
            )
            lines.append(f"{feature.name}：{status}  ")
    if per_group:
        lines.extend(("", "> 点击蓝色状态可把相反操作填入输入框，不会自动发送。"))
    else:
        lines.extend(("", "> 全局模式只有框架管理员允许更改"))
    return "\n".join(lines)


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


def intent_value(value: Any) -> int:
    if isinstance(value, int):
        return value
    raw = getattr(value, "value", 0)
    return raw if isinstance(raw, int) else 0


def format_request(item: dict[str, Any], index: int | None = None) -> str:
    prefix = f"#{index} " if index is not None else ""
    lines = [
        f"{prefix}新的入群申请",
        f"昵称：{item.get('username') or '未提供'}",
        f"申请时间：{item.get('apply_at') or '未提供'}",
        "来源：" + format_apply_source(item.get("apply_source")),
    ]
    if item.get("risk_tips"):
        lines.append(f"风险提示：{item['risk_tips']}")
    verify = item.get("verify_info") or {}
    if verify.get("verify_message"):
        lines.append(f"验证消息：{verify['verify_message']}")
    qa_list = verify.get("review_qa_list") or []
    for qa in qa_list:
        lines.append(
            f"问题：{qa.get('question') or '（无问题）'}\n"
            f"答：{qa.get('answer') or '（未回答）'}"
        )
    if item.get("auto_approved"):
        lines.append(f"自动审批策略：{item['auto_approved'].get('strategy_id') or '已自动通过'}")
    return "\n".join(lines)


def format_apply_source(value: Any) -> str:
    source = str(value or "").strip()
    if not source:
        return "未提供"
    return APPLY_SOURCE_NAMES.get(source.lower(), "其他来源")


def review_keyboard(index: int) -> dict[str, Any]:
    def button(button_id: str, label: str, command: str, style: int) -> dict[str, Any]:
        return {
            "id": f"join-{button_id}-{index}",
            "render_data": {
                "label": label,
                "visited_label": label,
                "style": style,
            },
            "action": {
                "type": 2,
                "permission": {
                    "type": 2,
                    "specify_role_ids": [],
                    "specify_user_ids": [],
                },
                "click_limit": 1,
                "data": command,
                "at_bot_show_channel_list": False,
            },
        }

    return {
        "content": {
            "rows": [
                {
                    "buttons": [
                        button("approve", "同意", f"/同意 {index}", 1),
                        button("decline", "拒绝", f"/拒绝 {index}", 0),
                    ]
                }
            ]
        }
    }


def format_mute_status(result: dict[str, Any]) -> str:
    global_rule = result.get("global_rule") or {}
    mode = global_rule.get("mode") or "none"
    mode_text = {
        "none": "未开启",
        "always": "始终全员禁言",
        "schedule": "按规则全员禁言",
    }.get(mode, f"未知模式（{mode}）")
    lines = ["本群禁言状态", f"全员禁言：{mode_text}"]

    schedule_rules = global_rule.get("schedule_rules") or []
    if schedule_rules:
        lines.append("定时规则：")
        for rule in schedule_rules:
            enabled = "启用" if rule.get("enabled") else "停用"
            lines.append(
                f"- [{enabled}] {rule.get('start_at') or '未知'} 至 "
                f"{rule.get('end_at') or '未知'}（{rule.get('task_id') or '无任务 ID'}）"
            )

    recurring_rules = global_rule.get("recurring_rules") or []
    if recurring_rules:
        weekday_names = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}
        lines.append("周期规则：")
        for rule in recurring_rules:
            enabled = "启用" if rule.get("enabled") else "停用"
            weekdays = "、".join(
                f"周{weekday_names.get(day, day)}" for day in rule.get("weekdays", [])
            ) or "未指定星期"
            lines.append(
                f"- [{enabled}] {weekdays} {rule.get('start_time') or '未知'}-"
                f"{rule.get('end_time') or '未知'}（{rule.get('task_id') or '无任务 ID'}）"
            )

    members = result.get("members") or []
    lines.append(f"单独禁言成员：{len(members)} 人")
    for member in members:
        username = member.get("username") or "未知昵称"
        member_openid = member.get("member_openid") or "未知 OpenID"
        expire_at = member.get("mute_expire_at") or "未知"
        lines.append(f"- {username}（{member_openid}），到期：{expire_at}")
    return "\n".join(lines)


def format_group_admin_help(default_duration: str) -> str:
    return (
        "# 🛡️ QQ 群管帮助\n\n"
        "## 成员管理\n\n"
        "> `/禁言 @用户 [时间]`  \n"
        f"> 禁言成员；不填时间默认 **{default_duration}**\n\n"
        "> `/解禁 @用户`  \n"
        "> 解除成员禁言\n\n"
        "> `/禁言状态`  \n"
        "> 查看全员禁言规则及被禁言成员\n\n"
        "## 群管管理\n\n"
        "> `/添加群管 @用户`  \n"
        "> 添加本群插件群管；默认仅 AstrBot 管理员可用\n\n"
        "> `/删除群管 @用户`  \n"
        "> 删除本群插件群管；可在配置中授权 QQ 群主或群管理员\n\n"
        "> `/群管列表`  \n"
        "> 查看本群群管\n\n"
        "> `/群管功能`  \n"
        "> 查看功能开关；分群模式下点击蓝色状态可填入切换指令\n\n"
        "## 时间格式\n\n"
        "支持 `30秒`、`10分`、`2小时`、`1天2小时`  \n"
        "纯数字按分钟处理，例如 `30` 表示 **30分钟**\n\n"
        "## 入群审批\n\n"
        "点击申请通知按钮，或直接发送编号指令：\n\n"
        "- `/同意 1`\n"
        "- `/拒绝 1 理由`\n\n"
        "也可以回复对应的申请通知：\n\n"
        "- `同意`\n"
        "- `拒绝`\n"
        "- `拒绝 理由`\n\n"
        "> `/群申请归零`  \n"
        "> 清除本群待审映射，并让下一条申请重新从 **#1** 编号\n\n"
        "---\n\n"
        "💡 AstrBot 管理员拥有全局权限；插件群管和 QQ 群主/管理员拥有本群普通群管权限。"
    )


def resolve_tool_event(value: Any) -> AstrMessageEvent:
    """Accept both legacy AstrMessageEvent and AstrBot 4.26 ContextWrapper."""
    if hasattr(value, "get_platform_name") and hasattr(value, "get_group_id"):
        return value
    context = getattr(value, "context", None)
    event = getattr(context, "event", None)
    if event is None:
        event = getattr(getattr(context, "context", None), "event", None)
    if event is None:
        raise RuntimeError("无法从 AstrBot 工具上下文中取得消息事件")
    return event


def review_action_text(event: AstrMessageEvent) -> str:
    """Read only newly typed plain text, excluding quote and mention components."""
    return "".join(
        str(getattr(part, "text", "") or "")
        for part in event.get_messages()
        if isinstance(part, Plain)
    ).strip()


def quoted_join_request_id(reply: Reply) -> str:
    quoted_text = str(reply.message_str or reply.text or "")
    if not quoted_text:
        quoted_text = "".join(
            str(getattr(part, "text", "") or "")
            for part in (reply.chain or [])
            if isinstance(part, Plain)
        )
    match = JOIN_REQUEST_ID_RE.search(quoted_text)
    return match.group(1).strip() if match else ""


def quoted_join_request_index(reply: Reply) -> int | None:
    quoted_text = str(reply.message_str or reply.text or "")
    if not quoted_text:
        quoted_text = "".join(
            str(getattr(part, "text", "") or "")
            for part in (reply.chain or [])
            if isinstance(part, Plain)
        )
    match = JOIN_REQUEST_INDEX_RE.search(quoted_text)
    return int(match.group(1)) if match else None


@register(
    PLUGIN_NAME,
    "yun474",
    "QQ 官方机器人群管理：禁言、入群申请审批、分群管理员与 LLM 工具",
    "2.4.0",
)
class QQGroupAdminPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._migrate_config_layout()
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.storage = PluginStorage(
            data_dir / "state.json",
            int(self._config("pending_retention_days", 30)),
        )
        self._patched: dict[str, dict[str, Any]] = {}
        self._patch_task: asyncio.Task | None = None
        self._parser_state_class: Any = None
        self._owned_parser_methods: dict[str, Any] = {}
        self._install_parser_patch()

    def _install_parser_patch(self) -> None:
        """Install parsers before QQ connections snapshot ConnectionState methods."""
        try:
            from botpy.connection import ConnectionState
        except Exception:
            logger.exception("[%s] 无法加载 QQ 事件解析器", PLUGIN_NAME)
            return

        self._parser_state_class = ConnectionState
        for event_name in LIFECYCLE_EVENTS:
            attr = f"parse_{event_name}"
            if hasattr(ConnectionState, attr):
                continue

            def parser(
                state: Any,
                payload: dict[str, Any],
                dispatched_event: str = event_name,
            ) -> None:
                data = dict(payload.get("d", {}) or {})
                data["_event_id"] = str(payload.get("id") or "")
                state._dispatch(dispatched_event, data)

            parser.__name__ = attr
            parser.__qualname__ = f"ConnectionState.{attr}"
            setattr(parser, "__qq_group_admin_parser__", True)
            setattr(ConnectionState, attr, parser)
            self._owned_parser_methods[attr] = parser

        if self._owned_parser_methods:
            logger.info(
                "[%s] 已预安装 QQ 入群申请与成员进退群解析器",
                PLUGIN_NAME,
            )

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        self._start_patch_task()

    @filter.on_platform_loaded()
    async def on_platform_loaded(self) -> None:
        await self._patch_platforms_once()

    @filter.on_plugin_loaded()
    async def on_plugin_loaded(self, metadata: Any) -> None:
        """Handle installation or hot reload after QQ platforms are already running."""
        await self._patch_platforms_once()
        self._start_patch_task()

    def _start_patch_task(self) -> None:
        if self._patch_task is None or self._patch_task.done():
            self._patch_task = asyncio.create_task(self._patch_platforms_until_ready())

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

                async def handler(
                    data: dict[str, Any],
                    pid: str = platform_id,
                    original: Any = old_handlers["on_group_join_request"],
                ) -> None:
                    await self._handle_join_request_event(pid, data)
                    if original is not None:
                        result = original(data)
                        if hasattr(result, "__await__"):
                            await result

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
                self._patched[platform_id] = {
                    "client": client,
                    "old_handlers": old_handlers,
                    "connections": set(),
                }
            patch_state = self._patched[platform_id]
            if meta.name == "qq_official":
                await self._ensure_group_member_intent(platform, client)
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

    async def _ensure_group_member_intent(self, platform: Any, client: Any) -> None:
        current = intent_value(getattr(client, "intents", 0))
        required = current | LIFECYCLE_INTENTS
        if current != required:
            client.intents = required
            platform_intents = getattr(platform, "intents", None)
            if hasattr(platform_intents, "value"):
                platform_intents.value = (
                    intent_value(platform_intents) | LIFECYCLE_INTENTS
                )
            logger.info(
                "[%s] 已启用群生命周期 Intents（1 << 24 | 1 << 25），当前值：%s",
                PLUGIN_NAME,
                required,
            )

        connection = getattr(client, "_connection", None)
        pending_sessions = getattr(connection, "_session_list", None) or []
        for session in pending_sessions:
            if isinstance(session, dict):
                session["intent"] = required

        for websocket in list(getattr(client, "_active_websockets", None) or []):
            session = getattr(websocket, "_session", None)
            if not isinstance(session, dict):
                continue
            if (
                intent_value(session.get("intent", 0)) & LIFECYCLE_INTENTS
                == LIFECYCLE_INTENTS
            ):
                continue
            session["intent"] = required
            session["session_id"] = ""
            session["last_seq"] = 0
            try:
                close = getattr(websocket, "close", None)
                if callable(close):
                    await close()
                else:
                    socket = getattr(websocket, "_conn", None)
                    if socket is not None and not getattr(socket, "closed", True):
                        websocket._can_reconnect = False
                        await socket.close()
                logger.info(
                    "[%s] 已重连 QQ WebSocket 以应用群生命周期 Intents",
                    PLUGIN_NAME,
                )
            except Exception:
                logger.exception("[%s] 应用群生命周期 Intents 时重连失败", PLUGIN_NAME)

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
        group_openid = str(item.get("group_openid") or "")
        member_openid = str(item.get("member_openid") or "")
        if not group_openid:
            logger.warning("[%s] 成员事件缺少 group_openid: %r", PLUGIN_NAME, item)
            return
        group_umo = self._group_umo(platform_id, group_openid)
        if not self._umo_enabled(group_umo):
            return
        if not self._feature_setting(enabled_key, group_umo):
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
        template = str(self._config(template_key, default) or "").strip()
        if not template:
            return
        can_at = notice_type == "member_join"
        content = render_member_notice(template, member_openid, can_at=can_at)
        platform = self.context.get_platform_inst(platform_id)
        if platform is None:
            return
        api = QQGroupManageAPI(platform.client)
        try:
            if can_at and "<qqbot-at-user" in content:
                try:
                    await api.send_group_markdown(
                        group_openid,
                        content,
                    )
                except Exception:
                    logger.warning(
                        "[%s] 进群 Markdown 欢迎发送失败，降级为普通文本",
                        PLUGIN_NAME,
                        exc_info=True,
                    )
                    fallback = render_member_notice(
                        template,
                        member_openid,
                        can_at=False,
                    )
                    await api.send_group_text(
                        group_openid,
                        fallback,
                    )
            else:
                await api.send_group_text(
                    group_openid,
                    content,
                )
        except Exception:
            logger.exception("[%s] 成员进退群通知发送失败", PLUGIN_NAME)

    async def _handle_join_request_event(
        self, platform_id: str, item: dict[str, Any]
    ) -> None:
        group_openid = str(item.get("group_openid") or "")
        if not group_openid:
            logger.warning("[%s] 入群申请缺少 group_openid: %r", PLUGIN_NAME, item)
            return
        group_umo = self._group_umo(platform_id, group_openid)
        if not self._umo_enabled(group_umo):
            return
        if not self._feature_setting("enable_join_notice", group_umo):
            return
        platform = self.context.get_platform_inst(platform_id)
        if platform is None:
            return
        stored = dict(item)
        stored["platform_id"] = platform_id
        stored["group_openid"] = group_openid
        pending_key, review_index = self.storage.reserve_pending(stored)
        content = format_request(item, review_index)
        review_enabled = self._feature_setting(
            "enable_join_reply_review",
            group_umo,
        )
        if review_enabled:
            content += (
                f"\n\n群管可点击按钮，或发送：/同意 {review_index} / "
                f"/拒绝 {review_index} [理由]\n也可回复本消息：同意 / 拒绝 [理由]"
            )
        try:
            api = QQGroupManageAPI(platform.client)
            try:
                result = await api.send_group_markdown(
                    group_openid,
                    content,
                    keyboard=review_keyboard(review_index) if review_enabled else None,
                )
            except Exception:
                logger.warning(
                    "[%s] 入群申请 Markdown 按钮发送失败，降级为纯文本通知",
                    PLUGIN_NAME,
                    exc_info=True,
                )
                result = await api.send_group_text(group_openid, content)
            message_id = self._response_id(result)
            if message_id:
                pending_key = self.storage.bind_pending_message(
                    pending_key,
                    message_id,
                )
        except Exception:
            self.storage.remove_pending(pending_key)
            logger.exception("[%s] 转发入群申请失败", PLUGIN_NAME)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=10)
    async def reply_review(self, event: AstrMessageEvent) -> None:
        if not self._is_qq_group(event) or not self._event_feature_setting(
            event,
            "enable_join_reply_review",
        ):
            return
        action_text = review_action_text(event)
        indexed_match = INDEX_ACTION_RE.match(action_text)
        action_match = ACTION_RE.match(action_text)
        if not indexed_match and not action_match:
            return

        pending_key = ""
        pending = None
        quoted_request_id = ""
        quoted_index = None
        if indexed_match:
            review_index = int(indexed_match.group(2))
            matched = self.storage.find_pending_by_index(
                event.get_group_id(),
                review_index,
            )
            if matched:
                pending_key, pending = matched
            event.stop_event()
        else:
            reply = next(
                (part for part in event.get_messages() if isinstance(part, Reply)),
                None,
            )
            if reply is None:
                return
            pending_key = str(reply.id or "")
            pending = self.storage.get_pending(pending_key) if pending_key else None
            quoted_index = quoted_join_request_index(reply)
            if not pending and quoted_index is not None:
                matched = self.storage.find_pending_by_index(
                    event.get_group_id(),
                    quoted_index,
                )
                if matched:
                    pending_key, pending = matched
            quoted_request_id = quoted_join_request_id(reply)
            if not pending and quoted_request_id:
                matched = self.storage.find_pending_by_join_request_id(
                    quoted_request_id,
                    event.get_group_id(),
                )
                if matched:
                    pending_key, pending = matched

        if not pending:
            if indexed_match or quoted_request_id or quoted_index is not None:
                event.stop_event()
                yield event.plain_result(
                    "找不到这条入群申请，可能编号错误、通知已过期或插件数据已被清理。"
                )
            return
        event.stop_event()
        if pending.get("group_openid") != event.get_group_id():
            yield event.plain_result("这条申请不属于当前群，不能跨群审批。")
            return
        if not self._can_manage(event):
            yield event.plain_result("你没有本群群管权限。")
            return
        approve = (indexed_match or action_match).group(1) in {"同意", "通过"}
        reason_group = 3 if indexed_match else 2
        reason = ((indexed_match or action_match).group(reason_group) or "").strip()
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
        self.storage.remove_pending(pending_key)
        result = "已同意入群申请。" if approve else f"已拒绝入群申请。{(' 理由：' + reason) if reason else ''}"
        yield event.plain_result(result)

    @filter.command("禁言")
    async def mute_command(self, event: AstrMessageEvent) -> None:
        """禁言被艾特的成员，时间支持 30秒、10分、2小时、1天。"""
        if not self._is_qq_group(event):
            yield event.plain_result("该指令仅支持 QQ 官方机器人群聊。")
            return
        if not self._event_feature_setting(event, "enable_mute_command"):
            yield event.plain_result("禁言指令已关闭。")
            return
        if not self._can_manage(event):
            yield event.plain_result("你没有本群群管权限。")
            return
        targets = self._mentioned_members(event)
        if not targets:
            yield event.plain_result("请艾特要禁言的成员，例如：/禁言 @用户 [时间]")
            return
        try:
            time = extract_mute_duration(
                event.get_message_str(),
                str(self._config("default_mute_duration", "1分") or "1分"),
            )
            seconds = parse_duration(time)
            self._validate_duration(seconds)
            for member_openid in targets:
                await self._mute(event, event.get_group_id(), member_openid, seconds)
        except Exception as exc:
            yield event.plain_result(f"禁言失败：{exc}")
            return
        if self._event_feature_setting(
            event,
            "silent_mute_success_notice",
            False,
        ):
            return
        if seconds == 0:
            yield event.plain_result(f"已解除 {len(targets)} 名成员的禁言。")
        else:
            yield event.plain_result(f"已禁言 {len(targets)} 名成员，时长 {time}。")

    @filter.command("解禁")
    async def unmute_command(self, event: AstrMessageEvent) -> None:
        """解除被艾特成员的禁言。"""
        if not self._is_qq_group(event):
            yield event.plain_result("该指令仅支持 QQ 官方机器人群聊。")
            return
        if not self._event_feature_setting(event, "enable_mute_command"):
            yield event.plain_result("禁言指令已关闭。")
            return
        if not self._can_manage(event):
            yield event.plain_result("你没有本群群管权限。")
            return
        targets = self._mentioned_members(event)
        if not targets:
            yield event.plain_result("请艾特要解除禁言的成员，例如：/解禁 @用户")
            return
        try:
            for member_openid in targets:
                await self._mute(event, event.get_group_id(), member_openid, 0)
        except Exception as exc:
            yield event.plain_result(f"解禁失败：{exc}")
            return
        if self._event_feature_setting(
            event,
            "silent_mute_success_notice",
            False,
        ):
            return
        yield event.plain_result(f"已解除 {len(targets)} 名成员的禁言。")

    @filter.command("添加群管")
    async def add_group_admin(self, event: AstrMessageEvent) -> None:
        if not self._is_qq_group(event):
            yield event.plain_result("该指令仅支持 QQ 官方机器人群聊。")
            return
        if not self._event_feature_setting(event, "enable_group_admin_commands"):
            return
        if not self._can_manage_plugin_admins(event):
            yield event.plain_result(
                "只有 AstrBot 管理员能添加插件群管；也可在配置中授权 QQ 群主或群管理员。"
            )
            return
        targets = self._mentioned_members(event)
        if not targets:
            yield event.plain_result("请艾特要添加的群管。")
            return
        added = sum(self.storage.add_group_admin(event.get_group_id(), item) for item in targets)
        yield event.plain_result(f"已添加 {added} 名本群群管。")

    @filter.command("删除群管", alias={"移除群管"})
    async def remove_group_admin(self, event: AstrMessageEvent) -> None:
        if not self._is_qq_group(event):
            yield event.plain_result("该指令仅支持 QQ 官方机器人群聊。")
            return
        if not self._event_feature_setting(event, "enable_group_admin_commands"):
            return
        if not self._can_manage_plugin_admins(event):
            yield event.plain_result(
                "只有 AstrBot 管理员能删除插件群管；也可在配置中授权 QQ 群主或群管理员。"
            )
            return
        targets = self._mentioned_members(event)
        if not targets:
            yield event.plain_result("请艾特要删除的群管。")
            return
        removed = sum(self.storage.remove_group_admin(event.get_group_id(), item) for item in targets)
        yield event.plain_result(f"已删除 {removed} 名本群群管。")

    @filter.command("群管列表")
    async def list_group_admins(self, event: AstrMessageEvent) -> None:
        if not self._is_qq_group(event):
            yield event.plain_result("该指令仅支持 QQ 官方机器人群聊。")
            return
        if not self._event_feature_setting(event, "enable_group_admin_commands"):
            return
        admins = self.storage.group_admins(event.get_group_id())
        content = "本群群管：\n" + ("\n".join(f"- {item}" for item in admins) if admins else "（暂无）")
        content += (
            "\nAstrBot 管理员拥有全局权限；QQ 群主和群管理员"
            "默认拥有本群普通群管权限。"
        )
        yield event.plain_result(content)

    @filter.command("群管帮助")
    async def group_admin_help(self, event: AstrMessageEvent) -> None:
        if not self._is_qq_group(event):
            yield event.plain_result("该指令仅支持已启用的 QQ 官方机器人群聊。")
            return
        if not self._event_feature_setting(event, "enable_group_admin_commands"):
            return
        default_duration = str(
            self._config("default_mute_duration", "1分") or "1分"
        )
        yield event.plain_result(format_group_admin_help(default_duration)).use_markdown(
            True
        )

    @filter.command("群管功能")
    async def group_feature_settings(
        self,
        event: AstrMessageEvent,
        feature_name: str = "",
        action: str = "",
    ) -> None:
        """查看或修改当前群的独立功能开关。"""
        if not self._is_qq_group(event):
            yield event.plain_result("该指令仅支持已启用的 QQ 官方机器人群聊。")
            return
        per_group = bool(
            self._config("enable_per_group_feature_settings", False)
        )
        changing = bool(feature_name or action)
        if not per_group and changing and not self._is_astr_admin(event):
            yield event.plain_result(
                "你没有权限更改配置项，别乱动人家的功能啊！"
            )
            return
        if not self._can_manage(event):
            yield event.plain_result("你没有本群群管权限。")
            return

        if feature_name or action:
            feature = FEATURES_BY_NAME.get(feature_name.strip())
            if feature is None:
                available = "、".join(item.name for item in FEATURES)
                yield event.plain_result(f"未知功能。可用功能：{available}")
                return
            normalized_action = action.strip()
            if normalized_action in {"开启", "打开", "启用", "开"}:
                enabled = True
            elif normalized_action in {"关闭", "停用", "关"}:
                enabled = False
            else:
                yield event.plain_result(
                    f"用法：/群管功能 {feature.name} 开启（或关闭）"
                )
                return
            raw_value = not enabled if feature.inverted else enabled
            try:
                if per_group:
                    self.storage.set_group_feature_override(
                        self._event_group_umo(event),
                        feature.key,
                        raw_value,
                    )
                else:
                    self._set_config(feature.key, raw_value)
            except Exception as exc:
                yield event.plain_result(f"保存功能开关失败：{exc}")
                return

        values: dict[str, bool] = {}
        group_umo = self._event_group_umo(event) if per_group else ""
        for feature in FEATURES:
            raw_value = self._feature_setting(
                feature.key,
                group_umo,
                feature.default,
            )
            values[feature.name] = not raw_value if feature.inverted else raw_value
        yield event.plain_result(
            format_feature_status(values, per_group=per_group)
        ).use_markdown(True)

    @filter.command("群申请归零")
    async def reset_join_requests(self, event: AstrMessageEvent) -> None:
        if not self._is_qq_group(event):
            yield event.plain_result("该指令仅支持 QQ 官方机器人群聊。")
            return
        if not self._can_manage(event):
            yield event.plain_result("你没有本群群管权限。")
            return
        removed = self.storage.reset_group_pending(event.get_group_id())
        yield event.plain_result(
            f"已清除本群 {removed} 条待审申请映射；下一条申请将从 #1 开始。"
        )

    @filter.command("禁言状态")
    async def mute_status_command(self, event: AstrMessageEvent) -> None:
        """查看全员禁言规则和当前处于禁言中的成员。"""
        if not self._is_qq_group(event):
            yield event.plain_result("该指令仅支持 QQ 官方机器人群聊。")
            return
        if not self._event_feature_setting(event, "enable_mute_status_command"):
            yield event.plain_result("禁言状态指令已关闭。")
            return
        if not self._can_manage(event):
            yield event.plain_result("你没有本群群管权限。")
            return
        try:
            result = await QQGroupManageAPI(
                self._platform(event).client
            ).get_mute_status(event.get_group_id())
        except Exception as exc:
            yield event.plain_result(f"查询禁言状态失败：{exc}")
            return
        if not isinstance(result, dict):
            yield event.plain_result("查询禁言状态失败：接口返回格式异常。")
            return
        yield event.plain_result(format_mute_status(result))

    @filter.llm_tool(name="qq_group_mute_member")
    async def mute_tool(
        self,
        event: Any,
        member_openid: str,
        duration: str = "",
    ) -> str:
        """禁言或解禁当前 QQ 群的一名普通成员，仅群管可用。

        Args:
            member_openid(string): 被操作成员的群成员 OpenID
            duration(string): 可选禁言时长，如 30秒、10分、2小时、1天；省略时使用插件默认时长，填 0 或 解除表示解禁
        """
        event = resolve_tool_event(event)
        if not self._is_qq_group(event):
            return "当前场景不是 QQ 官方机器人群聊，无法使用群禁言工具。"
        if not self._event_feature_setting(event, "enable_mute_tool"):
            return "QQ 群禁言工具已关闭。"
        try:
            duration = duration.strip() or str(
                self._config("default_mute_duration", "1分") or "1分"
            )
            seconds = parse_duration(duration)
            self._validate_duration(seconds)
            await self._mute(event, event.get_group_id(), member_openid, seconds)
        except Exception as exc:
            return f"禁言操作失败：{exc}"
        action = "解禁" if seconds == 0 else f"禁言（时长 {duration}）"
        return f"已成功执行{action}，请根据用户语境自然回复。"

    @filter.llm_tool(name="qq_group_unmute_member")
    async def unmute_tool(
        self,
        event: Any,
        member_openid: str,
    ) -> str:
        """解除当前 QQ 群一名普通成员的禁言，仅群管可用。

        Args:
            member_openid(string): 被解除禁言成员的群成员 OpenID
        """
        event = resolve_tool_event(event)
        if not self._is_qq_group(event):
            return "当前场景不是 QQ 官方机器人群聊，无法使用群解禁工具。"
        if not self._event_feature_setting(event, "enable_unmute_tool"):
            return "QQ 群解禁工具已关闭。"
        try:
            await self._mute(event, event.get_group_id(), member_openid, 0)
        except Exception as exc:
            return f"解禁操作失败：{exc}"
        return "已成功执行解禁，请根据用户语境自然回复。"

    @filter.llm_tool(name="qq_group_get_mute_status")
    async def mute_status_tool(self, event: Any) -> str:
        """查询当前 QQ 群的全员禁言规则和被禁言成员列表，仅群管可用。"""
        event = resolve_tool_event(event)
        if not self._is_qq_group(event):
            return "当前场景不是 QQ 官方机器人群聊，无法查询群禁言状态。"
        if not self._event_feature_setting(event, "enable_mute_status_tool"):
            return "QQ 群禁言状态工具已关闭。"
        try:
            result = await QQGroupManageAPI(
                self._platform(event).client
            ).get_mute_status(event.get_group_id())
        except Exception as exc:
            return f"查询禁言状态失败：{exc}"
        if not isinstance(result, dict):
            return "查询禁言状态失败：接口返回格式异常。"
        return format_mute_status(result)

    @filter.llm_tool(name="qq_group_list_join_requests")
    async def list_join_requests_tool(
        self,
        event: Any,
        cursor: str = "",
        limit: int = 20,
    ) -> str:
        """拉取当前 QQ 群待处理的入群申请列表，仅群管可用。

        Args:
            cursor(string): 分页游标，第一页传空字符串
            limit(number): 拉取条数，范围 1 到 100
        """
        event = resolve_tool_event(event)
        if not self._is_qq_group(event):
            return "当前场景不是 QQ 官方机器人群聊，无法拉取入群申请。"
        if not self._event_feature_setting(event, "enable_join_list_tool"):
            return "入群申请列表工具已关闭。"
        platform = self._platform(event)
        try:
            result = await QQGroupManageAPI(platform.client).list_join_requests(
                event.get_group_id(),
                cursor=cursor,
                limit=limit or int(self._config("join_request_page_size", 20)),
            )
        except Exception as exc:
            return f"拉取入群申请失败：{exc}"
        items = result.get("list", []) if isinstance(result, dict) else []
        if not items:
            return "当前没有待处理的入群申请。"
        text = "\n\n".join(format_request(item, index) for index, item in enumerate(items, 1))
        next_cursor = result.get("next_cursor", "") if isinstance(result, dict) else ""
        if next_cursor:
            text += f"\n\n下一页 cursor：{next_cursor}"
        return text

    @filter.llm_tool(name="qq_group_review_join_request")
    async def review_join_request_tool(
        self,
        event: Any,
        member_openid: str,
        join_request_id: str,
        action: str,
        reject_reason: str = "",
    ) -> str:
        """同意或拒绝当前 QQ 群的某个入群申请，仅群管可用。

        Args:
            member_openid(string): 申请人的群成员 OpenID
            join_request_id(string): 入群申请 ID
            action(string): approve 表示同意，decline 表示拒绝
            reject_reason(string): 拒绝理由，同意时留空
        """
        event = resolve_tool_event(event)
        if not self._is_qq_group(event):
            return "当前场景不是 QQ 官方机器人群聊，无法审批入群申请。"
        if not self._event_feature_setting(event, "enable_join_review_tool"):
            return "入群申请审批工具已关闭。"
        action = action.strip().lower()
        if action not in {"approve", "decline"}:
            return "action 只能是 approve 或 decline。"
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
            return f"审批失败：{exc}"
        if action == "approve":
            return "已同意入群申请，请根据用户语境自然回复。"
        return "已拒绝入群申请，请根据用户语境自然回复。"

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

    def _migrate_config_layout(self) -> None:
        """Move the old flat plugin config into the grouped dashboard layout once."""
        try:
            layout_version = int(self.config.get("config_layout_version", 0))
        except (TypeError, ValueError):
            layout_version = 0
        if layout_version >= 1:
            return
        for key in LEGACY_CONFIG_KEYS:
            if key not in self.config:
                continue
            section_name = CONFIG_SECTIONS[key]
            section = self.config.get(section_name)
            if not isinstance(section, dict):
                section = {}
                self.config[section_name] = section
            section[key] = self.config[key]
        self.config["config_layout_version"] = 1
        save = getattr(self.config, "save_config", None)
        if callable(save):
            save()

    def _config(self, key: str, default: Any = None) -> Any:
        section_name = CONFIG_SECTIONS.get(key)
        section = self.config.get(section_name, {}) if section_name else {}
        if isinstance(section, dict) and key in section:
            return section[key]
        return self.config.get(key, default)

    def _set_config(self, key: str, value: Any) -> None:
        section_name = CONFIG_SECTIONS.get(key)
        if section_name:
            section = self.config.get(section_name)
            if not isinstance(section, dict):
                section = {}
                self.config[section_name] = section
            section[key] = value
        else:
            self.config[key] = value
        if key in LEGACY_CONFIG_KEYS and key in self.config:
            self.config[key] = value
        save = getattr(self.config, "save_config", None)
        if callable(save):
            save()

    def _feature_setting(
        self,
        key: str,
        group_umo: str,
        default: bool = True,
    ) -> bool:
        global_value = bool(self._config(key, default))
        if not self._config("enable_per_group_feature_settings", False):
            return global_value
        getter = getattr(self.storage, "group_feature_override", None)
        override = getter(group_umo, key) if callable(getter) else None
        return global_value if override is None else override

    def _event_group_umo(self, event: AstrMessageEvent) -> str:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if umo:
            return umo
        get_platform_id = getattr(event, "get_platform_id", None)
        platform_id = str(get_platform_id() if callable(get_platform_id) else "")
        return self._group_umo(platform_id, event.get_group_id())

    def _event_feature_setting(
        self,
        event: AstrMessageEvent,
        key: str,
        default: bool = True,
    ) -> bool:
        if not self._config("enable_per_group_feature_settings", False):
            return bool(self._config(key, default))
        return self._feature_setting(key, self._event_group_umo(event), default)

    @staticmethod
    def _qq_member_role(event: AstrMessageEvent) -> str:
        message_obj = getattr(event, "message_obj", None)
        raw_message = getattr(message_obj, "raw_message", None)
        author = getattr(raw_message, "author", None)
        role: Any = getattr(author, "member_role", "")
        if not role:
            raw_data = getattr(raw_message, "raw_data", None)
            if isinstance(raw_data, dict):
                raw_author = raw_data.get("author") or {}
                if isinstance(raw_author, dict):
                    role = raw_author.get("member_role", "")
            elif isinstance(raw_message, dict):
                raw_author = raw_message.get("author") or {}
                if isinstance(raw_author, dict):
                    role = raw_author.get("member_role", "")
        role = getattr(role, "value", role)
        normalized = str(role or "").strip().lower()
        return normalized if normalized in {"owner", "admin", "member"} else ""

    def _can_manage_plugin_admins(self, event: AstrMessageEvent) -> bool:
        if self._is_astr_admin(event):
            return True
        role = self._qq_member_role(event)
        if role == "owner":
            return bool(
                self._config("allow_group_owner_manage_plugin_admins", False)
            )
        if role == "admin":
            return bool(
                self._config("allow_group_admin_manage_plugin_admins", False)
            )
        return False

    @staticmethod
    def _is_astr_admin(event: AstrMessageEvent) -> bool:
        return event.get_sender_id() == event.get_self_id() or event.is_admin()

    def _platform(self, event: AstrMessageEvent) -> Any:
        platform = self.context.get_platform_inst(event.get_platform_id())
        if platform is None or not hasattr(platform, "client"):
            raise RuntimeError("找不到当前 QQ 官方平台实例")
        return platform

    def _is_qq_group(self, event: AstrMessageEvent) -> bool:
        return (
            event.get_platform_name() in QQ_PLATFORMS
            and bool(event.get_group_id())
            and self._umo_enabled(
                str(getattr(event, "unified_msg_origin", "") or "")
            )
        )

    def _umo_enabled(self, umo: str) -> bool:
        configured = self._config("enabled_group_umos", []) or []
        if isinstance(configured, str):
            configured = [configured]
        whitelist = {
            str(item).strip()
            for item in configured
            if str(item).strip()
        }
        return not whitelist or umo in whitelist

    @staticmethod
    def _group_umo(platform_id: str, group_openid: str) -> str:
        return f"{platform_id}:GroupMessage:{group_openid}"

    def _can_manage(self, event: AstrMessageEvent) -> bool:
        get_admins = getattr(self.storage, "group_admins", None)
        group_admins = get_admins(event.get_group_id()) if callable(get_admins) else []
        return (
            event.get_sender_id() == event.get_self_id()
            or event.is_admin()
            or event.get_sender_id() in group_admins
            or self._qq_member_role(event) in {"owner", "admin"}
        )

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
        maximum = max(1, int(self._config("max_mute_seconds", 2592000)))
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
        state_class = self._parser_state_class
        if state_class is not None:
            for attr, parser in self._owned_parser_methods.items():
                if getattr(state_class, attr, None) is parser:
                    delattr(state_class, attr)
        self._owned_parser_methods.clear()
        self._parser_state_class = None


def render_member_notice(template: str, member_openid: str, *, can_at: bool) -> str:
    """Render the only supported notice placeholder: {member_at}."""
    if can_at and member_openid:
        member_value = f'<qqbot-at-user id="{member_openid}" />'
    else:
        member_value = member_openid or "未知成员"
    return template.replace("{member_at}", member_value)


def extract_mute_duration(
    message_text: str,
    default_duration: str,
) -> str:
    """Extract duration without letting command executors turn mentions into args."""
    text = re.sub(r"^/?禁言\s*", "", message_text.strip())
    text = re.sub(r"<qqbot-at-user\b[^>]*/?>", "", text, flags=re.I)
    text = re.sub(r"<@!?[^>]+>", "", text).strip()
    text = re.sub(r"(?<!\S)@\S+", "", text).strip()
    return text or default_duration.strip() or "1分"
