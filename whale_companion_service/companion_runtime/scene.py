from __future__ import annotations

from datetime import datetime


def build_scene(memories: list[dict]) -> dict:
    hour = datetime.now().hour
    period = "late_night" if hour < 6 else "morning" if hour < 11 else "daytime" if hour < 18 else "evening"
    recent = next((row for row in reversed(memories) if row.get("layer") == "L1"), None)
    return {
        "period": period,
        "recentObservedFactId": str((recent or {}).get("id") or ""),
        "recentApp": str((recent or {}).get("app") or "")[:160],
        "recentContext": str((recent or {}).get("context") or "")[:40],
    }
