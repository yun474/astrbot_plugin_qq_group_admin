from __future__ import annotations

import asyncio
import re
import secrets
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.star.session_plugin_manager import SessionPluginManager

from .api import QQGroupManageAPI
from .callback_guard import ReviewCallbackGuard
from .storage import PluginStorage
from .group_config import GroupConfig
from .keyword_rules import JOIN_REGEX_KEYS, compile_join_pattern, match_join_rules
from .settings_menu import WORD_LISTS, KEYWORD_VALUES, keyword_summary, word_page

PLUGIN_NAME = "astrbot_plugin_qq_group_admin"
QQ_PLATFORMS = {"qq_official", "qq_official_webhook"}
GROUP_MEMBER_INTENT = 1 << 24
GROUP_AND_C2C_INTENT = 1 << 25
INTERACTION_INTENT = 1 << 26
LIFECYCLE_INTENTS = GROUP_MEMBER_INTENT | GROUP_AND_C2C_INTENT | INTERACTION_INTENT
REVIEW_CALLBACK_RE = re.compile(
    r"^qqga:([0-9a-f]{32}):(approve|decline):(native|assigned|shared)$"
)
LIFECYCLE_EVENTS = (
    "group_join_request",
    "group_member_add",
    "group_member_remove",
)
ACTION_RE = re.compile(r"^/?(同意|通过|拒绝|驳回)(?:\s+(.+))?$", re.S)
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
    FeatureDefinition(
        "入群关键词审批", "enable_join_keyword_review", "关键词管理", default=False
    ),
    FeatureDefinition("违禁词禁言", "enable_keyword_mute", "关键词管理", default=False),
    FeatureDefinition(
        "违禁词撤回", "enable_keyword_recall", "关键词管理", default=False
    ),
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
    "strict_llm_permissions": "llm_tool_settings",
    "max_mute_seconds": "limit_settings",
    "enable_join_keyword_review": "keyword_settings",
    "join_whitelist_words": "keyword_settings",
    "join_blacklist_words": "keyword_settings",
    "join_keyword_priority": "keyword_settings",
    "join_keyword_match_mode": "keyword_settings",
    "enable_keyword_mute": "keyword_settings",
    "enable_keyword_recall": "keyword_settings",
    "mute_keywords": "keyword_settings",
    "keyword_mute_duration": "keyword_settings",
}
LEGACY_CONFIG_KEYS = tuple(
    key
    for key in CONFIG_SECTIONS
    if CONFIG_SECTIONS[key] != "keyword_settings"
    and key
    not in {
        "enable_per_group_feature_settings",
        "allow_group_owner_manage_plugin_admins",
        "allow_group_admin_manage_plugin_admins",
        "strict_llm_permissions",
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
                f"群管功能 {feature.name} {next_action}",
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
        "天": 86400,
        "日": 86400,
        "d": 86400,
        "小时": 3600,
        "时": 3600,
        "h": 3600,
        "分钟": 60,
        "分": 60,
        "m": 60,
        "秒": 1,
        "s": 1,
    }
    matches = list(TIME_PART_RE.finditer(text))
    if not matches or "".join(match.group(0) for match in matches).replace(
        " ", ""
    ) != text.replace(" ", ""):
        raise ValueError("时间格式错误，可用 30秒、10分、2小时、1天2小时；纯数字按分钟")
    return sum(int(match.group(1)) * units[match.group(2).lower()] for match in matches)


def intent_value(value: Any) -> int:
    if isinstance(value, int):
        return value
    raw = getattr(value, "value", 0)
    return raw if isinstance(raw, int) else 0


def contains_keyword(text: str, words: list[str]) -> bool:
    """Match literal substrings, ignoring empty entries and letter case."""
    text = text.casefold()
    return any(word.strip().casefold() in text for word in words if word.strip())


def join_keyword_decision(
    item: dict[str, Any],
    whitelist: list[str],
    blacklist: list[str],
    priority: str,
    mode: str = "包含匹配",
) -> bool | None:
    """Inspect answers only; questions, nicknames and verification messages are excluded."""
    if item.get("auto_approved"):
        return None
    answers = [
        qa.get("answer") or ""
        for qa in (item.get("verify_info") or {}).get("review_qa_list") or []
    ]
    rules = [(False, blacklist), (True, whitelist)]
    if priority == "白词优先":
        rules.reverse()
    if mode == "正则匹配":
        return match_join_rules(answers, rules)
    for approve, words in rules:
        if any(contains_keyword(answer, words) for answer in answers):
            return approve
    return None


def format_request(item: dict[str, Any], *, markdown: bool = False) -> str:
    lines = [
        "入群申请已自动通过" if item.get("auto_approved") else "新的入群申请",
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
        lines.append(
            f"自动审批策略：{item['auto_approved'].get('strategy_id') or '已自动通过'}"
        )
    if markdown:
        # All fields come from external users/API payloads, not trusted Markdown.
        fields = [
            re.sub(r"([\\`*_{}\[\]()#+.!|<>~-])", r"\\\1", line)
            for field in lines[1:]
            for line in field.splitlines()
        ]
        title = "入群申请已自动通过" if item.get("auto_approved") else "入群申请"
        return f"# 📨 {title}\n\n" + "  \n".join(fields)
    return "\n".join(lines)


def format_apply_source(value: Any) -> str:
    source = str(value or "").strip()
    if not source:
        return "未提供"
    return APPLY_SOURCE_NAMES.get(source.lower(), "其他来源")


def review_keyboard(
    callbacks: dict[str, dict[str, str]], admin_ids: list[str]
) -> dict[str, Any]:
    """Restrict both rows in QQ; assigned admins are also rechecked on callback."""

    def button(token: str, action: str, audience: str) -> dict[str, Any]:
        label = ("群管" if audience == "native" else "授权") + (
            "同意" if action == "approve" else "拒绝"
        )
        return {
            "id": f"qqga-{action}-{audience}",
            "render_data": {
                "label": label,
                "visited_label": label,
                "style": 1 if action == "approve" else 0,
            },
            "action": {
                "type": 1,
                "permission": {"type": 1}
                if audience == "native"
                else {"type": 0, "specify_user_ids": admin_ids},
                "data": f"qqga:{token}:{action}:{audience}",
                "unsupport_tips": "请更新 QQ 客户端，或引用申请通知回复 同意 / 拒绝",
            },
        }

    rows = [
        {
            "buttons": [
                button(token, binding["action"], audience)
                for token, binding in callbacks.items()
                if binding["audience"] == audience
            ]
        }
        for audience in ("native", "assigned")
        if audience == "native" or admin_ids
    ]
    return {"content": {"rows": rows}}


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
            weekdays = (
                "、".join(
                    f"周{weekday_names.get(day, day)}"
                    for day in rule.get("weekdays", [])
                )
                or "未指定星期"
            )
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
        "@机器人后直接发送下列指令，无需添加 `/`；也可使用 AstrBot 中配置的唤醒词。\n\n"
        "## 成员管理\n\n"
        "> `禁言 @用户 [时间]`  \n"
        f"> 禁言成员；不填时间默认 **{default_duration}**\n\n"
        "> `解禁 @用户`  \n"
        "> 解除成员禁言\n\n"
        "> `禁言状态`  \n"
        "> 查看全员禁言规则及被禁言成员\n\n"
        "## 群管管理\n\n"
        "> `添加群管 @用户`  \n"
        "> 添加本群插件群管；默认仅 AstrBot 管理员可用\n\n"
        "> `删除群管 @用户`  \n"
        "> 删除本群插件群管；可在配置中授权 QQ 群主或群管理员\n\n"
        "> `群管列表`  \n"
        "> 查看本群群管\n\n"
        "> `群管功能`  \n"
        "> 查看功能开关；点击蓝色状态填入切换指令，发送后生效\n\n"
        "## 时间格式\n\n"
        "支持 `30秒`、`10分`、`2小时`、`1天2小时`  \n"
        "纯数字按分钟处理，例如 `30` 表示 **30分钟**\n\n"
        "## 入群审批\n\n"
        "点击申请通知下方的同意 / 拒绝按钮，直接完成审批。\n\n"
        "第一行「群管同意 / 群管拒绝」：QQ 群主、QQ 群管理员可操作。\n\n"
        "第二行「授权同意 / 授权拒绝」：发卡时的 AstrBot 管理员和本群插件群管可操作，点击时会再次检查授权。\n\n"
        "拒绝并填写理由时，可引用申请通知回复 `拒绝 理由`。\n\n"
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


def qq_field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def review_quote(event: AstrMessageEvent) -> set[str] | None:
    """Read only the immediate quote's IDs, never the current message's msg_idx."""
    reply = next((p for p in event.get_messages() if isinstance(p, Reply)), None)
    references = {str(reply.id)} if reply is not None and reply.id else set()

    # Older adapters may preserve QQ's quote fields without building a Reply.
    raw = getattr(event.message_obj, "raw_message", None)
    quoted = reply is not None
    for source in (raw, qq_field(raw, "raw_data")):
        reference_id = qq_field(qq_field(source, "message_reference"), "message_id")
        if reference_id:
            quoted = True
            references.add(str(reference_id))
        ext = qq_field(qq_field(source, "message_scene"), "ext")
        if isinstance(ext, list):
            for entry in ext:
                if not isinstance(entry, str):
                    continue
                key, separator, value = entry.partition("=")
                if separator and key.strip() == "ref_msg_idx":
                    quoted = True
                    if value.strip():
                        references.add(value.strip())
        if str(qq_field(source, "message_type")) == "103":
            quoted = True
            elements = qq_field(source, "msg_elements")
            if isinstance(elements, list) and elements:
                for key in ("id", "message_id", "msg_idx"):
                    value = qq_field(elements[0], key)
                    if value:
                        references.add(str(value))
    return references if quoted else None


@register(
    PLUGIN_NAME,
    "yun474",
    "QQ 官方机器人群管理：禁言、入群申请审批、分群管理员与 LLM 工具",
    "2.7.0",
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
        unresolved = GroupConfig(self.config).migrate(self.storage, self.context)
        if unresolved:
            logger.warning(
                "[%s] 以下旧分群记录需要在 UMO 列表核对：%s", PLUGIN_NAME, unresolved
            )
        self._patched: dict[str, dict[str, Any]] = {}
        self._patch_task: asyncio.Task | None = None
        self._parser_state_class: Any = None
        self._owned_parser_methods: dict[str, Any] = {}
        self._reviews_inflight: set[tuple[str, str, str]] = set()
        self._callback_guard = ReviewCallbackGuard()
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
            if (
                platform_id in self._patched
                and self._patched[platform_id]["client"] is not client
            ):
                # Platform reload keeps its ID but replaces the SDK client.
                self._restore_client_handlers(self._patched.pop(platform_id))
            if platform_id not in self._patched:
                old_handlers = {
                    name: getattr(client, name, None)
                    for name in (
                        "on_group_join_request",
                        "on_group_member_add",
                        "on_group_member_remove",
                        "on_interaction_create",
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

                async def interaction_handler(
                    interaction: Any,
                    pid: str = platform_id,
                    original: Any = old_handlers["on_interaction_create"],
                ) -> None:
                    if await self._handle_review_interaction(pid, interaction):
                        return
                    if original is not None:
                        await original(interaction)

                setattr(client, "on_interaction_create", interaction_handler)
                self._patched[platform_id] = {
                    "client": client,
                    "old_handlers": old_handlers,
                    "owned_handlers": {
                        name: getattr(client, name) for name in old_handlers
                    },
                    "connections": {},
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

                def join_request_parser(
                    payload: dict[str, Any], c: Any = client
                ) -> None:
                    data = dict(payload.get("d", {}) or {})
                    data["_event_id"] = str(payload.get("id") or "")
                    c.ws_dispatch("group_join_request", data)

                def member_add_parser(payload: dict[str, Any], c: Any = client) -> None:
                    data = dict(payload.get("d", {}) or {})
                    data["_event_id"] = str(payload.get("id") or "")
                    c.ws_dispatch("group_member_add", data)

                def member_remove_parser(
                    payload: dict[str, Any], c: Any = client
                ) -> None:
                    data = dict(payload.get("d", {}) or {})
                    data["_event_id"] = str(payload.get("id") or "")
                    c.ws_dispatch("group_member_remove", data)

                parsers = {
                    "group_join_request": join_request_parser,
                    "group_member_add": member_add_parser,
                    "group_member_remove": member_remove_parser,
                }
                patch_state["connections"][id(connection)] = (
                    connection,
                    {name: connection.parser.get(name) for name in parsers},
                    parsers,
                )
                connection.parser.update(parsers)
                logger.info("[%s] 已接入 QQ 入群申请与成员进退群事件", PLUGIN_NAME)

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
                "[%s] 已启用群生命周期和按钮回调 Intents，当前值：%s",
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
        if not await self._sdk_group_enabled(group_umo):
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
        template = str(
            self._group_setting(template_key, group_umo, default) or ""
        ).strip()
        if not template:
            return
        platform = self.context.get_platform_inst(platform_id)
        if platform is None:
            return
        can_at = notice_type == "member_join"
        content = render_member_notice(
            template,
            member_openid,
            can_at=can_at,
            appid=str(getattr(platform, "appid", "") or ""),
        )
        if not content.strip():
            return
        api = QQGroupManageAPI(platform.client)
        try:
            if "<qqbot-at-user" in content or "![头像 #" in content:
                try:
                    await api.send_group_markdown(
                        group_openid,
                        content,
                    )
                except Exception:
                    logger.warning(
                        "[%s] 成员通知 Markdown 发送失败，降级为普通文本",
                        PLUGIN_NAME,
                        exc_info=True,
                    )
                    fallback = render_member_notice(
                        template,
                        member_openid,
                        can_at=False,
                    ).strip()
                    if fallback:
                        await api.send_group_text(group_openid, fallback)
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
        if not await self._sdk_group_enabled(group_umo):
            return
        request_key = (
            platform_id,
            group_openid,
            str(item.get("join_request_id") or ""),
        )
        if self.storage.is_reviewed(*request_key):
            return
        auto_approved = bool(item.get("auto_approved"))
        if auto_approved:
            self._record_reviewed_request(*request_key)
        notice_enabled = self._feature_setting("enable_join_notice", group_umo)
        auto_review_enabled = self._feature_setting(
            "enable_join_keyword_review", group_umo, False
        )
        if not notice_enabled and not auto_review_enabled:
            return
        platform = self.context.get_platform_inst(platform_id)
        if platform is None:
            return
        if auto_review_enabled and await self._auto_review_join_request(
            platform_id, platform.client, item
        ):
            return
        if not notice_enabled:
            return
        stored = dict(item)
        stored["platform_id"] = platform_id
        stored["group_openid"] = group_openid
        admin_ids = []
        pending_key = ""
        if not auto_approved:
            # Reservation is synchronous: a replay cannot replace the first
            # notification's tokens while its send is still awaiting QQ.
            if (
                self.storage.find_pending_by_join_request_id(
                    str(item.get("join_request_id") or ""), group_openid, platform_id
                )
                is not None
            ):
                return
            admin_ids = self._callback_admin_ids(platform_id, group_openid)
            stored["review_callbacks"] = {
                secrets.token_hex(16): {"action": action, "audience": audience}
                for audience in ("native", "assigned")
                if audience == "native" or admin_ids
                for action in ("approve", "decline")
            }
            pending_key = self.storage.reserve_pending(stored)
        content = format_request(item, markdown=True)
        review_enabled = not auto_approved and self._feature_setting(
            "enable_join_reply_review",
            group_umo,
        )
        if review_enabled:
            content += "\n\n点击下方按钮直接审批；或引用本消息：同意/拒绝 理由。"
        try:
            api = QQGroupManageAPI(platform.client)
            try:
                result = await api.send_group_markdown(
                    group_openid,
                    content,
                    keyboard=review_keyboard(stored["review_callbacks"], admin_ids)
                    if review_enabled
                    else None,
                )
            except Exception:
                logger.warning(
                    "[%s] 入群申请 Markdown 按钮发送失败，降级为纯文本通知",
                    PLUGIN_NAME,
                    exc_info=True,
                )
                fallback = format_request(item)
                if review_enabled:
                    fallback += (
                        "\n\n按钮暂不可用，可引用本消息回复：同意 / 拒绝 [理由]。"
                    )
                result = await api.send_group_text(group_openid, fallback)
            message_id = self._response_id(result)
            ref_idx = str(qq_field(qq_field(result, "ext_info"), "ref_idx") or "")
            if pending_key and (message_id or ref_idx):
                pending_key = self.storage.bind_pending_message(
                    pending_key,
                    message_id,
                    ref_idx,
                )
        except (Exception, asyncio.CancelledError) as exc:
            if pending_key:
                self.storage.remove_pending(pending_key)
            if isinstance(exc, asyncio.CancelledError):
                raise
            logger.exception("[%s] 转发入群申请失败", PLUGIN_NAME)

    async def _auto_review_join_request(
        self, platform_id: str, client: Any, item: dict[str, Any]
    ) -> bool:
        group_umo = self._group_umo(platform_id, str(item.get("group_openid") or ""))
        try:
            approve = join_keyword_decision(
                item,
                self._group_setting("join_whitelist_words", group_umo, []),
                self._group_setting("join_blacklist_words", group_umo, []),
                self._group_setting("join_keyword_priority", group_umo, "黑词优先"),
                self._group_setting("join_keyword_match_mode", group_umo, "包含匹配"),
            )
        except (ValueError, TimeoutError):
            logger.exception("[%s] 入群正则无效或匹配超时，转交人工审批", PLUGIN_NAME)
            return False
        if approve is None:
            return False
        group_id = str(item.get("group_openid") or "")
        member_id = str(item.get("member_openid") or "")
        request_id = str(item.get("join_request_id") or "")
        if not member_id or not request_id:
            return False
        request_key = (platform_id, group_id, request_id)
        if request_key in self._reviews_inflight:
            return True
        self._reviews_inflight.add(request_key)
        try:
            try:
                await QQGroupManageAPI(client).review_join_request(
                    group_id, member_id, request_id, approve=approve
                )
            except Exception:
                logger.exception(
                    "[%s] 入群关键词审批失败，保留人工审批流程", PLUGIN_NAME
                )
                return False
            self._record_reviewed_request(*request_key)
            return True
        finally:
            self._reviews_inflight.discard(request_key)

    def _record_reviewed_request(
        self, platform_id: str, group_openid: str, join_request_id: str
    ) -> None:
        try:
            self.storage.remove_reviewed_request(
                platform_id, group_openid, join_request_id
            )
        except Exception:
            logger.exception("[%s] 审批已完成，但保存完成记录失败", PLUGIN_NAME)

    def _callback_admin_ids(self, platform_id: str, group_openid: str) -> list[str]:
        config = self.context.get_config(self._group_umo(platform_id, group_openid))
        return sorted(
            {
                str(user_id)
                for user_id in (
                    *config.get("admins_id", []),
                    *self.storage.group_admins(group_openid),
                )
                if str(user_id)
            }
        )

    async def _handle_review_interaction(
        self, platform_id: str, interaction: Any
    ) -> bool:
        """Consume our callbacks and authorize the clicker, including on old buttons."""
        resolved = getattr(getattr(interaction, "data", None), "resolved", None)
        data = str(getattr(resolved, "button_data", "") or "")
        if not data.startswith("qqga:"):
            return False
        platform = self.context.get_platform_inst(platform_id)
        if platform is None:
            return True
        api = QQGroupManageAPI(platform.client)
        interaction_id = str(getattr(interaction, "id", "") or "")
        if not interaction_id:
            return True

        async def acknowledge(code: int) -> None:
            try:
                # ACK is independent of the potentially slower approval request.
                await asyncio.wait_for(
                    api.acknowledge_interaction(interaction_id, code), 3
                )
            except Exception:
                if self._callback_guard.should_log_error("ack"):
                    logger.exception("[%s] 回应审批按钮失败", PLUGIN_NAME)

        match = REVIEW_CALLBACK_RE.fullmatch(data)
        group = str(getattr(interaction, "group_openid", "") or "")
        sender = str(getattr(interaction, "group_member_openid", "") or "")
        if (
            not match
            or not group
            or not sender
            or getattr(interaction, "type", None) != 11
            or getattr(interaction, "chat_type", None) != 1
        ):
            await acknowledge(4)
            return True
        token, action, audience = match.groups()
        if getattr(resolved, "button_id", None) != f"qqga-{action}-{audience}":
            await acknowledge(4)
            return True
        umo = self._group_umo(platform_id, group)
        if not await self._sdk_group_enabled(umo) or not self._feature_setting(
            "enable_join_reply_review", umo
        ):
            await acknowledge(4)
            return True
        assigned_admin = sender in self._callback_admin_ids(platform_id, group)
        matched = self.storage.find_pending_by_token(token)
        if matched is None:
            await acknowledge(3)  # Completed, expired or from a removed notification.
            return True
        _, pending = matched
        if (
            pending.get("platform_id") != platform_id
            or pending.get("group_openid") != group
        ):
            await acknowledge(4)
            return True
        callbacks = pending.get("review_callbacks")
        native_button = False
        if callbacks is not None:
            # Bind BOTH operation and audience to an independently generated token.
            # A legacy token or an edited assigned button cannot become native.
            if callbacks.get(token) != {"action": action, "audience": audience}:
                await acknowledge(4)
                return True
            native_button = audience == "native"
            if not native_button and not assigned_admin:
                await acknowledge(5)
                return True
        denial = self._callback_guard.check_click(
            platform_id,
            group,
            sender,
            assigned_admin=assigned_admin or native_button,
        )
        if denial is not None:
            await acknowledge(denial)
            return True
        request_key = (platform_id, group, str(pending["join_request_id"]))
        if request_key in self._reviews_inflight:
            await acknowledge(2)
            return True
        self._reviews_inflight.add(request_key)
        try:
            if not assigned_admin and not native_button:
                # Legacy notifications retain live role verification. New native
                # buttons rely on QQ enforcing permission.type=1 before dispatch.
                if not self._callback_guard.start_lookup():
                    await acknowledge(2)
                    return True
                try:
                    member = await asyncio.wait_for(
                        api.get_group_member_info(group, sender), 2
                    )
                except Exception:
                    if self._callback_guard.should_log_error("lookup"):
                        logger.exception(
                            "[%s] 无法验证按钮点击者身份，请检查获取群成员信息接口权限",
                            PLUGIN_NAME,
                        )
                    await acknowledge(1)
                    return True
                finally:
                    self._callback_guard.finish_lookup()
                if (
                    not isinstance(member, dict)
                    or member.get("member_openid") != sender
                    or member.get("member_role") not in {"owner", "admin"}
                ):
                    self._callback_guard.remember_denied(platform_id, group, sender)
                    await acknowledge(4)
                    return True
            # A quoted reply may have resolved the request during the role lookup.
            if self.storage.find_pending_by_token(token) is None:
                await acknowledge(3)
                return True
            await acknowledge(0)
            try:
                await api.review_join_request(
                    group,
                    str(pending["member_openid"]),
                    str(pending["join_request_id"]),
                    approve=action == "approve",
                )
            except Exception:
                logger.exception("[%s] 按钮审批入群申请失败", PLUGIN_NAME)
                result = "入群申请审批失败，请查看机器人日志并核实申请状态。"
            else:
                self._record_reviewed_request(*request_key)
                result = (
                    "已同意入群申请。" if action == "approve" else "已拒绝入群申请。"
                )
            try:
                await api.send_group_text(
                    group,
                    result,
                    event_id=str(
                        getattr(interaction, "event_id", "") or interaction_id
                    ),
                )
            except Exception:
                # The approval may already have succeeded; never repeat it for a send failure.
                logger.exception("[%s] 发送按钮审批结果失败", PLUGIN_NAME)
        finally:
            self._reviews_inflight.discard(request_key)
        return True

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=100)
    async def keyword_mute(self, event: AstrMessageEvent) -> None:
        if not self._is_qq_group(event):
            return
        mute_enabled = self._event_feature_setting(event, "enable_keyword_mute", False)
        recall_enabled = self._event_feature_setting(
            event, "enable_keyword_recall", False
        )
        if not mute_enabled and not recall_enabled:
            return
        sender = event.get_sender_id()
        if not sender or sender == event.get_self_id():
            return
        if (
            event.is_at_or_wake_command
            and re.match(r"^群管功能(?:\s|$)", event.get_message_str().strip())
            and self._can_manage(event)
        ):
            return  # Let authorized users edit the word list without punishing its contents.
        text = "".join(
            part.text for part in event.get_messages() if isinstance(part, Plain)
        )
        if not contains_keyword(text, self._event_setting(event, "mute_keywords", [])):
            return
        if not await self._sdk_group_enabled(self._event_group_umo(event)):
            return
        quote = review_quote(event)
        if (
            quote
            and self._can_manage(event)
            and self._event_feature_setting(event, "enable_join_reply_review")
            and self._review_action_match(event) is not None
            and self.storage.find_pending_by_quote(
                quote, event.get_platform_id(), event.get_group_id()
            )
            is not None
        ):
            # Only a real, authorized review takes precedence over the word filter.
            await self.reply_review(event)
            return
        # Consume matched messages even if QQ rejects the mute, keeping commands/LLM silent.
        event.stop_event()
        if recall_enabled:
            try:
                # Never use a quoted message ID or a session's last message ID.
                message_id = getattr(event.message_obj, "message_id", "")
                if not message_id:
                    raise ValueError("当前消息缺少消息 ID，无法撤回")
                await asyncio.wait_for(
                    QQGroupManageAPI(self._platform(event).client).recall_group_message(
                        event.get_group_id(), str(message_id)
                    ),
                    timeout=10,
                )
            except Exception:
                logger.exception("[%s] 违禁词消息撤回失败", PLUGIN_NAME)
        if not mute_enabled:
            return
        try:
            seconds = parse_duration(
                str(self._event_setting(event, "keyword_mute_duration", "10分"))
            )
            self._validate_duration(seconds, self._event_group_umo(event))
            if seconds == 0:
                raise ValueError("违禁词禁言时长必须大于 0")
            await self._mute(event, event.get_group_id(), sender, seconds)
        except Exception:
            logger.exception("[%s] 违禁词自动禁言失败", PLUGIN_NAME)

    def _review_action_match(self, event: AstrMessageEvent) -> re.Match | None:
        action_text = review_action_text(event)
        action_match = ACTION_RE.fullmatch(action_text)
        if action_match is None:
            config = self.context.get_config(self._event_group_umo(event))
            for prefix in config.get("wake_prefix", []):
                if prefix and action_text.startswith(prefix):
                    action_match = ACTION_RE.fullmatch(
                        action_text[len(prefix) :].strip()
                    )
                    break
        return action_match

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=10)
    async def reply_review(self, event: AstrMessageEvent) -> None:
        if not self._is_qq_group(event) or not self._event_feature_setting(
            event,
            "enable_join_reply_review",
        ):
            return
        quote = review_quote(event)
        if quote is None:
            return
        action_match = self._review_action_match(event)
        if action_match is None:
            return
        try:
            matched = self.storage.find_pending_by_quote(
                quote, event.get_platform_id(), event.get_group_id()
            )
            if matched is None:
                result = (
                    "这条引用没有提供原消息 ID 或有效引用索引，请使用申请卡片上的审批按钮。"
                    if not quote
                    else "引用索引未唯一匹配到本群待审批申请，可能已处理或过期，请使用申请卡片上的审批按钮。"
                )
            elif not self._can_manage(event):
                result = "你没有本群群管权限。"
            else:
                pending_key, pending = matched
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
                    result = f"审批失败：{exc}"
                else:
                    self.storage.remove_pending(pending_key)
                    result = (
                        "已同意入群申请。"
                        if approve
                        else f"已拒绝入群申请。{(' 理由：' + reason) if reason else ''}"
                    )
            # Send before stopping: yielding a result after stop_event can either
            # skip sending or replace the STOP result on older AstrBot versions.
            await event.send(event.plain_result(result))
        finally:
            event.stop_event()

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
            yield event.plain_result("请艾特要禁言的成员，例如：禁言 @用户 [时间]")
            return
        try:
            time = extract_mute_duration(
                event.get_message_str(),
                str(
                    self._event_setting(event, "default_mute_duration", "1分") or "1分"
                ),
            )
            seconds = parse_duration(time)
            self._validate_duration(seconds, self._event_group_umo(event))
        except Exception as exc:
            yield event.plain_result(f"禁言失败：{exc}")
            return
        failure = await self._mute_members(event, targets, seconds)
        if failure:
            yield event.plain_result(failure)
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
            yield event.plain_result("请艾特要解除禁言的成员，例如：解禁 @用户")
            return
        failure = await self._mute_members(event, targets, 0)
        if failure:
            yield event.plain_result(failure)
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
        try:
            added = self.storage.update_group_admins(
                event.get_group_id(), targets, add=True
            )
        except OSError:
            logger.exception("[%s] 保存群管名单失败", PLUGIN_NAME)
            yield event.plain_result(
                "群管名单保存失败，本次修改未生效，请检查存储权限和磁盘空间。"
            )
            return
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
        try:
            removed = self.storage.update_group_admins(
                event.get_group_id(), targets, add=False
            )
        except OSError:
            logger.exception("[%s] 保存群管名单失败", PLUGIN_NAME)
            yield event.plain_result(
                "群管名单保存失败，本次修改未生效，请检查存储权限和磁盘空间。"
            )
            return
        yield event.plain_result(f"已删除 {removed} 名本群群管。")

    @filter.command("群管列表")
    async def list_group_admins(self, event: AstrMessageEvent) -> None:
        if not self._is_qq_group(event):
            yield event.plain_result("该指令仅支持 QQ 官方机器人群聊。")
            return
        if not self._event_feature_setting(event, "enable_group_admin_commands"):
            return
        admins = self.storage.group_admins(event.get_group_id())
        content = "本群群管：\n" + (
            "\n".join(f"- {item}" for item in admins) if admins else "（暂无）"
        )
        content += (
            "\nAstrBot 管理员拥有全局权限；QQ 群主和群管理员默认拥有本群普通群管权限。"
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
            self._event_setting(event, "default_mute_duration", "1分") or "1分"
        )
        yield event.plain_result(
            format_group_admin_help(default_duration)
        ).use_markdown(True)

    @filter.command("群管功能")
    async def group_feature_settings(
        self,
        event: AstrMessageEvent,
        feature_name: str = "",
        action: str = "",
        value: str = "",
    ) -> None:
        """查看或修改当前群的独立功能开关。"""
        if not self._is_qq_group(event):
            yield event.plain_result("该指令仅支持已启用的 QQ 官方机器人群聊。")
            return
        per_group = bool(self._config("enable_per_group_feature_settings", False))
        changing = bool(feature_name or action) and not (
            (feature_name in WORD_LISTS or feature_name in KEYWORD_VALUES)
            and action in {"", "查看"}
        )
        if not per_group and changing and not self._is_astr_admin(event):
            yield event.plain_result("你没有权限更改配置项，别乱动人家的功能啊！")
            return
        if not self._can_manage(event):
            yield event.plain_result("你没有本群群管权限。")
            return

        if feature_name in WORD_LISTS or feature_name in KEYWORD_VALUES:
            # AstrBot's older command parser splits string args on spaces. Read the
            # remaining original text so one phrase stays one word-list entry.
            parts = event.get_message_str().strip().split(maxsplit=3)
            if parts and parts[0] == "群管功能" and len(parts) == 4:
                value = parts[3]
            try:
                result = self._keyword_config_command(
                    event, feature_name, action, value
                )
            except (ValueError, OSError) as exc:
                yield event.plain_result(f"配置未修改：{exc}")
                return
            yield event.plain_result(result).use_markdown(True)
            return

        if feature_name or action:
            feature = FEATURES_BY_NAME.get(feature_name.strip())
            if feature is None:
                available = "、".join(
                    [*(item.name for item in FEATURES), *WORD_LISTS, *KEYWORD_VALUES]
                )
                yield event.plain_result(f"未知功能。可用功能：{available}")
                return
            normalized_action = action.strip()
            if normalized_action in {"开启", "打开", "启用", "开"}:
                enabled = True
            elif normalized_action in {"关闭", "停用", "关"}:
                enabled = False
            else:
                yield event.plain_result(
                    f"用法：群管功能 {feature.name} 开启（或关闭）"
                )
                return
            raw_value = not enabled if feature.inverted else enabled
            try:
                self._save_setting(event, feature.key, raw_value)
            except Exception as exc:
                yield event.plain_result(f"保存功能开关失败：{exc}")
                return
            yield event.plain_result(
                f"{feature.name}已{'开启' if enabled else '关闭'}。"
            )
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
            + "\n\n群 UMO：`"
            + self._event_group_umo(event)
            + "`"
            + keyword_summary(
                {
                    **{
                        key: self._event_setting(event, key, [])
                        for key in WORD_LISTS.values()
                    },
                    "join_keyword_priority": self._event_setting(
                        event, "join_keyword_priority", "黑词优先"
                    ),
                    "join_keyword_match_mode": self._event_setting(
                        event, "join_keyword_match_mode", "包含匹配"
                    ),
                    "keyword_mute_duration": self._event_setting(
                        event, "keyword_mute_duration", "10分"
                    ),
                }
            )
        ).use_markdown(True)

    def _save_setting(self, event: AstrMessageEvent, key: str, value: Any) -> None:
        if self._config("enable_per_group_feature_settings", False):
            GroupConfig(self.config).set_value(
                self._event_group_umo(event), CONFIG_SECTIONS[key], key, value
            )
        else:
            self._set_config(key, value)

    def _keyword_config_command(
        self, event: AstrMessageEvent, name: str, action: str, value: str
    ) -> str:
        value = value.strip()
        mode = self._event_setting(event, "join_keyword_match_mode", "包含匹配")
        if name in WORD_LISTS:
            key = WORD_LISTS[name]
            regex_mode = key in JOIN_REGEX_KEYS and mode == "正则匹配"
            words = list(self._event_setting(event, key, []))
            if action in {"", "查看"}:
                return word_page(name, words, int(value or "1"), mode)
            if action == "清空":
                if value != "确认":
                    raise ValueError(f"清空需发送：群管功能 {name} 清空 确认")
                words = []
            elif action in {"添加", "删除"}:
                if not value:
                    raise ValueError(f"用法：群管功能 {name} {action} 词条")
                # Regex escapes are case-sensitive: \D and \d must remain distinct.
                normalize = str.strip if regex_mode else lambda s: s.strip().casefold()
                normalized = [normalize(word) for word in words]
                if action == "添加":
                    if regex_mode:
                        compile_join_pattern(value)
                    if normalize(value) in normalized:
                        return "该词条已存在。"
                    words.append(value)
                elif normalize(value) in normalized:
                    words = [
                        word for word in words if normalize(word) != normalize(value)
                    ]
                else:
                    raise ValueError("词条不存在，请先查看列表")
            else:
                raise ValueError("支持：查看、添加、删除、清空 确认")
            self._save_setting(event, key, words)
            return f"{name}已保存，当前 {len(words)} 条。"
        key = KEYWORD_VALUES[name]
        if action in {"", "查看"}:
            current = (
                mode
                if key == "join_keyword_match_mode"
                else self._event_setting(event, key)
            )
            return f"{name}：{current}"
        if action != "设置":
            raise ValueError(f"用法：群管功能 {name} 设置 内容")
        if key == "join_keyword_priority":
            if value not in {"白词优先", "黑词优先"}:
                raise ValueError("请选择 白词优先 或 黑词优先")
        elif key == "join_keyword_match_mode":
            if value not in {"包含匹配", "正则匹配"}:
                raise ValueError("请选择 包含匹配 或 正则匹配")
            if value == "正则匹配":
                for word_key in JOIN_REGEX_KEYS:
                    for word in self._event_setting(event, word_key, []):
                        if word.strip():
                            compile_join_pattern(word.strip())
        else:
            seconds = parse_duration(value)
            self._validate_duration(seconds, self._event_group_umo(event))
            if seconds == 0:
                raise ValueError("违禁词禁言时长必须大于 0")
        self._save_setting(event, key, value)
        return f"{name}已保存。"

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
        """禁言或解禁当前 QQ 群的一名普通成员，开启严格权限审查时仅群管可用。

        Args:
            member_openid(string): 被操作成员的群成员 OpenID
            duration(string): 可选禁言时长，如 30秒、10分、2小时、1天；省略时使用插件默认时长，填 0 或 解除表示解禁
        """
        event = resolve_tool_event(event)
        if not self._is_qq_group(event):
            return "当前场景不是 QQ 官方机器人群聊，无法使用群禁言工具。"
        if not self._event_feature_setting(event, "enable_mute_tool"):
            return "QQ 群禁言工具已关闭。"
        if self._event_setting(
            event, "strict_llm_permissions", False
        ) and not self._can_manage(event):
            return "唤醒人没有本群群管权限，严格权限审查已拒绝此次工具调用。"
        try:
            duration = duration.strip() or str(
                self._event_setting(event, "default_mute_duration", "1分") or "1分"
            )
            seconds = parse_duration(duration)
            self._validate_duration(seconds, self._event_group_umo(event))
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
        """解除当前 QQ 群一名普通成员的禁言，开启严格权限审查时仅群管可用。

        Args:
            member_openid(string): 被解除禁言成员的群成员 OpenID
        """
        event = resolve_tool_event(event)
        if not self._is_qq_group(event):
            return "当前场景不是 QQ 官方机器人群聊，无法使用群解禁工具。"
        if not self._event_feature_setting(event, "enable_unmute_tool"):
            return "QQ 群解禁工具已关闭。"
        if self._event_setting(
            event, "strict_llm_permissions", False
        ) and not self._can_manage(event):
            return "唤醒人没有本群群管权限，严格权限审查已拒绝此次工具调用。"
        try:
            await self._mute(event, event.get_group_id(), member_openid, 0)
        except Exception as exc:
            return f"解禁操作失败：{exc}"
        return "已成功执行解禁，请根据用户语境自然回复。"

    @filter.llm_tool(name="qq_group_get_mute_status")
    async def mute_status_tool(self, event: Any) -> str:
        """查询当前 QQ 群的全员禁言规则和被禁言成员列表，开启严格权限审查时仅群管可用。"""
        event = resolve_tool_event(event)
        if not self._is_qq_group(event):
            return "当前场景不是 QQ 官方机器人群聊，无法查询群禁言状态。"
        if not self._event_feature_setting(event, "enable_mute_status_tool"):
            return "QQ 群禁言状态工具已关闭。"
        if self._event_setting(
            event, "strict_llm_permissions", False
        ) and not self._can_manage(event):
            return "唤醒人没有本群群管权限，严格权限审查已拒绝此次工具调用。"
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
        limit: int = 0,
    ) -> str:
        """拉取当前 QQ 群待处理的入群申请列表，开启严格权限审查时仅群管可用。

        Args:
            cursor(string): 分页游标，第一页传空字符串
            limit(number): 拉取条数，范围 1 到 100；省略或填 0 使用插件配置的默认条数
        """
        event = resolve_tool_event(event)
        if not self._is_qq_group(event):
            return "当前场景不是 QQ 官方机器人群聊，无法拉取入群申请。"
        if not self._event_feature_setting(event, "enable_join_list_tool"):
            return "入群申请列表工具已关闭。"
        if self._event_setting(
            event, "strict_llm_permissions", False
        ) and not self._can_manage(event):
            return "唤醒人没有本群群管权限，严格权限审查已拒绝此次工具调用。"
        platform = self._platform(event)
        try:
            result = await QQGroupManageAPI(platform.client).list_join_requests(
                event.get_group_id(),
                cursor=cursor,
                limit=limit
                or int(self._event_setting(event, "join_request_page_size", 20)),
            )
        except Exception as exc:
            return f"拉取入群申请失败：{exc}"
        items = result.get("list", []) if isinstance(result, dict) else []
        if not items:
            return "当前没有待处理的入群申请。"
        # Tool results need the identifiers consumed by the review tool;
        # public notification cards deliberately keep using format_request alone.
        text = "\n\n".join(
            f"{format_request(item)}\n"
            f"member_openid：{item.get('member_openid') or ''}\n"
            f"join_request_id：{item.get('join_request_id') or ''}"
            for item in items
        )
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
        """同意或拒绝当前 QQ 群的某个入群申请，开启严格权限审查时仅群管可用。

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
        if self._event_setting(
            event, "strict_llm_permissions", False
        ) and not self._can_manage(event):
            return "唤醒人没有本群群管权限，严格权限审查已拒绝此次工具调用。"
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

    async def _mute_members(
        self, event: AstrMessageEvent, targets: list[str], seconds: int
    ) -> str | None:
        failures: list[str] = []
        for member_openid in targets:
            try:
                await self._mute(event, event.get_group_id(), member_openid, seconds)
            except Exception as exc:
                failures.append(f"- {member_openid}：{exc}")
        if not failures:
            return None
        action = "解禁" if seconds == 0 else "禁言"
        return (
            f"{action}结果：成功 {len(targets) - len(failures)} 名，"
            f"失败 {len(failures)} 名。\n失败成员：\n" + "\n".join(failures)
        )

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
        expire_at = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
            timespec="seconds"
        )
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
        request_key = (event.get_platform_id(), group_openid, join_request_id)
        if self.storage.is_reviewed(*request_key):
            raise ValueError("该入群申请已处理，请勿重复审批。")
        if request_key in self._reviews_inflight:
            raise ValueError("该入群申请正在处理中，请勿重复审批。")
        self._reviews_inflight.add(request_key)
        try:
            result = await QQGroupManageAPI(
                self._platform(event).client
            ).review_join_request(
                group_openid,
                member_openid,
                join_request_id,
                approve=approve,
                reject_reason=reason,
            )
            self._record_reviewed_request(*request_key)
            return result
        finally:
            self._reviews_inflight.discard(request_key)

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
        if self.config.get("config_layout_version", 0) >= 2:
            management = self.config["group_management"]
            if key == "enable_per_group_feature_settings":
                return management["enabled"]
            section = management["global_settings"].get(CONFIG_SECTIONS.get(key), {})
            if key in section:
                return section[key]
        section_name = CONFIG_SECTIONS.get(key)
        section = self.config.get(section_name, {}) if section_name else {}
        if isinstance(section, dict) and key in section:
            return section[key]
        return self.config.get(key, default)

    def _set_config(self, key: str, value: Any) -> None:
        section_name = CONFIG_SECTIONS.get(key)
        if self.config.get("config_layout_version", 0) >= 2:
            before = deepcopy(dict(self.config))
            try:
                self.config["group_management"]["global_settings"][section_name][
                    key
                ] = value
                GroupConfig(self.config).save()
            except Exception:
                self.config.clear()
                self.config.update(before)
                raise
            return
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
        return bool(self._group_setting(key, group_umo, default))

    def _group_setting(self, key: str, group_umo: str, default: Any = None) -> Any:
        global_value = self._config(key, default)
        if not self._config("enable_per_group_feature_settings", False):
            return global_value
        if self.config.get("config_layout_version", 0) >= 2:
            profile = GroupConfig(self.config).profile(group_umo)
            if profile is not None:
                return profile.get(CONFIG_SECTIONS.get(key), {}).get(key, global_value)
            return global_value
        getter = getattr(self.storage, "group_feature_override", None)
        override = getter(group_umo, key) if callable(getter) else None
        return global_value if override is None else override

    def _event_group_umo(self, event: AstrMessageEvent) -> str:
        return self._group_umo(event.get_platform_id(), event.get_group_id())

    def _event_setting(
        self, event: AstrMessageEvent, key: str, default: Any = None
    ) -> Any:
        if not self._config("enable_per_group_feature_settings", False):
            return self._config(key, default)
        return self._group_setting(key, self._event_group_umo(event), default)

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
                self._event_setting(
                    event, "allow_group_owner_manage_plugin_admins", False
                )
            )
        if role == "admin":
            return bool(
                self._event_setting(
                    event, "allow_group_admin_manage_plugin_admins", False
                )
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
            and self._umo_enabled(self._event_group_umo(event))
        )

    def _umo_enabled(self, umo: str) -> bool:
        configured = self._config("enabled_group_umos", []) or []
        if isinstance(configured, str):
            configured = [configured]
        whitelist = {str(item).strip() for item in configured if str(item).strip()}
        return not whitelist or umo in whitelist

    async def _sdk_group_enabled(self, umo: str) -> bool:
        if not self._umo_enabled(umo):
            return False
        try:
            plugins = self.context.get_config(umo).get("plugin_set", ["*"])
            if plugins != ["*"] and PLUGIN_NAME not in plugins:
                return False
            return await SessionPluginManager.is_plugin_enabled_for_session(
                umo, PLUGIN_NAME
            )
        except Exception:
            logger.exception("[%s] 无法读取会话插件状态，已拒绝 SDK 事件", PLUGIN_NAME)
            return False

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
            member_openid = getattr(mention, "member_openid", None) or getattr(
                mention, "id", None
            )
            if member_openid and str(member_openid) not in targets:
                targets.append(str(member_openid))
        # Fallback for adapters that preserve At components directly.
        for part in event.get_messages():
            if isinstance(part, At) and str(part.qq) not in {
                "qq_official",
                event.get_self_id(),
                "all",
            }:
                if str(part.qq) not in targets:
                    targets.append(str(part.qq))
        return targets[:10]

    def _validate_duration(self, seconds: int, group_umo: str = "") -> None:
        maximum = max(
            1, int(self._group_setting("max_mute_seconds", group_umo, 2592000))
        )
        if seconds < 0 or seconds > maximum:
            raise ValueError(f"禁言时长必须在 0 到 {maximum} 秒之间")

    @staticmethod
    def _response_id(result: Any) -> str:
        if isinstance(result, dict):
            return str(result.get("id") or "")
        return str(getattr(result, "id", "") or "")

    @staticmethod
    def _restore_client_handlers(state: dict[str, Any]) -> None:
        for connection, old_parsers, owned_parsers in state["connections"].values():
            for name, parser in owned_parsers.items():
                if connection.parser.get(name) is not parser:
                    continue
                if old_parsers[name] is None:
                    connection.parser.pop(name, None)
                else:
                    connection.parser[name] = old_parsers[name]
        client = state["client"]
        for attr, old_handler in state["old_handlers"].items():
            if getattr(client, attr, None) is not state["owned_handlers"][attr]:
                continue
            if old_handler is None:
                delattr(client, attr)
            else:
                setattr(client, attr, old_handler)

    async def terminate(self) -> None:
        if self._patch_task:
            self._patch_task.cancel()
            try:
                await self._patch_task
            except asyncio.CancelledError:
                pass
        for state in self._patched.values():
            self._restore_client_handlers(state)
        self._patched.clear()
        state_class = self._parser_state_class
        if state_class is not None:
            for attr, parser in self._owned_parser_methods.items():
                if getattr(state_class, attr, None) is parser:
                    delattr(state_class, attr)
        self._owned_parser_methods.clear()
        self._parser_state_class = None


def render_member_notice(
    template: str,
    member_openid: str,
    *,
    can_at: bool,
    appid: str = "",
) -> str:
    """Render member placeholders for lifecycle notices."""
    if can_at and member_openid:
        member_value = f'<qqbot-at-user id="{member_openid}" />'
    else:
        member_value = member_openid or "未知成员"
    avatar_value = ""
    if appid and member_openid:
        avatar_url = (
            "https://q.qlogo.cn/qqapp/"
            f"{quote(appid, safe='')}/{quote(member_openid, safe='')}/640"
        )
        avatar_value = f"![头像 #100px #100px]({avatar_url})"
    return template.replace("{member_at}", member_value).replace(
        "{member_avatar}", avatar_value
    )


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
