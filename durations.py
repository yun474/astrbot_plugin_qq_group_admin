"""禁言等时长文本解析。"""

from __future__ import annotations

import re

TIME_PART_RE = re.compile(r"(\d+)\s*(天|日|小时|时|分钟|分|秒|s|m|h|d)", re.I)


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
