# -*- coding: utf-8 -*-
"""读取 daily_state 必须是幂等的：同一时刻读多少次，答案都一样。

曾经不是。饭点窗口内 `meal_hunger_condition` 产出的 hunger 被连同持久条件一起写回
`conditions_json`，下一次读取又追加一次，于是每读一次 energy 掉 6。这组测试用固定时钟
把那条界线钉死：持久条件（sleep / dream）落库，临时条件（hunger / transient_life_event）
只参与本次合成。

这里不检验 hunger 的 energyDelta 或窗口定义——那是日常生活算法的事，本文件只管
"读取不改变状态"。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from whale_companion_service.companion_runtime.daily_life import (
    TRANSIENT_CONDITION_KINDS,
    empty_chronotype,
    meal_hunger_condition,
)
from whale_companion_service.memory_server import MemoryRepository


def repo_at(tmp_path: Path, moment: datetime, name: str = "daily.sqlite3") -> MemoryRepository:
    """一台时钟被钉死的仓库。时间不走，读取就不该有任何借口改变结果。"""
    return MemoryRepository(tmp_path / name, clock=lambda: moment)


def a_meal_moment() -> datetime:
    """找一个确实落在饭点窗口内的时刻，并断言它真的在窗口里。"""
    base = datetime(2026, 3, 5, tzinfo=timezone.utc)
    chronotype = empty_chronotype()
    for minutes in range(0, 24 * 60):
        moment = base + timedelta(minutes=minutes)
        if meal_hunger_condition(moment, chronotype) is not None:
            return moment
    raise AssertionError("这一天里找不到饭点窗口，饭点定义可能已经改变")


def a_quiet_moment() -> datetime:
    base = datetime(2026, 3, 5, tzinfo=timezone.utc)
    chronotype = empty_chronotype()
    for minutes in range(0, 24 * 60):
        moment = base + timedelta(minutes=minutes)
        if meal_hunger_condition(moment, chronotype) is None:
            return moment
    raise AssertionError("这一天里找不到非饭点时刻")


def stored_conditions(repo: MemoryRepository, user_id: str, date: str) -> list[dict]:
    with repo._connect() as db:
        row = db.execute(
            "SELECT conditions_json FROM daily_state WHERE user_id=? AND state_date=?",
            (user_id, date),
        ).fetchone()
    return json.loads(row["conditions_json"]) if row else []


def test_reading_inside_a_meal_window_ten_times_returns_one_answer(tmp_path: Path) -> None:
    """验收口径：饭点固定时间重复读取至少 10 次，返回结果必须完全一致。"""
    moment = a_meal_moment()
    repo = repo_at(tmp_path, moment)
    readings = [repo.daily_state("u") for _ in range(12)]

    first = readings[0]
    for index, reading in enumerate(readings[1:], start=2):
        assert reading == first, f"第 {index} 次读取与第 1 次不同"
    # energy 不随读取次数下降——这正是原来的症状。
    assert len({reading["energy"] for reading in readings}) == 1


def test_a_meal_window_contributes_exactly_one_hunger(tmp_path: Path) -> None:
    moment = a_meal_moment()
    repo = repo_at(tmp_path, moment)
    for _ in range(5):
        conditions = repo.daily_state("u")["conditions"]
        hungers = [row for row in conditions if row.get("kind") == "hunger"]
        assert len(hungers) == 1, f"返回状态里出现了 {len(hungers)} 条 hunger"


def test_transient_conditions_are_never_persisted(tmp_path: Path) -> None:
    moment = a_meal_moment()
    repo = repo_at(tmp_path, moment)
    date = moment.strftime("%Y-%m-%d")
    for _ in range(5):
        repo.daily_state("u")
        stored = stored_conditions(repo, "u", date)
        kinds = [row.get("kind") for row in stored]
        assert not (set(kinds) & set(TRANSIENT_CONDITION_KINDS)), \
            f"临时条件被写进了 conditions_json：{kinds}"
        # 持久条件照常落库，含义不变。
        assert "sleep" in kinds
        assert len(stored) == len(stored_conditions(repo, "u", date))


def test_hunger_disappears_from_the_state_once_the_window_closes(tmp_path: Path) -> None:
    meal = a_meal_moment()
    chronotype = empty_chronotype()
    after = meal + timedelta(hours=1)
    while meal_hunger_condition(after, chronotype) is not None:
        after += timedelta(minutes=10)

    clock = {"now": meal}
    repo = MemoryRepository(tmp_path / "window.sqlite3", clock=lambda: clock["now"])
    during = repo.daily_state("u")
    assert any(row.get("kind") == "hunger" for row in during["conditions"])

    clock["now"] = after
    assert after.strftime("%Y-%m-%d") == meal.strftime("%Y-%m-%d"), "同一天内比较才有意义"
    later = repo.daily_state("u")
    # 窗口一过 hunger 就该消失，而不是留在当日流水里。
    assert not any(row.get("kind") == "hunger" for row in later["conditions"])
    # 持久条件仍在，且没有被这次读取改写。
    assert [row["kind"] for row in during["conditions"] if row["kind"] == "sleep"] == ["sleep"]
    assert any(row.get("kind") == "sleep" for row in later["conditions"])


def test_outside_a_meal_window_behaviour_is_unchanged(tmp_path: Path) -> None:
    moment = a_quiet_moment()
    repo = repo_at(tmp_path, moment)
    readings = [repo.daily_state("u") for _ in range(10)]
    assert all(reading == readings[0] for reading in readings)
    assert not any(row.get("kind") == "hunger" for row in readings[0]["conditions"])
    stored = stored_conditions(repo, "u", moment.strftime("%Y-%m-%d"))
    assert [row["kind"] for row in stored] == \
        [row["kind"] for row in readings[0]["conditions"]
         if row["kind"] not in TRANSIENT_CONDITION_KINDS]


def test_a_database_polluted_by_the_old_version_heals_on_read(tmp_path: Path) -> None:
    """旧库里可能已经堆了好几条 hunger。读取要滤掉它们，并把清理结果落定。"""
    moment = a_meal_moment()
    repo = repo_at(tmp_path, moment)
    date = moment.strftime("%Y-%m-%d")
    clean = repo.daily_state("u")
    persistent = stored_conditions(repo, "u", date)

    # 手工重放旧版本造成的污染：三条 hunger 加一条临时生活事件被写进了持久条件。
    hunger = meal_hunger_condition(moment, empty_chronotype())
    polluted = persistent + [hunger, hunger, hunger,
                             {"kind": "transient_life_event", "title": "旧版本留下的",
                              "label": "", "mood": "", "energyDelta": -4.0,
                              "intensity": 0.5, "cause": "transient", "phase": "active"}]
    with repo._connect() as db:
        db.execute(
            "UPDATE daily_state SET conditions_json=? WHERE user_id=? AND state_date=?",
            (json.dumps(polluted, ensure_ascii=False), "u", date),
        )
    assert len(stored_conditions(repo, "u", date)) == len(persistent) + 4

    # 第一次读取就该恢复到与从未被污染时完全一致的结果。
    healed = repo.daily_state("u")
    assert healed == clean
    assert stored_conditions(repo, "u", date) == persistent
    # 而且是真的写回去了：再读一次仍然一致。
    assert repo.daily_state("u") == clean


def test_a_corrupt_conditions_json_falls_back_without_crashing(tmp_path: Path) -> None:
    moment = a_meal_moment()
    repo = repo_at(tmp_path, moment)
    date = moment.strftime("%Y-%m-%d")
    baseline = repo.daily_state("u")
    for broken in ("{not json", '"a string"', "42", "null"):
        with repo._connect() as db:
            db.execute(
                "UPDATE daily_state SET conditions_json=? WHERE user_id=? AND state_date=?",
                (broken, "u", date),
            )
        assert repo.daily_state("u") == baseline, f"{broken} 没有安全回退"


@pytest.mark.parametrize("hour", [0, 6, 9, 12, 15, 18, 21, 23])
def test_no_hour_of_the_day_makes_reading_change_state(tmp_path: Path, hour: int) -> None:
    """扫一遍全天：任何时刻重复读取都不该改变结果。原来的 bug 只在饭点现形。"""
    moment = datetime(2026, 3, 5, hour, 35, tzinfo=timezone.utc)
    repo = repo_at(tmp_path, moment, name=f"hour-{hour}.sqlite3")
    readings = [repo.daily_state("u") for _ in range(4)]
    assert all(reading == readings[0] for reading in readings), f"{hour}:35 读取不幂等"


def test_the_default_wall_clock_repository_is_also_idempotent(tmp_path: Path) -> None:
    """回归原始失败的真正形状：两次独立读取，用真实时钟，结果必须一致。

    `test_living_companion_integration.py::test_daily_state_persists_and_flows_into_frame`
    就是这样读两次的——一次直接读，一次经组装链路读。它此前只在饭点窗口内失败，
    因此在别的时间跑是绿的。这条断言与当前几点无关，任何时刻都成立。
    """
    repo = MemoryRepository(tmp_path / "wall-clock.sqlite3")
    readings = [repo.daily_state("u") for _ in range(10)]
    assert all(reading == readings[0] for reading in readings)
    assert len({reading["energy"] for reading in readings}) == 1
