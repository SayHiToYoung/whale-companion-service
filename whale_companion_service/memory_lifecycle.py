# -*- coding: utf-8 -*-
"""共享记忆的生命周期覆盖层。

原始 L1/L2/L3 事件保持不可变；本模块只描述一条记忆现在是否适合被大鲸主动使用。

两条线互不代替：
- **用户意图**（resolved / dormant / suppressed / corrected）是硬闸门，只由用户明说触发，
  时间永远不能把它冲掉，也永远不能替用户宣布一件事结束。
- **淡出**由 `memory_activation` 的连续激活度决定，取代了原来按 layer 写死的新鲜期。
  同样是"过去两天"，桌面上的一段专注和用户亲口说的难过不该淡到同一个程度。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .memory_activation import classify_memory, memory_activation, memory_reference_time


LIFECYCLE_STATUSES = {
    "active",
    "pending_confirmation",
    "resolved",
    "dormant",
    "suppressed",
    "corrected",
}
SPEAKABLE_STATUSES = {"active", "pending_confirmation"}

_SUPPRESS_RE = re.compile(
    r"(?:不想再(?:提|聊)|以后(?:别|不要)(?:再)?(?:提|问|聊)|"
    r"(?:别|不要)(?:再)(?:提|问|聊)(?:这|那)?(?:件事|个事|它)?)"
)
_DORMANT_RE = re.compile(
    r"(?:先不(?:说|聊)|今天不想(?:说|聊)|暂时不想(?:说|聊)|别问了|算了)"
)
_CORRECTION_RE = re.compile(
    r"(?:(?:你|我)?(?:记|说|搞|弄|听)错了|不是我说的|我没(?:这么|那么)?说过|"
    r"你(?:这|那)条?(?:记|写)得?不对|不是(?:那|这)样的?[，,。！!]?我是)"
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
    # 纠正先于"结束"判定：用户说"你记错了，那件事早就解决了"要按纠正处理，
    # 否则大鲸会把一条自己记错的内容"标记为已解决"，错误本身反而被保留下来。
    if _CORRECTION_RE.search(value):
        return "corrected", "user_corrected_the_memory"
    if _RESOLVED_RE.search(value):
        return "resolved", "user_said_it_improved_or_ended"
    if _ACTIVE_RE.search(value):
        return "active", "user_said_it_is_still_ongoing"
    if _DORMANT_RE.search(value):
        return "dormant", "user_paused_the_topic"
    return "", ""


def _parse_time(value: object) -> datetime | None:
    """保留旧名字给既有调用方；解析规则与激活度层共用一份。"""
    from .memory_activation import parse_time

    return parse_time(value)


def _memory_time(memory: dict) -> datetime | None:
    return memory_reference_time(memory)


def decorate_memory_lifecycle(
    memory: dict,
    lifecycle: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
    relevance: float = 0.0,
) -> dict:
    """返回带生命周期视图的副本，不改写原始记忆。"""
    result = dict(memory)
    stored = dict(lifecycle or {})
    stored_status = str(stored.get("status") or default_lifecycle_status(memory))
    if stored_status not in LIFECYCLE_STATUSES:
        stored_status = default_lifecycle_status(memory)

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    reading = memory_activation(
        memory, stored, now=current, relevance=relevance, status=stored_status,
    )
    # 不再有"48 小时整点作废"这一刀：激活度跌破阈值才退到背景，
    # 而阈值对不同类型的记忆意味着完全不同的时长。
    freshness = "fresh" if reading["speakable"] else "stale"
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
        "memoryClass": reading["memoryClass"],
        "activation": reading["activation"],
        "retention": reading["retention"],
        "strength": reading["strength"],
        "halfLifeHours": reading["halfLifeHours"],
        "ageHours": reading["ageHours"],
        "mentionCount": reading["mentionCount"],
        "lastMentionedAt": str(stored.get("lastMentionedAt") or ""),
        "activationComponents": reading["components"],
    }
    return result


def memory_is_speakable(memory: dict) -> bool:
    lifecycle = memory.get("lifecycle") if isinstance(memory.get("lifecycle"), dict) else {}
    status = str(lifecycle.get("status") or default_lifecycle_status(memory))
    return status in SPEAKABLE_STATUSES


def memory_class_of(memory: dict) -> str:
    lifecycle = memory.get("lifecycle") if isinstance(memory.get("lifecycle"), dict) else {}
    return classify_memory(memory, lifecycle)


def memory_activation_of(memory: dict) -> float:
    """读取已装饰记忆上的激活度；未装饰的记忆按满激活处理，交由调用方再算。"""
    lifecycle = memory.get("lifecycle") if isinstance(memory.get("lifecycle"), dict) else {}
    try:
        return float(lifecycle.get("activation", 1.0))
    except (TypeError, ValueError):
        return 1.0
