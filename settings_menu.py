"""Small Markdown menus for editing keyword settings in chat."""

import re
from urllib.parse import quote

WORD_LISTS = {
    "进群白词": "join_whitelist_words",
    "进群黑词": "join_blacklist_words",
    "违禁词": "mute_keywords",
}
KEYWORD_VALUES = {
    "进群匹配模式": "join_keyword_match_mode",
    "白黑词优先级": "join_keyword_priority",
    "违禁词时长": "keyword_mute_duration",
}


def escape(text: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+.!|<>~-])", r"\\\1", text).replace("\n", " ")


def link(label: str, command: str) -> str:
    return f"[{label}](mqqapi://aio/inlinecmd?command={quote(command, safe='')}&enter=false&reply=false)"


def keyword_summary(values: dict) -> str:
    mode = values.get("join_keyword_match_mode", "包含匹配")
    lines = [
        "",
        "## 关键词内容",
        "",
        f"进群白词、黑词：{escape(mode)}；消息违禁词：包含匹配。",
        link(
            "切换进群匹配模式",
            "群管功能 进群匹配模式 设置 "
            + ("包含匹配" if mode == "正则匹配" else "正则匹配"),
        ),
        "",
    ]
    for name, key in WORD_LISTS.items():
        words = values[key]
        preview = "、".join(escape(word[:25]) for word in words[:3]) or "空"
        lines.append(
            f"{name}（{len(words)} 条）：{preview}{'…' if len(words) > 3 else ''}  "
        )
        lines.append(
            link("查看 / 删除", f"群管功能 {name} 查看")
            + " · "
            + link("添加", f"群管功能 {name} 添加 ")
            + "  "
        )
    lines.append(
        "优先规则："
        + escape(values["join_keyword_priority"])
        + " · "
        + link(
            "切换",
            "群管功能 白黑词优先级 设置 "
            + (
                "白词优先"
                if values["join_keyword_priority"] == "黑词优先"
                else "黑词优先"
            ),
        )
    )
    lines.append(
        "违禁词时长："
        + escape(values["keyword_mute_duration"])
        + " · "
        + link("修改", "群管功能 违禁词时长 设置 ")
    )
    lines.append(
        "\n> 链接只填入指令；添加、修改时补全内容再发送。支持含空格的完整词条。"
    )
    return "\n".join(lines)


def word_page(name: str, words: list[str], page: int, mode: str = "包含匹配") -> str:
    pages = max(1, (len(words) + 9) // 10)
    if not 1 <= page <= pages:
        raise ValueError(f"页码应在 1 到 {pages} 之间")
    lines = [f"## {name}（{len(words)} 条，第 {page}/{pages} 页）", ""]
    if name in {"进群白词", "进群黑词"}:
        lines.append(
            "正则匹配：每条一个正则，默认忽略大小写；使用 ^ 和 $ 可限制完整回答。\n"
            if mode == "正则匹配"
            else "包含匹配：回答含有所填词条即命中，忽略大小写，无需编写正则。\n"
        )
    for word in words[(page - 1) * 10 : page * 10]:
        lines.append(
            f"- {escape(word)} · " + link("删除", f"群管功能 {name} 删除 {word}")
        )
    if not words:
        lines.append("（暂无词条）")
    if page > 1:
        lines.append(link("上一页", f"群管功能 {name} 查看 {page - 1}"))
    if page < pages:
        lines.append(link("下一页", f"群管功能 {name} 查看 {page + 1}"))
    lines.append(link("添加词条", f"群管功能 {name} 添加 "))
    lines.append(f"\n清空需发送：群管功能 {name} 清空 确认")
    return "\n".join(lines)
