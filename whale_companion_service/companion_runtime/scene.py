from __future__ import annotations

from datetime import datetime


def build_scene(memories: list[dict], *, now: datetime | None = None) -> dict:
    """时段和最近一条可验证的桌面事实。

    `now` 可注入：这个模块此前直接读墙钟，注入固定时钟的仿真因此无法完全复现一次读取。
    不传仍按墙钟走，调用方的行为不变。
    """
    hour = (now or datetime.now()).hour
    period = "late_night" if hour < 6 else "morning" if hour < 11 else "daytime" if hour < 18 else "evening"
    recent = next((row for row in reversed(memories) if row.get("layer") == "L1"), None)
    return {
        "period": period,
        "recentObservedFactId": str((recent or {}).get("id") or ""),
        "recentApp": str((recent or {}).get("app") or "")[:160],
        "recentContext": str((recent or {}).get("context") or "")[:40],
    }
