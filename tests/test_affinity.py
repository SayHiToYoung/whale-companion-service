from __future__ import annotations

from datetime import datetime, timedelta, timezone

from whale_companion_service.companion_runtime.affinity import (
    apology_repair_delta,
    apply_event,
    boundary_violation_delta,
    decay_state,
    empty_affinity_state,
    interaction_state_of,
    proactive_quota,
    stage_key_of,
    stage_of,
)

NOW = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)


def _event(key="e1", kind="warm", delta=2.0, at=None) -> dict:
    return {"eventKey": key, "kind": kind, "delta": delta, "at": (at or NOW).isoformat()}


def test_stage_lookup_boundaries():
    assert stage_key_of(0) == "acquaintance"
    assert stage_key_of(199) == "acquaintance"
    assert stage_key_of(200) == "familiar"
    assert stage_key_of(599) == "familiar"
    assert stage_key_of(600) == "close"
    assert stage_key_of(900) == "intimate"
    assert stage_key_of(1200) == "deeply_bonded"
    assert stage_key_of(-1200) == "deeply_distant"
    assert stage_key_of(-400) == "distant"


def test_stage_flags():
    assert stage_of(0)["allow_followup"] is True
    assert stage_of(0)["allow_memory"] is False
    assert stage_of(200)["allow_playful"] is True
    assert stage_of(200)["proactive_care_limit"] == 1


def test_boundary_violation_deltas():
    assert boundary_violation_delta(1) == 4
    assert boundary_violation_delta(2) == 7
    assert boundary_violation_delta(3) == 12
    assert boundary_violation_delta(0) == 0
    assert boundary_violation_delta(1, malicious=True) == 14


def test_apology_repair_returns_sixty_percent():
    assert apology_repair_delta(10) == 6
    assert apology_repair_delta(7) == 4


def test_warm_event_raises_score():
    state, ledger = apply_event(empty_affinity_state(), [], _event(delta=2.0), now=NOW)
    assert state["score"] == 2.0
    assert state["stage"] == "acquaintance"
    assert len(ledger) == 1


def test_dedup_within_window():
    state = empty_affinity_state()
    state, ledger = apply_event(state, [], _event("e1", delta=2.0), now=NOW)
    state2, ledger2 = apply_event(state, ledger, _event("e1", delta=2.0, at=NOW + timedelta(minutes=5)), now=NOW + timedelta(minutes=5))
    assert state2["score"] == 2.0          # 去重，未再加分
    assert len(ledger2) == 1


def test_single_event_cap():
    state, _ = apply_event(empty_affinity_state(), [], _event("e1", "warm", delta=99.0), now=NOW)
    assert state["score"] == 4.0           # 普通事件单次上限 4


def test_daily_positive_cap():
    state = empty_affinity_state()
    ledger: list[dict] = []
    for i in range(10):
        state, ledger = apply_event(state, ledger, _event(f"e{i}", delta=4.0, at=NOW + timedelta(minutes=i)), now=NOW + timedelta(minutes=i))
    assert state["score"] <= 12.0          # 每日正向上限 12


def test_hysteresis_blocks_flicker():
    # 从 199 分（初识）加 2 分到 201，理论跨到熟悉，但迟滞 20 分不足 → 压回初识上界 199。
    state = {"score": 199.0, "stage": "acquaintance", "interaction": "relaxed", "updatedAt": NOW.isoformat()}
    state2, _ = apply_event(state, [], _event("x", "warm", delta=2.0), now=NOW)
    assert state2["stage"] == "acquaintance"


def test_interaction_state():
    assert interaction_state_of("acquaintance") == "relaxed"
    assert interaction_state_of("familiar") == "warm"
    assert interaction_state_of("intimate") == "affectionate"
    assert interaction_state_of("familiar", recent_violation=True) == "hurt"
    assert interaction_state_of("distant", recent_violation=True) == "avoidant"


def test_proactive_quota_takes_min():
    # min(全局10, 用户3, 阶段1, 互动inf) = 1
    assert proactive_quota(10, 3, "familiar", "warm") == 1
    # 疏离阶段额度 0。
    assert proactive_quota(10, 10, "distant", "relaxed") == 0
    # 互动 hurt/avoidant 强制 0。
    assert proactive_quota(10, 10, "familiar", "hurt") == 0


def test_decay_toward_zero_after_grace():
    state = {"score": 100.0, "stage": "acquaintance", "interaction": "relaxed", "updatedAt": NOW.isoformat()}
    later = NOW + timedelta(days=3 + 10)   # 宽限 3 天后又过 10 天
    decayed = decay_state(state, now=later)
    assert 0 < decayed["score"] < 100.0


def test_negative_score_does_not_decay_up():
    state = {"score": -300.0, "stage": "distant", "interaction": "avoidant", "updatedAt": NOW.isoformat()}
    later = NOW + timedelta(days=30)
    decayed = decay_state(state, now=later)
    assert decayed["score"] == -300.0       # 负分保持，不随时间回弹


def test_derive_warm_event():
    from whale_companion_service.companion_runtime.affinity import derive_affinity_event
    event = derive_affinity_event("谢谢你真好", "warm:m1")
    assert event["kind"] == "warm"
    assert event["delta"] == 2.0
    assert event["eventKey"] == "warm:m1"


def test_derive_violation_event():
    from whale_companion_service.companion_runtime.affinity import derive_affinity_event
    event = derive_affinity_event("你真没用", "boundary:m2")
    assert event["kind"] == "boundary"
    assert event["delta"] == -7.0


def test_derive_none_for_neutral():
    from whale_companion_service.companion_runtime.affinity import derive_affinity_event
    assert derive_affinity_event("今天天气不错") is None
    assert derive_affinity_event("") is None
