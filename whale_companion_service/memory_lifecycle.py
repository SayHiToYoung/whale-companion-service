# -*- coding: utf-8 -*-
"""共享记忆的生命周期覆盖层。

原始 L1/L2/L3 事件保持不可变；本模块只描述一条记忆现在是否适合被大鲸主动使用。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


LIFECYCLE_STATUSES = {
    "active",
    "pending_confirmation",
    "resolved",
    "dormant",
    "suppressed",
}
SPEAKABLE_STATUSES = {"active", "pending_confirmation"}
FRESH_SECONDS_BY_LAYER = {
    "L1": 48 * 60 * 60,
    "L2": 48 * 60 * 60,
    "L3": 72 * 60 * 60,
}

_SUPPRESS_RE = re.compile(
    r"(?:不想再(?:提|聊)|以后(?:别|不要)(?:再)?(?:提|问|聊)|"
    r"(?:别|不要)(?:再)(?:提|问|聊)(?:这|那)?(?:件事|个事|它)?)"
)
_DORMANT_RE = re.compile(
    r"(?:先不(?:说|聊)|今天不想(?:说|聊)|暂时不想(?:说|聊)|别问了|算了)"
)
_RESOLVED_RE = re.compile(
    r"(?:好多了|已经(?:好多了|好了|没事了)|没事了|缓过来了|"
    r"(?:事情|问题|这个|它)?(?:已经)?(?:解决了|搞定了|结束了|过去了))"
)
_ACTIVE_RE = re.compile(
    r"(?:还没(?:好|解决|搞定|结束)|还是(?:很|好|有点|特别)?(?:烦|难过|焦虑|累)|"
    r"(?:事情|问题|这个|它)?还在(?:继续|弄|处理))"
)


def default_lifecycle_status(memory: dict) -> str:
    return "pending_confirmation" if memory.get("layer") == "L2" else "active"


def lifecycle_transition_from_text(text: str) -> tuple[str, str]:
    """只识别用户明确表达的状态变化，避免替用户判断事情是否结束。"""
    value = " ".join(str(text or "").split())
    if not value:
        return "", ""
    if _SUPPRESS_RE.search(value):
        return "suppressed", "user_asked_not_to_revisit"
    if _RESOLVED_RE.search(value):
        return "resolved", "user_said_it_improved_or_ended"
    if _ACTIVE_RE.search(value):
        return "active", "user_said_it_is_still_ongoing"
    if _DORMANT_RE.search(value):
        return "dormant", "user_paused_the_topic"
    return "", ""


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _memory_time(memory: dict) -> datetime | None:
    for key in ("occurredAt", "endedAt", "startedAt", "createdAt", "receivedAt"):
        parsed = _parse_time(memory.get(key))
        if parsed is not None:
            return parsed
    return None


def decorate_memory_lifecycle(
    memory: dict,
    lifecycle: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> dict:
    """返回带生命周期视图的副本，不改写原始记忆。"""
    result = dict(memory)
    stored = dict(lifecycle or {})
    stored_status = str(stored.get("status") or default_lifecycle_status(memory))
    if stored_status not in LIFECYCLE_STATUSES:
        stored_status = default_lifecycle_status(memory)

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    occurred = _memory_time(memory)
    lifecycle_updated = _parse_time(stored.get("updatedAt"))
    freshness_time = max(
        (value for value in (occurred, lifecycle_updated) if value is not None),
        default=None,
    )
    age_seconds = max(0, int((current - freshness_time).total_seconds())) if freshness_time else 0
    fresh_for = FRESH_SECONDS_BY_LAYER.get(str(memory.get("layer") or ""), 48 * 60 * 60)
    freshness = "fresh" if freshness_time is None or age_seconds <= fresh_for else "stale"
    effective_status = stored_status
    reason = str(stored.get("reason") or "")
    if freshness == "stale" and stored_status in SPEAKABLE_STATUSES:
        effective_status = "dormant"
        reason = "aged_out"

    result["lifecycle"] = {
        "status": effective_status,
        "storedStatus": stored_status,
        "freshness": freshness,
        "reason": reason,
        "sourceType": str(stored.get("sourceType") or "default_policy"),
        "sourceMessageId": str(stored.get("sourceMessageId") or ""),
        "updatedAt": str(stored.get("updatedAt") or ""),
    }
    return result


def memory_is_speakable(memory: dict) -> bool:
    lifecycle = memory.get("lifecycle") if isinstance(memory.get("lifecycle"), dict) else {}
    status = str(lifecycle.get("status") or default_lifecycle_status(memory))
    return status in SPEAKABLE_STATUSES
