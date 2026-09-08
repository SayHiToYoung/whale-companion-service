from __future__ import annotations


def build_safety(boundaries: list[dict], emotion: dict) -> dict:
    return {
        "activeBoundaries": [row.get("rule", "") for row in boundaries if row.get("status") == "active"],
        "doNotInferEmotion": not emotion["mayStateAsFact"],
        "doNotInventUserPsychology": True,
        "doNotInventHumanBiography": True,
    }
