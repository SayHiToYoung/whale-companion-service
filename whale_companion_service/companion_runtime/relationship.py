from __future__ import annotations


def build_relationship(conversation: list[dict], user_facts: list[dict], open_threads: dict | None = None) -> dict:
    threads = open_threads if isinstance(open_threads, dict) else {}
    user_turns = sum(1 for row in conversation if row.get("role") == "user")
    stage = "familiar" if user_turns >= 12 else "warming" if user_turns >= 4 else "early"
    return {
        "stage": stage,
        "userTurnCount": user_turns,
        "knownFactCount": sum(1 for row in user_facts if row.get("status") == "confirmed"),
        "interactionStyle": "direct_and_playful" if stage != "early" else "warm_but_not_intimate",
        # 用户说过"过两天再问我"这类约定；这是关系里唯一由用户主动交给大鲸的待办。
        "commitments": list(threads.get("pendingFollowUps", [])),
        "openThreadCount": int(threads.get("openCount") or 0),
    }
