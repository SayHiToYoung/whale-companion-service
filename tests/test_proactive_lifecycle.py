from __future__ import annotations

from datetime import datetime, timedelta, timezone

from whale_companion_service.companion_runtime.proactive import (
    build_proactive_candidates,
    evaluate_proactive_lifecycle,
    score_candidate,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _ago(minutes: float) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat()


def test_build_candidates_normalizes_sources():
    candidates = build_proactive_candidates(
        pending_follow_ups=[{"hint": "过两天再问我", "weight": 0.8}],
        carried_emotion="难过", carried_weight=0.5,
        opening="你今天写了 2 小时代码",
        daily_greeting="早安",
        meal_care="该吃午饭啦",
    )
    kinds = [c["kind"] for c in candidates]
    assert kinds[0] == "follow_up"
    assert "open_emotion" in kinds
    assert "grounded_opening" in kinds
    assert "daily_greeting" in kinds
    assert "meal_care" in kinds
    for c in candidates:
        assert c["signature"]
        assert "score" not in c  # 打分在 evaluate 阶段注入


def test_build_skips_empty_content():
    candidates = build_proactive_candidates(daily_greeting="   ", meal_care="")
    assert candidates == []


def test_score_orders_follow_up_highest():
    fu = build_proactive_candidates(pending_follow_ups=[{"hint": "x", "weight": 0.9}])[0]
    gr = build_proactive_candidates(daily_greeting="早安")[0]
    assert score_candidate(fu) > score_candidate(gr)


def test_lifecycle_selects_highest_score_candidate():
    result = evaluate_proactive_lifecycle(
        now=NOW, activity="idle", period="daytime",
        relationship_stage="familiar",
        pending_follow_ups=[{"hint": "过两天再问我", "weight": 0.8}],
        daily_greeting="早安",
    )
    assert result["shouldSpeak"] is True
    assert result["selected"]["kind"] == "follow_up"


def test_lifecycle_veto_relationship_boundary():
    result = evaluate_proactive_lifecycle(
        now=NOW, activity="idle", period="daytime",
        relationship_stage="distant",
        daily_greeting="早安",
    )
    assert result["shouldSpeak"] is False
    assert result["vetoReason"] == "relationship_boundary"


def test_lifecycle_veto_daily_cap():
    result = evaluate_proactive_lifecycle(
        now=NOW, activity="idle", period="daytime",
        relationship_stage="familiar",
        daily_sent=8, daily_limit=8,
        daily_greeting="早安",
    )
    assert result["shouldSpeak"] is False
    assert result["vetoReason"] == "daily_cap"


def test_lifecycle_veto_no_response_pause():
    result = evaluate_proactive_lifecycle(
        now=NOW, activity="idle", period="daytime",
        relationship_stage="familiar",
        no_response_streak=3,
        daily_greeting="早安",
    )
    assert result["shouldSpeak"] is False
    assert result["vetoReason"] == "no_response_pause"


def test_lifecycle_veto_token_hard_limit():
    result = evaluate_proactive_lifecycle(
        now=NOW, activity="idle", period="daytime",
        relationship_stage="familiar",
        token_remaining=0,
        daily_greeting="早安",
    )
    assert result["shouldSpeak"] is False
    assert result["vetoReason"] == "token_hard_limit"


def test_lifecycle_follow_up_overrides_quiet_hours():
    result = evaluate_proactive_lifecycle(
        now=NOW, activity="idle", period="late_night",
        relationship_stage="familiar",
        pending_follow_ups=[{"hint": "过两天再问我", "weight": 0.8}],
    )
    assert result["shouldSpeak"] is True
    assert result["selected"]["kind"] == "follow_up"


def test_lifecycle_nothing_grounded():
    result = evaluate_proactive_lifecycle(
        now=NOW, activity="idle", period="daytime",
        relationship_stage="familiar",
    )
    assert result["shouldSpeak"] is False
    assert result["candidates"] == []
    assert result["vetoReason"] == "nothing_grounded"


def test_lifecycle_audit_keeps_veto_reasons():
    result = evaluate_proactive_lifecycle(
        now=NOW, activity="idle", period="daytime",
        relationship_stage="distant",
        daily_greeting="早安",
    )
    assert result["candidates"][0]["vetoReason"] == "relationship_boundary"
