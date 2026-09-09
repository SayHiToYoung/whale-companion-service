from __future__ import annotations

from . import affinity


def _fallback_stage(user_turns: int) -> str:
    """无好感度状态时的保守映射：按对话轮数落到 8 档。"""
    if user_turns >= 60:
        return "intimate"
    if user_turns >= 12:
        return "close"
    if user_turns >= 4:
        return "familiar"
    return "acquaintance"


def build_relationship(conversation: list[dict], user_facts: list[dict], open_threads: dict | None = None,
                       affinity_state: dict | None = None, boundaries: list[dict] | None = None) -> dict:
    """当前关系帧：8 档阶段 + 互动状态 + 权限位 + 约定/未闭合线程。

    affinity_state 来自好感度状态机（见 affinity.py），缺省时按对话轮数保守落到 8 档，
    保证没有持久化状态时也能给出合理的关系分寸。

    `permissions` 是这份关系对外的唯一形态：只说能做什么，不带分数。
    """
    threads = open_threads if isinstance(open_threads, dict) else {}
    user_turns = sum(1 for row in conversation if row.get("role") == "user")

    if isinstance(affinity_state, dict) and affinity_state.get("stage"):
        stage = str(affinity_state["stage"])
        interaction = str(affinity_state.get("interaction") or "relaxed")
        score = float(affinity_state.get("score") or 0.0)
    else:
        stage = _fallback_stage(user_turns)
        interaction = "relaxed"
        score = 0.0

    stage_info = affinity.stage_of_by_key(stage)
    if stage in {"deeply_distant", "strongly_distant", "distant"}:
        interaction_style = "restrained_polite"
    elif stage == "acquaintance":
        interaction_style = "warm_but_not_intimate"
    else:
        interaction_style = "direct_and_playful"

    internal = affinity.normalize_affinity_state(affinity_state or {"stage": stage, "interaction": interaction})
    permissions = affinity.relationship_permissions(internal, boundaries=boundaries)

    return {
        "stage": stage,
        "stageLabel": stage_info["label"],
        "interaction": interaction,
        "score": round(score, 2),
        "boundaryState": internal["boundaryState"] if not boundaries
                         else affinity.derive_boundary_state(boundaries),
        "permissions": permissions,
        "userTurnCount": user_turns,
        "knownFactCount": sum(1 for row in user_facts if row.get("status") == "confirmed"),
        "interactionStyle": interaction_style,
        "allowPlayful": bool(stage_info["allow_playful"]),
        "allowFollowup": bool(stage_info["allow_followup"]),
        "allowMemory": bool(stage_info["allow_memory"]),
        "allowDailyCare": bool(stage_info["allow_daily_care"]),
        "commitments": list(threads.get("pendingFollowUps", [])),
        "openThreadCount": int(threads.get("openCount") or 0),
    }
