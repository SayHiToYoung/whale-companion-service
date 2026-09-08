# -*- coding: utf-8 -*-
"""未闭合的情绪线程：陪伴帧里唯一跨会话延续的状态。

其余 companion_runtime 模块都只看当前这一轮。本模块把 `emotional_threads`
表里仍然开着的线程读进帧里，让大鲸知道"上次那件事还没完"。

它不推断任何用户没说过的情绪：线程只能由用户亲口说出的 L3 情绪创建，这里
只负责把它带回来，并按时间衰减决定还能不能带。
"""
from __future__ import annotations

from datetime import datetime, timezone


# 情绪不是到期作废，是一天天变淡。用半衰期表达这件事，帧里因此带着一个
# 连续的 carryWeight，而不是"带 / 不带"两档——隔了两天的事该提得更轻。
EMOTION_HALF_LIFE_DAYS = 1.5
# 约定比情绪活得久："过两天再问我"值得多记一阵子。
FOLLOW_UP_HALF_LIFE_DAYS = 3.5
# 低于这个权重就当作已经过去，不再进入当前轮。
CARRY_WEIGHT_FLOOR = 0.25
MAX_THREADS_IN_FRAME = 3

_OPEN_STATUSES = frozenset({"active", "acknowledged"})


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _days_since(value: object, now: datetime) -> float:
    parsed = _parse_time(value)
    if parsed is None:
        return float("inf")
    return max(0.0, (now - parsed).total_seconds() / 86_400.0)


def _last_touch(thread: dict) -> object:
    return thread.get("lastMentionedAt") or thread.get("updatedAt") or thread.get("openedAt")


def _decay(days: float, half_life: float) -> float:
    """半衰期衰减；无法解析时间的线程权重为 0。"""
    if days == float("inf"):
        return 0.0
    return round(0.5 ** (days / half_life), 4)


def build_open_threads(threads: list[dict], now: datetime | None = None) -> dict:
    """把仍然开着的线程整理成帧里的一段结构化状态。"""
    moment = now or datetime.now(timezone.utc)
    rows = [
        row for row in threads
        if isinstance(row, dict) and str(row.get("status") or "") in _OPEN_STATUSES
    ]
    rows.sort(key=lambda row: _days_since(_last_touch(row), moment))

    summarized: list[dict] = []
    for row in rows[:MAX_THREADS_IN_FRAME]:
        days = _days_since(_last_touch(row), moment)
        summarized.append({
            "threadId": str(row.get("threadId") or ""),
            "label": str(row.get("label") or ""),
            "topic": str(row.get("topic") or "")[:240],
            "need": str(row.get("need") or "unknown"),
            "status": str(row.get("status") or ""),
            "mentionCount": int(row.get("mentionCount") or 0),
            "daysSinceLastTouch": round(days, 2) if days != float("inf") else None,
            "carryWeight": _decay(days, EMOTION_HALF_LIFE_DAYS),
        })

    carried, carried_from, carried_days, carried_weight = "", "", 0.0, 0.0
    for view in summarized:
        if view["label"] and view["carryWeight"] >= CARRY_WEIGHT_FLOOR:
            carried, carried_from = view["label"], view["threadId"]
            carried_days = view["daysSinceLastTouch"] or 0.0
            carried_weight = view["carryWeight"]
            break

    pending: list[dict] = []
    for row in rows:
        hint = str(row.get("followUpHint") or "").strip()
        if not hint:
            continue
        weight = _decay(_days_since(_last_touch(row), moment), FOLLOW_UP_HALF_LIFE_DAYS)
        if weight < CARRY_WEIGHT_FLOOR:
            continue
        pending.append({
            "threadId": str(row.get("threadId") or ""),
            "hint": hint[:80],
            "topic": str(row.get("topic") or "")[:240],
            "weight": weight,
        })

    return {
        "openCount": len(rows),
        "threads": summarized,
        "carriedEmotion": carried,
        "carriedFromThread": carried_from,
        "carriedAgeDays": carried_days,
        "carriedWeight": carried_weight,
        "pendingFollowUps": pending[:MAX_THREADS_IN_FRAME],
    }
