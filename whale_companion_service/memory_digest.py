# -*- coding: utf-8 -*-
"""日摘要与周摘要的数据结构。

摘要在这一阶段只有骨架，没有正文：`summary` 恒为空、`status` 恒为 `pending_summary`。
这是刻意的——写出"你这周好像有点累"需要一次额外的模型调用，而本阶段的目标是先把
"哪些记忆属于哪一段时间、它们是什么类型、哪几条最活跃"这件事确定下来，并且确定得可复算。
骨架是确定性的：同样的记忆在任何时刻重建，得到逐字节相同的摘要对象。

摘要不进模型上下文。它是一个可以稍后被填充、被审阅、被撤回的容器，
不是一条"大鲸认为你这周过得如何"的记忆。
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone

from .memory_activation import memory_activation, parse_time


DIGEST_KINDS = ("daily", "weekly")
DIGEST_STATUS_PENDING = "pending_summary"
MAX_DIGEST_HIGHLIGHTS = 5
_TIME_FIELDS = ("occurredAt", "endedAt", "startedAt", "createdAt", "receivedAt")


def memory_local_date(memory: dict) -> str:
    """摘要与既有 `daily_episodes` 必须落在同一天上，所以日切口径逐字沿用它：
    客户端给了 `localDate` 就听客户端的，否则按服务端本地时区换算。"""
    explicit = str(memory.get("localDate") or "").strip()
    if explicit:
        return explicit
    for key in _TIME_FIELDS:
        parsed = parse_time(memory.get(key))
        if parsed is not None:
            return parsed.astimezone().date().isoformat()
    return ""


def week_start_of(local_date: str) -> str:
    """周摘要以周一为界；返回该日期所属周的周一。"""
    try:
        day = date.fromisoformat(str(local_date or ""))
    except ValueError:
        return ""
    return (day - timedelta(days=day.weekday())).isoformat()


def iso_week_of(local_date: str) -> str:
    try:
        day = date.fromisoformat(str(local_date or ""))
    except ValueError:
        return ""
    year, week, _weekday = day.isocalendar()
    return f"{year}-W{week:02d}"


def build_memory_digest(
    kind: str,
    memories: list[dict],
    *,
    period_start: str,
    now: datetime | None = None,
) -> dict:
    """把一段时间里的记忆汇成一个待摘要的骨架，不生成任何自然语言。"""
    if kind not in DIGEST_KINDS:
        raise ValueError("unsupported digest kind")
    span = 1 if kind == "daily" else 7
    try:
        first = date.fromisoformat(str(period_start or ""))
    except ValueError as exc:
        raise ValueError("invalid digest periodStart") from exc
    last = first + timedelta(days=span - 1)
    dates = {(first + timedelta(days=index)).isoformat() for index in range(span)}
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    rows = [
        row for row in memories
        if isinstance(row, dict) and str(row.get("id") or "")
        and memory_local_date(row) in dates
    ]
    readings = {}
    for row in rows:
        lifecycle = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
        readings[str(row["id"])] = memory_activation(row, lifecycle, now=moment)

    ordered = sorted(rows, key=lambda row: (
        -readings[str(row["id"])]["activation"], str(row["id"])))
    return {
        "digestId": f"digest:{kind}:{first.isoformat()}",
        "kind": kind,
        "periodStart": first.isoformat(),
        "periodEnd": last.isoformat(),
        "localDate": first.isoformat() if kind == "daily" else "",
        "isoWeek": iso_week_of(first.isoformat()),
        # 正文留白是本阶段的约定，不是未完成的实现。
        "status": DIGEST_STATUS_PENDING,
        "summary": "",
        "summarySource": "",
        "memoryCount": len(rows),
        "memoryIds": sorted(str(row["id"]) for row in rows),
        "classCounts": dict(sorted(Counter(
            readings[str(row["id"])]["memoryClass"] for row in rows).items())),
        "layerCounts": dict(sorted(Counter(
            str(row.get("layer") or "") for row in rows).items())),
        "emotionLabels": dict(sorted(Counter(
            str(row.get("label") or "") for row in rows
            if row.get("layer") == "L3" and row.get("label")).items())),
        "highlights": [{
            "memoryId": str(row["id"]),
            "memoryClass": readings[str(row["id"])]["memoryClass"],
            "activation": readings[str(row["id"])]["activation"],
        } for row in ordered[:MAX_DIGEST_HIGHLIGHTS]],
        "openLoopMemoryIds": sorted(
            str(row["id"]) for row in rows
            if readings[str(row["id"])]["components"]["unresolved"] >= 1.0),
        "generatedAt": moment.isoformat(),
    }


def digest_periods(memories: list[dict]) -> list[tuple[str, str]]:
    """这批记忆覆盖到的 (kind, periodStart) 列表，用于只重建受影响的摘要。"""
    days = {memory_local_date(row) for row in memories if isinstance(row, dict)}
    days.discard("")
    periods = {("daily", day) for day in days}
    periods.update(("weekly", week_start_of(day)) for day in days)
    return sorted(period for period in periods if period[1])
