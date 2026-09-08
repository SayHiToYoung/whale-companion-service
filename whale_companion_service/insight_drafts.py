# -*- coding: utf-8 -*-
"""Evidence-bound, non-speaking insight drafts.

This combines Dream Loop-style offline consolidation with topic constellations.
Drafts are deliberately excluded from model context until a later explicit
confirmation workflow promotes them.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .profile_memory import stable_id


def _local_date(memory: dict) -> str:
    explicit = str(memory.get("localDate") or "").strip()
    if explicit:
        return explicit
    for key in ("occurredAt", "endedAt", "startedAt", "createdAt"):
        raw = str(memory.get(key) or "").strip()
        if raw:
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone().date().isoformat()
            except ValueError:
                pass
    return ""


def build_insight_drafts(memories: list[dict]) -> list[dict]:
    """Produce conservative cross-event patterns with explicit evidence IDs."""
    topic_groups: dict[str, list[dict]] = defaultdict(list)
    emotion_groups: dict[str, list[dict]] = defaultdict(list)
    for row in memories:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        if row.get("layer") == "L1":
            project = row.get("project") if isinstance(row.get("project"), dict) else {}
            topic = str(project.get("name") or row.get("app") or "").strip()
            if topic:
                topic_groups[topic].append(row)
        elif row.get("layer") == "L3" and row.get("label"):
            emotion_groups[str(row["label"])].append(row)

    drafts: list[dict] = []
    for topic, rows in topic_groups.items():
        days = sorted({day for day in (_local_date(row) for row in rows) if day})
        if len(rows) < 2 or len(days) < 2:
            continue
        evidence = [str(row["id"]) for row in sorted(rows, key=_local_date)[-8:]]
        drafts.append({
            "id": stable_id("insight", "topic", topic, *evidence),
            "status": "draft",
            "category": "recurring_topic",
            "headline": f"最近反复投入在 {topic}",
            "body": f"近 {len(days)} 天都出现过与 {topic} 有关的桌面记录。这只是行为模式候选，不代表进展或情绪。",
            "confidence": min(0.75, 0.5 + 0.05 * len(days)),
            "evidenceMemoryIds": evidence,
            "mayEnterModelContext": False,
        })
    for label, rows in emotion_groups.items():
        days = sorted({day for day in (_local_date(row) for row in rows) if day})
        if len(rows) < 2:
            continue
        evidence = [str(row["id"]) for row in sorted(rows, key=_local_date)[-8:]]
        drafts.append({
            "id": stable_id("insight", "emotion", label, *evidence),
            "status": "draft",
            "category": "repeated_stated_emotion",
            "headline": f"多次明确提到 {label}",
            "body": f"用户在 {max(1, len(days))} 天内留下了 {len(rows)} 条相同情绪标签；原因和持续性仍未知。",
            "confidence": min(0.8, 0.55 + 0.05 * len(rows)),
            "evidenceMemoryIds": evidence,
            "mayEnterModelContext": False,
        })
    return sorted(drafts, key=lambda row: (row["confidence"], row["headline"]), reverse=True)
