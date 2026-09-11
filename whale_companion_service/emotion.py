"""Strict recognition of emotions explicitly stated by the user."""
from __future__ import annotations

import re


_EMOTION_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("frustrated", ("好烦", "很烦", "烦死", "烦躁", "不爽", "闹心")),
    ("angry", ("生气", "气死", "恼火", "火大")),
    ("sad", ("难过", "伤心", "想哭", "低落", "沮丧")),
    ("anxious", ("焦虑", "紧张", "担心", "慌")),
    ("tired", ("好累", "很累", "累死", "疲惫", "没力气")),
    ("happy", ("开心", "高兴", "快乐", "爽到了")),
    ("excited", ("兴奋", "激动", "期待")),
    ("wronged", ("委屈", "憋屈")),
)


def explicit_emotion_label(text: str) -> str:
    """Return a label only for a direct, first-person emotional statement."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()[:1000]
    if not value:
        return ""
    first_person = any(token in value for token in ("我", "本人", "自己"))
    # Chinese commonly omits the first-person subject in a current-state sentence:
    # "今天真的好烦" is still an explicit self-report in a direct chat.  Keep the
    # accepted lead-in deliberately small so a sentence such as "他今天好烦" is not
    # accidentally attributed to the user.
    direct_feeling = bool(
        re.search(
            r"(?:^|[，。！？\s])"
            r"(?:(?:今天|现在|刚才|这会儿)(?:真的|确实|简直)?|(?:真的|确实|简直))?"
            r"(?:好|很|太|真|有点|特别)"
            r"(?:烦|累|开心|难过|焦虑|生气|委屈)",
            value,
        )
    )
    if not first_person and not direct_feeling:
        return ""
    for label, words in _EMOTION_RULES:
        if any(word in value for word in words):
            return label
    return ""
