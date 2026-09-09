from __future__ import annotations

from datetime import datetime, timezone

import pytest

from whale_companion_service.companion_runtime.companion_emotion import (
    companion_emotion_context,
    empty_companion_emotion,
    update_companion_emotion,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_empty_is_calm():
    state = empty_companion_emotion()
    assert state["mood"] == ""
    assert state["valence"] == 0.0
    assert state["arousal"] == 0.2


def test_warmed_raises_valence():
    state = update_companion_emotion(empty_companion_emotion(), "谢谢你，有你在真好", "", now=NOW)
    assert state["valence"] > 0.3
    assert state["mood"] == "被你这句话暖到了"
    assert state["lastEvent"] == "warmed"


def test_concern_is_not_a_mirror():
    # 用户难过，她不会跟着深度悲伤：mood 是「在意你」，唤醒度上升而非跌入谷底。
    state = update_companion_emotion(empty_companion_emotion(), "我很难过", "sad", now=NOW)
    assert state["mood"] == "有点在意你"
    assert state["valence"] > -0.5
    assert state["arousal"] > 0.1


def test_inertia_blends_instead_of_overwriting():
    warm = update_companion_emotion(empty_companion_emotion(), "谢谢你", "", now=NOW)
    sad = update_companion_emotion(warm, "我很难过", "sad", now=NOW)
    # new = 0.6 * raw + 0.4 * cur，前一秒被暖到、下一秒听说难过，不会一下跌到底。
    raw_valence = -0.25
    expected = 0.6 * raw_valence + 0.4 * warm["valence"]
    assert sad["valence"] == pytest.approx(expected, abs=1e-4)


def test_decay_toward_baseline():
    warm = update_companion_emotion(empty_companion_emotion(), "谢谢你", "", now=NOW)
    later = datetime(2026, 1, 1, 14, 0, 0, tzinfo=timezone.utc)
    decayed = update_companion_emotion(warm, "嗯", "", now=later)
    assert 0 < decayed["valence"] < warm["valence"]


def test_calm_clears_mood_after_long_decay():
    warm = update_companion_emotion(empty_companion_emotion(), "谢谢你", "", now=NOW)
    much_later = datetime(2026, 1, 2, 18, 0, 0, tzinfo=timezone.utc)
    decayed = update_companion_emotion(warm, "嗯", "", now=much_later)
    assert decayed["mood"] == ""
    assert abs(decayed["valence"]) < 0.12


def test_staying_up_small_mood():
    state = update_companion_emotion(empty_companion_emotion(), "今天又要熬夜了", "", now=NOW)
    assert state["mood"] == "又不好好睡觉"
    assert state["valence"] < 0


def test_context_calm_hides_mood():
    ctx = companion_emotion_context(empty_companion_emotion())
    assert ctx["calm"] is True
    assert ctx["mood"] == ""


def test_context_emotive_shows_mood():
    warm = update_companion_emotion(empty_companion_emotion(), "谢谢你", "", now=NOW)
    ctx = companion_emotion_context(warm)
    assert ctx["calm"] is False
    assert ctx["mood"] == "被你这句话暖到了"


def test_clamped_within_bounds():
    state = empty_companion_emotion()
    for _ in range(10):
        state = update_companion_emotion(state, "谢谢你真好喜欢你", "", now=NOW)
    assert -1.0 <= state["valence"] <= 1.0
    assert 0.0 <= state["arousal"] <= 1.0
