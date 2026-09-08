# -*- coding: utf-8 -*-
"""短期陪伴对话状态；它只描述当前会话，不写入长期记忆。"""
from __future__ import annotations

import re


_EMOTIONAL_BID = re.compile(r"(?:唉|哎|哎呀|唉呀){1,2}[，。！？!?…~～\s]*")
_STOP_PHRASES = ("不想说", "不想聊", "别问了", "别问", "算了", "没什么", "没事")
_TOPIC_SWITCH = re.compile(
    r"(?:你知道|你记得|我问你|问你呢|今天干嘛|今天做了什么|几点|多少|为什么|怎么做|怎么弄)"
)


def is_emotional_bid(text: str) -> bool:
    return bool(_EMOTIONAL_BID.fullmatch(str(text or "").strip()))


def is_explicit_stop(text: str) -> bool:
    value = str(text or "")
    return any(phrase in value for phrase in _STOP_PHRASES)


def emotional_dialogue_state(conversation: list[dict]) -> dict:
    """从最近消息推导一次短暂的情绪承接阶段。

    最多延续叹气后的三次用户表达，避免把一次模糊信号无限解释成情绪。
    """
    user_turns = [
        str(item.get("text") or "").strip()
        for item in conversation
        if isinstance(item, dict) and item.get("role") == "user" and str(item.get("text") or "").strip()
    ]
    if not user_turns:
        return {"phase": "idle", "turn": 0}
    if is_explicit_stop(user_turns[-1]):
        return {"phase": "closed", "turn": 0}
    # 明确的信息问题、纠正或追问代表用户已经切换任务，不能继续套用旧的情绪线程。
    if _TOPIC_SWITCH.search(user_turns[-1]) and not is_emotional_bid(user_turns[-1]):
        return {"phase": "idle", "turn": 0}
    anchor = next(
        (index for index in range(len(user_turns) - 1, -1, -1) if is_emotional_bid(user_turns[index])),
        -1,
    )
    if anchor < 0:
        return {"phase": "idle", "turn": 0}
    turn = len(user_turns) - anchor - 1
    if turn == 0:
        return {"phase": "invited", "turn": 0, "anchor": user_turns[anchor]}
    if turn <= 3:
        return {"phase": "exploring", "turn": turn, "anchor": user_turns[anchor]}
    return {"phase": "idle", "turn": 0}
