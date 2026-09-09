from __future__ import annotations

from datetime import datetime, timezone

from whale_companion_service.companion_runtime.daily_life import (
    compose_daily_state,
    current_segment,
    daily_life_context,
    default_daily_plan,
    empty_chronotype,
    generate_daily_conditions,
    is_active_window,
    meal_hunger_condition,
    period_of,
    refine_segment,
)

NOW = datetime(2026, 1, 10, 15, 0, 0, tzinfo=timezone.utc)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 1, 10, hour, minute, 0, tzinfo=timezone.utc)


def test_default_chronotype():
    c = empty_chronotype()
    assert c["wakeMinute"] == 7 * 60 + 30
    assert c["sleepMinute"] == 22 * 60 + 30
    assert c["source"] == "default"


def test_period_mapping():
    c = empty_chronotype()
    assert period_of(_at(3, 0), c) == "late_night"      # 睡眠窗跨午夜
    assert period_of(_at(8, 0), c) == "morning"
    assert period_of(_at(15, 0), c) == "daytime"
    assert period_of(_at(20, 0), c) == "evening"
    assert period_of(_at(23, 0), c) == "late_night"     # 过了 22:30


def test_active_window():
    c = empty_chronotype()
    assert is_active_window(_at(15, 0), c) is True
    assert is_active_window(_at(3, 0), c) is False


def test_daily_conditions_are_deterministic():
    a = generate_daily_conditions(NOW, empty_chronotype())
    b = generate_daily_conditions(NOW, empty_chronotype())
    assert a == b
    # 不同日子通常不同（种子按日变化）。
    other = datetime(2026, 1, 11, 15, 0, 0, tzinfo=timezone.utc)
    c = generate_daily_conditions(other, empty_chronotype())
    # 结构一致即可，具体值允许不同（不强断言）。
    assert isinstance(c, list) and len(c) >= 1


def test_compose_energy_sums_deltas():
    conditions = [
        {"kind": "sleep", "label": "睡得挺沉", "mood": "", "energyDelta": 12.0, "intensity": 0.8},
        {"kind": "hunger", "label": "有点饿", "mood": "", "energyDelta": -6.0, "intensity": 0.6},
    ]
    state = compose_daily_state(conditions, NOW, empty_chronotype())
    assert state["energy"] == 75.0 + 12.0 - 6.0


def test_compose_energy_clamped():
    conditions = [{"kind": "sleep", "label": "累", "mood": "", "energyDelta": -200.0, "intensity": 1.0}]
    state = compose_daily_state(conditions, NOW, empty_chronotype())
    assert state["energy"] == 10.0


def test_mood_bias_picks_strongest_nonempty():
    conditions = [
        {"kind": "a", "mood": "", "energyDelta": 0.0, "intensity": 0.9},
        {"kind": "b", "mood": "有点困", "energyDelta": 0.0, "intensity": 0.5},
        {"kind": "c", "mood": "挺开心", "energyDelta": 0.0, "intensity": 0.7},
    ]
    state = compose_daily_state(conditions, NOW, empty_chronotype())
    assert state["moodBias"] == "挺开心"


def test_meal_hunger_only_in_window():
    c = empty_chronotype()
    assert meal_hunger_condition(_at(8, 30), c) is not None   # 早餐窗 8:30-9:30
    assert meal_hunger_condition(_at(15, 0), c) is None       # 非饭点


def test_default_plan_and_current_segment():
    c = empty_chronotype()
    plan = default_daily_plan(c)
    assert len(plan) >= 4
    segment = current_segment(plan, _at(15, 0), c)
    assert segment is not None
    assert "日常" in segment["activity"]


def test_refine_segment_structured():
    state = compose_daily_state([], NOW, empty_chronotype())
    segment = current_segment(default_daily_plan(), _at(15, 0), empty_chronotype())
    refined = refine_segment(segment, state)
    assert refined["location"] == ""                     # 不编造地点
    assert refined["todayEvents"]
    assert refined["proactiveEvents"]
    assert refined["stateVariables"][0]["name"] == "精力"


def test_daily_life_context_no_fabrication_hint():
    ctx = daily_life_context(compose_daily_state([], NOW, empty_chronotype()), empty_chronotype(), NOW)
    assert ctx["energy"] == 75.0
    assert ctx["activeWindow"] is True
    assert "编造" in ctx["usage"]
