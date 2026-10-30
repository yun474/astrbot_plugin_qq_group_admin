"""进群验证的出题、判题与按钮构造，不涉及网络请求和存储。"""

from __future__ import annotations

import re
import secrets
from typing import Any

from .keyword_rules import MATCH_TIMEOUT, compile_join_pattern

VERIFY_CALLBACK_RE = re.compile(r"^qqgv:([0-9a-f]{32}):(\d)$")
MODE_ARITHMETIC = "算术题"
MODE_CUSTOM = "自定义问题"
VERIFY_MODES = (MODE_ARITHMETIC, MODE_CUSTOM)
TIMEOUT_IGNORE = "不处理"
TIMEOUT_REMIND = "提醒一次"
TIMEOUT_FAIL = "按失败禁言"
TIMEOUT_ACTIONS = (TIMEOUT_IGNORE, TIMEOUT_REMIND, TIMEOUT_FAIL)
OPERATORS = {"加": "+", "减": "-", "乘": "×", "除": "÷"}
OPTION_COUNT = 4
DEFAULT_PROMPT = "{member_at} 欢迎入群！请先完成进群验证：\n\n{question}\n\n{rules}"

_random = secrets.SystemRandom()


def make_arithmetic(
    operators: list[str], max_number: int, rng: Any = _random
) -> tuple[str, list[str], int]:
    """返回（题目，选项，正确选项下标）；除法必定整除，减法结果不为负。"""
    allowed = [op for op in operators if op in OPERATORS] or list(OPERATORS)
    limit = max(2, min(max_number, 1000))
    op = rng.choice(allowed)
    if op == "除":
        b = rng.randint(1, limit)
        answer = rng.randint(1, limit)
        a = b * answer
    else:
        a, b = rng.randint(1, limit), rng.randint(1, limit)
        if op == "减" and a < b:
            a, b = b, a
        answer = {"加": a + b, "减": a - b, "乘": a * b}[op]
    # 干扰项围绕正确答案生成，避免一眼看出唯一的“合理”选项。
    candidates = [
        value for value in range(answer - 10, answer + 11) if value >= 0 and value != answer
    ]
    options = rng.sample(candidates, OPTION_COUNT - 1)
    correct = rng.randrange(OPTION_COUNT)
    options.insert(correct, answer)
    return f"{a} {OPERATORS[op]} {b} = ?", [str(value) for value in options], correct


def parse_custom_questions(lines: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """解析 `问题 => 正则`，返回（有效题目，无效行）。"""
    questions: list[tuple[str, str]] = []
    invalid: list[str] = []
    for line in lines:
        text = str(line).strip()
        if not text:
            continue
        question, separator, pattern = text.partition("=>")
        question, pattern = question.strip(), pattern.strip()
        if not separator or not question or not pattern:
            invalid.append(text)
            continue
        try:
            compile_join_pattern(pattern)
        except ValueError:
            invalid.append(text)
            continue
        questions.append((question, pattern))
    return questions, invalid


def check_custom_answer(pattern: str, text: str) -> bool:
    """正则无效或匹配超时时抛出 ValueError / TimeoutError，由调用方按配置错误处理。"""
    return bool(compile_join_pattern(pattern).search(text, timeout=MATCH_TIMEOUT))


def verify_keyboard(token: str, member_openid: str, options: list[str]) -> dict[str, Any]:
    """选项按钮只允许被验证的新成员点击。"""
    return {
        "content": {
            "rows": [
                {
                    "buttons": [
                        {
                            "id": f"qqgv-{index}",
                            "render_data": {
                                "label": option,
                                "visited_label": option,
                                "style": 1,
                            },
                            "action": {
                                "type": 1,
                                "permission": {
                                    "type": 0,
                                    "specify_user_ids": [member_openid],
                                },
                                "data": f"qqgv:{token}:{index}",
                                "unsupport_tips": "请更新 QQ 客户端后点击按钮作答",
                            },
                        }
                        for index, option in enumerate(options)
                    ]
                }
            ]
        }
    }


def question_text(record: dict[str, Any]) -> str:
    if record["mode"] == "custom":
        return f"{record['question']}\n（直接在群里发送答案即可）"
    if record.get("text_answer"):
        return f"{record['question']}\n（请直接在群里发送答案数字）"
    return f"{record['question']}\n（点击下方按钮选择答案）"


def rules_text(max_failures: int, timeout_minutes: int, timeout_action: str) -> str:
    parts = [f"答错 {max_failures} 次将被禁言"]
    if timeout_minutes > 0:
        if timeout_action == TIMEOUT_FAIL:
            parts.insert(0, f"限时 {timeout_minutes} 分钟，超时按未通过处理")
        else:
            parts.insert(0, f"请在 {timeout_minutes} 分钟内完成")
    parts.append("验证前发送的消息会被撤回")
    return "；".join(parts) + "。"


def render_prompt(
    template: str, member_openid: str, question: str, rules: str, *, markdown: bool
) -> str:
    member = f'<qqbot-at-user id="{member_openid}" />' if markdown else "新成员"
    content = (
        (template.strip() or DEFAULT_PROMPT)
        .replace("{member_at}", member)
        .replace("{question}", question)
        .replace("{rules}", rules)
    )
    # QQ Markdown 的单个换行不会断行，需要行尾两个空格。
    return content.replace("\n", "  \n") if markdown else content
