"""群内分类菜单，导航和设置统一使用蓝字指令。"""

import re
from urllib.parse import quote

WORD_LISTS = {
    "进群白词": "join_whitelist_words",
    "进群黑词": "join_blacklist_words",
    "违禁词": "mute_keywords",
    "踢人词": "kick_keywords",
}
PAGES = (
    "成员管理",
    "入群审批",
    "进群验证",
    "发言管理",
    "通知设置",
    "LLM工具",
    "群信息与权限",
)
WORD_PAGES = {
    "进群白词": "入群审批",
    "进群黑词": "入群审批",
    "违禁词": "发言管理",
    "踢人词": "发言管理",
}
KEYWORD_VALUES = {
    "进群匹配模式": "join_keyword_match_mode",
    "白黑词优先级": "join_keyword_priority",
    "违禁词时长": "keyword_mute_duration",
    "验证模式": "verify_mode",
    "验证失败处理": "verify_fail_action",
    "验证超时处理": "verify_timeout_action",
    "验证失败时长": "verify_fail_mute_duration",
    "验证答错次数": "verify_max_failures",
    "验证限时": "verify_timeout_minutes",
}


def escape(text: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+.!|<>~-])", r"\\\1", text).replace("\n", " ")


def link(label: str, command: str) -> str:
    return f"[{label}](mqqapi://aio/inlinecmd?command={quote(command, safe='')}&enter=false&reply=false)"


def keyword_summary(values: dict, page: str = "入群审批") -> str:
    mode = values.get("join_keyword_match_mode", "包含匹配")
    lines = [""]
    if page == "入群审批":
        lines.append(
            "匹配方式："
            + link(
                mode,
                "群管功能 进群匹配模式 设置 "
                + ("包含匹配" if mode == "正则匹配" else "正则匹配"),
            )
        )
    for name, key in WORD_LISTS.items():
        if WORD_PAGES[name] != page:
            continue
        words = values[key]
        lines.append(f"{name}（{len(words)} 条）  ")
        lines.append(
            link("查看 / 删除", f"群管功能 {name} 查看")
            + " · "
            + link("添加", f"群管功能 {name} 添加 ")
            + "  "
        )
    if page == "入群审批":
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
    else:
        lines.append(
            "违禁词时长："
            + link(values["keyword_mute_duration"], "群管功能 违禁词时长 设置 ")
        )
    return "\n".join(lines)


def verify_summary(mode: str, values: dict | None = None) -> str:
    other = "自定义问题" if mode == "算术题" else "算术题"
    values = values or {}
    lines = ["", "验证题型：" + link(mode, f"群管功能 验证模式 设置 {other}")]
    for name, key, default in (
        ("验证答错次数", "verify_max_failures", 3),
        ("验证失败时长", "verify_fail_mute_duration", "1天"),
        ("验证限时", "verify_timeout_minutes", 10),
    ):
        lines.append(
            f"{name}：" + link(str(values.get(key, default)), f"群管功能 {name} 设置 ")
        )
    for name, options, current in (
        (
            "验证失败处理",
            ("禁言", "移出群聊"),
            values.get("verify_fail_action", "禁言"),
        ),
        (
            "验证超时处理",
            ("不处理", "提醒一次", "按失败禁言", "移出群聊"),
            values.get("verify_timeout_action", "按失败禁言"),
        ),
    ):
        lines.append(f"{name}（{escape(current)}）  ")
        lines.append(
            " · ".join(
                link(option, f"群管功能 {name} 设置 {option}") for option in options
            )
        )
    lines.append(link("待验证成员", "验证列表"))
    return "\n".join(lines)


def word_page(name: str, words: list[str], page: int) -> str:
    pages = max(1, (len(words) + 9) // 10)
    if not 1 <= page <= pages:
        raise ValueError(f"页码应在 1 到 {pages} 之间")
    lines = [f"## {name}（{len(words)} 条，第 {page}/{pages} 页）", ""]
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
    lines.append(link("清空词表", f"群管功能 {name} 清空 确认"))
    lines.append(link("返回分类", f"群管功能 {WORD_PAGES[name]}"))
    lines.append(link("返回首页", "群管功能"))
    return "  \n".join(lines)
