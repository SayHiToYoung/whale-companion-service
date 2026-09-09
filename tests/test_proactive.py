from __future__ import annotations

from datetime import datetime, timedelta, timezone

from whale_companion_service.companion_runtime.proactive import decide_proactive_speak

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _ago(minutes: float) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat()


def test_veto_while_user_still_chatting():
    result = decide_proactive_speak(now=NOW, last_user_message_at=_ago(0.5), activity="active", period="daytime")
    assert result["shouldSpeak"] is False
    assert result["vetoReason"] == "user_still_chatting"


def test_veto_too_soon():
    result = decide_proactive_speak(now=NOW, last_spoke_at=_ago(5), activity="idle", period="daytime")
    assert result["vetoReason"] == "too_soon"


def test_veto_quiet_hours():
    result = decide_proactive_speak(now=NOW, activity="idle", period="late_night")
    assert result["vetoReason"] == "quiet_hours"


def test_follow_up_has_priority():
    result = decide_proactive_speak(
        now=NOW,
        activity="idle",
        period="daytime",
        pending_follow_ups=[{"hint": "过两天再问我", "weight": 0.8}],
    )
    assert result["shouldSpeak"] is True
    assert result["kind"] == "follow_up"
    assert result["content"] == "过两天再问我"


def test_follow_up_overrides_quiet_hours():
    result = decide_proactive_speak(
        now=NOW,
        activity="idle",
        period="late_night",
        pending_follow_ups=[{"hint": "过两天再问我", "weight": 0.8}],
    )
    assert result["shouldSpeak"] is True
    assert result["kind"] == "follow_up"


def test_check_in_on_open_emotion_thread():
    result = decide_proactive_speak(
        now=NOW,
        activity="idle",
        period="evening",
        carried_emotion="难过",
        carried_weight=0.5,
    )
    assert result["shouldSpeak"] is True
    assert result["kind"] == "check_in"


def test_observation_from_grounded_opening():
    result = decide_proactive_speak(
        now=NOW,
        activity="idle",
        period="daytime",
        opening="你今天在 xxx 上忙了大约 2 小时",
    )
    assert result["shouldSpeak"] is True
    assert result["kind"] == "observation"


def test_nothing_grounded_never_speaks():
    result = decide_proactive_speak(now=NOW, activity="idle", period="daytime")
    assert result["vetoReason"] == "nothing_grounded"


def test_stale_follow_up_ignored():
    result = decide_proactive_speak(
        now=NOW,
        activity="idle",
        period="daytime",
        pending_follow_ups=[{"hint": "过两天再问我", "weight": 0.1}],
    )
    assert result["vetoReason"] == "nothing_grounded"
