from __future__ import annotations

from ..dialogue_state import is_emotional_bid
from ..emotion import explicit_emotion_label


def build_emotion_state(user_text: str, shared_scene: dict, open_threads: dict | None = None) -> dict:
    """当前轮的情绪状态。

    `userEmotion` 只记录用户这一轮亲口说出的情绪，没说就是 unknown。
    `carriedEmotion` 是上次仍未闭合的线程带过来的，属于低置信度背景，
    它永远不会让 `mayStateAsFact` 变真——大鲸可以记得，但不能断言。
    """
    explicit = explicit_emotion_label(user_text)
    threads = open_threads if isinstance(open_threads, dict) else {}
    carried = "" if explicit else str(threads.get("carriedEmotion") or "")
    return {
        "userEmotion": explicit or "unknown",
        "confidence": 1.0 if explicit else 0.0,
        "source": "user_stated" if explicit else ("carried_thread" if carried else "none"),
        "carriedEmotion": carried,
        "carriedFromThread": str(threads.get("carriedFromThread") or "") if carried else "",
        "carriedAgeDays": float(threads.get("carriedAgeDays") or 0.0) if carried else 0.0,
        "signal": "emotional_bid" if is_emotional_bid(user_text) else "none",
        "interactionTone": "confused_or_challenging" if shared_scene["intent"] == "reaction_to_companion" else "neutral",
        "mayStateAsFact": bool(explicit),
    }
