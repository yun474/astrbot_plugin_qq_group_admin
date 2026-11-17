"""移出通知的昵称读取、Markdown 排版和文本降级。"""

from html import escape as xml_escape
from typing import Any
from urllib.parse import quote

from .settings_menu import escape


def field(value: Any, key: str, default: Any = None) -> Any:
    return (
        value.get(key, default)
        if isinstance(value, dict)
        else getattr(value, key, default)
    )


def event_member_name(event: Any, member: str) -> str:
    raw = getattr(event.message_obj, "raw_message", None)
    data = raw if isinstance(raw, dict) else field(raw, "raw_data", {}) or {}
    author = data.get("author") or field(raw, "author", {})
    if member == event.get_sender_id():
        name = field(author, "username", "")
        if isinstance(name, str):
            return name.strip()
    for mention in data.get("mentions", []) or field(raw, "mentions", []) or []:
        if str(field(mention, "member_openid") or field(mention, "id", "")) == member:
            name = field(mention, "username", "")
            if isinstance(name, str):
                return name.strip()
    return ""


def member_avatar(appid: str, member: str) -> str:
    if not appid:
        return ""
    return (
        f"https://q.qlogo.cn/qqapp/{quote(appid, safe='')}/{quote(member, safe='')}/640"
    )


def notice_text(
    results: list[dict],
    source: str,
    *,
    operator: str = "",
    reason: str = "",
    markdown: bool = True,
) -> str:
    """批量按结果汇总，单人展示移出前保存的昵称和头像。"""
    labels = {
        "success": "已移出群聊",
        "unknown": "结果待确认",
        "failed": "移出失败",
        "muted": "已回退禁言",
        "mute_unknown": "移出失败，回退禁言结果待确认",
        "mute_failed": "移出与回退禁言均失败",
    }
    render = escape if markdown else str
    lines = [("## " if markdown else "") + "👢 成员移出记录", ""]

    def row(label: str, value: str) -> None:
        lines.append((f"**{label}**" if markdown else label) + "　" + render(value))

    def sources() -> None:
        row("触发来源", source)
        if operator:
            mention = (
                f'<qqbot-at-user id="{xml_escape(operator, quote=True)}" />'
                if markdown
                else operator
            )
            lines.append(("**处理人**" if markdown else "处理人") + "　" + mention)
        if reason:
            row("触发原因", reason)

    if len(results) == 1:
        item = results[0]
        row("成员", item["name"])
        if markdown and item.get("avatar"):
            lines.extend(("", f"![头像 #80px #80px]({item['avatar']})", ""))
        sources()
        result = labels[item["status"]]
        if item["status"] == "muted":
            result += " " + item["duration"]
        row("处理结果", result)
        if item.get("error"):
            row(
                "移出失败原因"
                if item["status"] in {"muted", "mute_failed", "mute_unknown"}
                else "说明",
                item["error"],
            )
        if item.get("mute_error"):
            row("禁言失败原因", item["mute_error"])
    else:
        sources()
        counts = {
            status: sum(item["status"] == status for item in results)
            for status in ("success", "failed", "unknown")
        }
        row(
            "处理结果",
            " · ".join(
                f"{label} {counts[status]} 人"
                for status, label in (
                    ("success", "成功"),
                    ("failed", "失败"),
                    ("unknown", "待确认"),
                )
                if counts[status]
            ),
        )
        for status, label in (
            ("success", "已移出"),
            ("failed", "未移出"),
            ("unknown", "待确认"),
        ):
            names = [
                item["name"] + (f" · {item['error']}" if item.get("error") else "")
                for item in results
                if item["status"] == status
            ]
            if names:
                row(
                    "成员" if counts["success"] == len(results) else label,
                    "、".join(names),
                )
    return ("  \n" if markdown else "\n").join(lines)
