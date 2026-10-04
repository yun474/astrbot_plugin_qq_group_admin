"""Compile join-answer rules and bound regex execution on untrusted answers."""

from functools import lru_cache
from time import monotonic

import regex

JOIN_REGEX_KEYS = {"join_whitelist_words", "join_blacklist_words"}
MATCH_TIMEOUT = 0.05


@lru_cache(maxsize=128)
def compile_join_pattern(pattern: str):
    try:
        return regex.compile(pattern, regex.IGNORECASE | regex.VERSION1)
    except regex.error as exc:
        raise ValueError(f"入群正则无效：{exc}") from exc


def match_join_rules(
    answers: list[str], rules: list[tuple[bool, list[str]]]
) -> bool | None:
    # Validate both lists before approving anything, including lower-priority rules.
    compiled = [
        (
            approve,
            [compile_join_pattern(word.strip()) for word in words if word.strip()],
        )
        for approve, words in rules
    ]
    deadline = monotonic() + MATCH_TIMEOUT
    for approve, patterns in compiled:
        for answer in answers:
            for pattern in patterns:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("入群正则匹配超时")
                if pattern.search(answer, timeout=remaining):
                    return approve
    return None
