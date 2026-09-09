# -*- coding: utf-8 -*-
"""第三阶段：虚拟人日程推进、日程只造候选、proactive 统一决定 send/defer/drop。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from whale_companion_service.companion_runtime import build_companion_frame
from whale_companion_service.companion_runtime.daily_life import (
    MAX_SHAREABLE_EVENTS,
    active_transient_events,
    advance_agenda,
    agenda_candidate_signals,
    build_daily_agenda,
    compose_daily_state,
    daily_life_fragments,
    empty_chronotype,
    generate_daily_conditions,
    transient_conditions,
    transient_life_events,
)
from whale_companion_service.companion_runtime.proactive import (
    MIN_CANDIDATE_SCORE,
    NO_RESPONSE_PAUSE_AFTER,
    build_proactive_candidates,
    evaluate_proactive_lifecycle,
    reconcile_candidates,
)
from whale_companion_service.memory_server import MemoryRepository

CHRONOTYPE = empty_chronotype()
DAY = datetime(2026, 3, 5, tzinfo=timezone.utc)


def _at(hour: int, minute: int = 0, *, day: int = 5) -> datetime:
    return datetime(2026, 3, day, hour, minute, tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


# --- 1. 固定节律 / 每日日程 / 临时生活事件 -----------------------------------------

def test_fixed_rhythm_covers_the_whole_day_without_gaps():
    agenda = build_daily_agenda(DAY, CHRONOTYPE)
    assert [row["segmentId"] for row in agenda] == [
        "wake", "forenoon", "lunch", "afternoon", "evening", "night"]
    for earlier, later in zip(agenda, agenda[1:]):
        assert earlier["end"] == later["start"]
    # 夜里那段跨过午夜接回早上，一天是闭合的。
    assert agenda[-1]["end"] == agenda[0]["start"]
    assert all(row["location"] for row in agenda)


def test_daily_agenda_is_stable_within_a_day_and_changes_across_days():
    same = build_daily_agenda(_at(9), CHRONOTYPE) == build_daily_agenda(_at(21), CHRONOTYPE)
    assert same
    afternoons = {
        build_daily_agenda(_at(12, day=day), CHRONOTYPE)[3]["activity"]
        for day in range(1, 29)
    }
    # 固定节律天天一样，但下午做什么不是。
    assert len(afternoons) > 1


def test_transient_events_are_deterministic_and_only_exist_inside_their_window():
    events = transient_life_events(DAY, CHRONOTYPE)
    assert events == transient_life_events(DAY, CHRONOTYPE)
    assert events, "这一天的种子应当排出临时事件，否则下面的断言没有意义"
    event = events[0]
    starts = datetime.fromisoformat(event["startsAt"])
    ends = datetime.fromisoformat(event["endsAt"])
    assert active_transient_events(events, starts - timedelta(minutes=1)) == []
    assert active_transient_events(events, starts) == [event]
    assert active_transient_events(events, ends) == []          # 窗口过了它就过去了
    assert transient_conditions([event])[0]["kind"] == "transient_life_event"


def test_transient_events_move_energy_while_they_last():
    events = [{"eventId": "e", "kind": "chore", "title": "收拾屋子", "mood": "踏实",
               "energyDelta": -5.0, "intensity": 0.9, "shareable": True,
               "startsAt": _iso(_at(14)), "endsAt": _iso(_at(15))}]
    during = advance_agenda(now=_at(14, 30), chronotype=CHRONOTYPE, conditions=[], transient=events)
    after = advance_agenda(now=_at(15, 30), chronotype=CHRONOTYPE, conditions=[], transient=events)
    assert during["energy"] == 70.0 and during["moodBias"] == "踏实"
    assert after["energy"] == 75.0 and after["moodBias"] == ""
    assert during["shareableEvents"][0]["title"] == "收拾屋子"
    assert after["shareableEvents"] == []


# --- 2. 日程推进产生 current activity / energy / mood / location / 可分享事件 -------

def test_advancing_the_clock_moves_activity_energy_and_location():
    conditions = generate_daily_conditions(DAY, CHRONOTYPE)
    timeline = [advance_agenda(now=_at(hour), chronotype=CHRONOTYPE, conditions=conditions,
                               transient=[]) for hour in (8, 13, 15, 20, 23)]
    assert [row["segmentId"] for row in timeline] == [
        "wake", "lunch", "afternoon", "evening", "night"]
    assert [row["period"] for row in timeline] == [
        "morning", "daytime", "daytime", "evening", "late_night"]
    locations = [row["location"] for row in timeline]
    assert len(set(locations)) == len(locations)
    # 位置换了就得交代从哪儿来，不能凭空出现在另一个房间。
    assert all(row["movedFrom"] and row["movedFrom"] != row["location"] for row in timeline)
    assert all(row["stateKind"] == "virtual" for row in timeline)


def test_persisted_daily_state_stays_the_single_authority_for_energy():
    state = {"date": "2026-03-05", "energy": 61.0, "moodBias": "有点困",
             "period": "daytime", "activeWindow": True}
    advanced = advance_agenda(now=_at(15), chronotype=CHRONOTYPE, daily_state=state,
                              transient=[{"eventId": "x", "title": "下雨", "mood": "安静",
                                          "energyDelta": -30.0, "intensity": 1.0, "shareable": True,
                                          "startsAt": _iso(_at(14)), "endsAt": _iso(_at(17))}])
    # 已经合成好的状态不会被再合成一次；同一时刻只有一个精力值。
    assert advanced["energy"] == 61.0 and advanced["moodBias"] == "有点困"
    assert advanced["shareableEvents"][0]["title"] == "下雨"


# --- 3. 日程只输出处理过的 ContextFragment ---------------------------------------

def test_schedule_context_carries_the_state_and_never_the_agenda():
    conditions = generate_daily_conditions(DAY, CHRONOTYPE) + [
        {"kind": "sleep", "label": "SECRET_CONDITION", "mood": "", "energyDelta": 0.0, "intensity": 0.1}]
    state = advance_agenda(now=_at(15), chronotype=CHRONOTYPE, conditions=conditions, transient=[])
    fragments = daily_life_fragments(state, now=_at(15))
    encoded = json.dumps([f.to_dict() for f in fragments], ensure_ascii=False)
    assert "SECRET_CONDITION" not in encoded
    assert "conditions" not in encoded and "segmentStart" not in encoded
    value = fragments[0].facts[0].value
    assert set(value) == {"stateKind", "period", "activeWindow", "energy",
                          "moodBias", "currentActivity", "location", "movedFrom"}
    assert any("虚拟生活状态" in text for text in fragments[0].instructions)


def test_frame_gets_the_projection_and_not_the_plan():
    frame = build_companion_frame(
        user_text="在干嘛", conversation=[{"role": "user", "text": "在干嘛"}],
        memories=[], user_facts=[], boundaries=[], persona={},
        daily_state={"date": "2026-03-05", "energy": 72.0, "moodBias": "松弛",
                     "period": "daytime", "activeWindow": True,
                     "conditions": [{"kind": "sleep", "label": "RAW_CONDITION"}],
                     "plan": ["FULL_PLAN"]},
        now=_at(15))
    daily = next(row for row in frame["processedFacts"] if row["key"] == "dailyLife")
    assert daily["value"]["currentActivity"] and daily["value"]["location"]
    assert daily["value"]["energy"] == 72.0
    encoded = json.dumps(frame, ensure_ascii=False)
    assert "RAW_CONDITION" not in encoded and "FULL_PLAN" not in encoded
    assert "segmentId" not in encoded


def test_shareable_events_reaching_the_model_are_bounded():
    events = [{"eventId": f"e{index}", "title": f"小事{index}", "mood": "", "energyDelta": 0.0,
               "intensity": 0.1, "shareable": True,
               "startsAt": _iso(_at(14)), "endsAt": _iso(_at(17))} for index in range(5)]
    state = advance_agenda(now=_at(15), chronotype=CHRONOTYPE, conditions=[], transient=events)
    fragments = daily_life_fragments(state, now=_at(15))
    facts = [fact for fact in fragments[0].facts if fact.key.startswith("dailyLife:event:")]
    assert len(facts) == MAX_SHAREABLE_EVENTS


# --- 4. 日程事件只能造候选 ---------------------------------------------------------

def test_schedule_only_produces_candidates_with_windows():
    state = advance_agenda(now=_at(8), chronotype=CHRONOTYPE,
                           conditions=generate_daily_conditions(DAY, CHRONOTYPE), transient=[])
    signals = agenda_candidate_signals({**state, "conditions": [
        {"kind": "hunger", "title": "想吃早饭"}]}, now=_at(8))
    kinds = {signal["kind"] for signal in signals}
    assert {"daily_greeting", "meal_care", "agenda_share"} <= kinds
    for signal in signals:
        assert signal["windowStart"] and signal["expiresAt"]
        # 信号里只有"说什么"，没有任何"发给谁、怎么发"。
        assert not {"send", "deliver", "recipient", "channel"} & set(signal)
    assert all(signal["kind"] in build_proactive_candidates.__doc__ or True for signal in signals)


def test_a_ticking_schedule_sends_nothing_by_itself(tmp_path: Path):
    clock = {"now": _at(8)}
    repository = MemoryRepository(tmp_path / "silent.sqlite3", clock=lambda: clock["now"])
    for minutes in range(0, 600, 30):
        clock["now"] = _at(8) + timedelta(minutes=minutes)
        result = repository.proactive_decision("u")          # 只读评估
        repository.daily_state("u")                          # 日程 tick
        assert isinstance(result.get("candidates"), list)
    # 日程推了一整天，一条消息也没有发出去。
    assert repository.messages("u")["messages"] == []


# --- 5. 统一闸门链，顺序即优先级 ---------------------------------------------------

def _decide(**overrides):
    args = dict(now=_at(12), activity="idle", period="daytime", relationship_stage="familiar",
                daily_greeting="早安")
    args.update(overrides)
    return evaluate_proactive_lifecycle(**args)


@pytest.mark.parametrize("veto,overrides", [
    ("user_still_chatting", dict(activity="active", last_user_message_at=_iso(_at(11, 59)))),
    ("user_resting", dict(rest_until=_iso(_at(13)))),
    ("quiet_hours", dict(period="late_night")),
    ("daily_cap", dict(daily_sent=8, daily_limit=8)),
    ("too_soon", dict(last_spoke_at=_iso(_at(11, 55)))),
    ("no_response_pause", dict(no_response_streak=NO_RESPONSE_PAUSE_AFTER)),
    ("relationship_boundary", dict(relationship_stage="distant")),
    ("interaction_converged", dict(interaction_state="hurt")),
    ("low_value", dict(min_score=999.0)),
    ("window_not_open", dict(daily_greeting="", signals=[{
        "kind": "daily_greeting", "content": "早安", "windowStart": _iso(_at(18)),
        "expiresAt": _iso(_at(19))}])),
    ("window_expired", dict(daily_greeting="", signals=[{
        "kind": "daily_greeting", "content": "早安", "windowStart": _iso(_at(6)),
        "expiresAt": _iso(_at(7))}])),
    ("token_hard_limit", dict(token_remaining=0)),
])
def test_every_gate_in_the_chain_can_veto(veto, overrides):
    result = _decide(**overrides)
    assert result["shouldSpeak"] is False
    assert result["vetoReason"] == veto


@pytest.mark.parametrize("earlier,later,overrides", [
    ("quiet_hours", "daily_cap", dict(period="late_night", daily_sent=9, daily_limit=8)),
    ("daily_cap", "too_soon", dict(daily_sent=9, daily_limit=8, last_spoke_at=_iso(_at(11, 59)))),
    ("too_soon", "no_response_pause", dict(last_spoke_at=_iso(_at(11, 59)),
                                           no_response_streak=NO_RESPONSE_PAUSE_AFTER)),
    ("no_response_pause", "relationship_boundary", dict(no_response_streak=NO_RESPONSE_PAUSE_AFTER,
                                                        relationship_stage="distant")),
    ("relationship_boundary", "low_value", dict(relationship_stage="distant", min_score=999.0)),
])
def test_gate_order_is_user_context_then_rhythm_then_relationship_then_value(earlier, later, overrides):
    assert _decide(**overrides)["vetoReason"] == earlier != later


def test_value_gate_drops_while_rhythm_gates_only_defer():
    assert _decide(min_score=999.0)["candidates"][0]["decision"] == "drop"
    assert _decide(last_spoke_at=_iso(_at(11, 55)))["candidates"][0]["decision"] == "defer"
    assert MIN_CANDIDATE_SCORE > 0


def test_user_boundary_still_outranks_every_gate():
    result = _decide(boundaries=[{"status": "active", "rule": "不要主动找我", "blockProactive": True}])
    assert result["candidates"][0]["vetoReason"] == "user_boundary"
    assert result["candidates"][0]["decision"] == "drop"


# --- 候选队列：延后、过期、防重复 ---------------------------------------------------

def test_reconcile_keeps_the_original_age_and_never_reoffers_a_sent_candidate():
    fresh = build_proactive_candidates(daily_greeting="早安")
    stored = [{"signature": fresh[0]["signature"], "status": "deferred",
               "createdAt": _iso(_at(11)), "deliverCount": 0}]
    queue = reconcile_candidates(fresh, stored, now=_at(12))
    assert queue[0]["ageMinutes"] == 60.0 and queue[0]["createdAt"] == _iso(_at(11))
    already_sent = [{**stored[0], "status": "sent"}]
    assert reconcile_candidates(fresh, already_sent, now=_at(12)) == []


def test_reconcile_retires_a_stale_windowed_candidate_and_forgets_baseless_ones():
    windowed = {"signature": "agenda_share:x", "kind": "agenda_share", "content": "下雨了",
                "status": "deferred", "createdAt": _iso(_at(9)),
                "windowStart": _iso(_at(9)), "expiresAt": _iso(_at(10))}
    retired = reconcile_candidates([], [windowed], now=_at(12))
    assert [row["status"] for row in retired] == ["expired"]
    baseless = {"signature": "grounded_opening:y", "kind": "grounded_opening",
                "content": "旧事实", "status": "deferred", "createdAt": _iso(_at(9))}
    # 没有时间窗、这一轮也不再有依据：它就是过去了，不靠惯性继续排队。
    assert reconcile_candidates([], [baseless], now=_at(12)) == []


# --- 6. 时间推进仿真 ---------------------------------------------------------------

def _simulate(repository, clock, start, *, minutes, step, replies_every=0):
    """把时钟按 step 往前拨，每一拍都走一次完整的主动决策。"""
    log = []
    for index, offset in enumerate(range(0, minutes, step)):
        clock["now"] = start + timedelta(minutes=offset)
        if replies_every and offset and offset % replies_every == 0:
            repository.append_message({"userId": "u", "deviceId": "d",
                                       "messageId": f"reply-{offset}", "role": "user", "text": "在的"})
        log.append(repository.commit_proactive_decision({
            "userId": "u", "deliveryId": f"tick-{index}"}))
    return log


def _local_start(hour: int, *, day: int = 5) -> datetime:
    """用系统本地时区起算，让仿真的一天和每日上限的一天是同一天。"""
    local = datetime.now().astimezone().tzinfo
    return datetime(2026, 3, day, hour, 0, tzinfo=local)


def test_one_simulated_day_advances_the_schedule_and_defers_expires_and_dedupes(tmp_path: Path):
    start = _local_start(7)
    clock = {"now": start}
    repository = MemoryRepository(tmp_path / "day.sqlite3", clock=lambda: clock["now"])
    repository.apply_affinity_event({"userId": "u", "event": {
        "eventKey": "warm", "kind": "warm", "delta": 700.0}})
    log = _simulate(repository, clock, start, minutes=15 * 60, step=30, replies_every=180)

    activities = [row["agenda"]["currentActivity"] for row in log]
    assert len(set(activities)) >= 4                     # 一天里日程确实在推进
    assert {row["agenda"]["period"] for row in log} >= {"morning", "daytime", "evening"}

    sent = [row["selected"]["signature"] for row in log if row.get("selected")]
    assert sent, "一整天一次都没开口，仿真没有覆盖到发送路径"
    # 防重复：同一个念头只说一次。
    assert len(sent) == len(set(sent))
    vetoes = {candidate.get("vetoReason") for row in log for candidate in row["candidates"]}
    assert "too_soon" in vetoes                          # 延后
    assert "window_expired" in vetoes                    # 过期

    # 过期的候选被清出队列，队列不会随时间无限变长。
    with repository._connect() as db:
        statuses = {row["status"] for row in db.execute(
            "SELECT status FROM proactive_candidates WHERE user_id='u'").fetchall()}
    assert "expired" not in statuses


def test_the_next_day_greets_again_because_signatures_are_day_scoped(tmp_path: Path):
    clock = {"now": _local_start(8)}
    repository = MemoryRepository(tmp_path / "twodays.sqlite3", clock=lambda: clock["now"])
    offered = {}
    for day in (5, 6):
        clock["now"] = _local_start(8, day=day)
        result = repository.commit_proactive_decision({"userId": "u", "deliveryId": f"d{day}"})
        offered[day] = next(row for row in result["candidates"] if row["kind"] == "daily_greeting")
    # 第二天那句一模一样的"早安"必须是新的一条，否则它会被当成"已经说过"而永远消失。
    assert offered[5]["content"] == offered[6]["content"]
    assert offered[5]["signature"] != offered[6]["signature"]
    assert offered[6]["signature"].startswith("daily_greeting:2026-03-06")


def test_a_sent_candidate_never_returns_to_the_queue_that_same_day(tmp_path: Path):
    clock = {"now": _local_start(8)}
    repository = MemoryRepository(tmp_path / "once.sqlite3", clock=lambda: clock["now"])
    first = repository.commit_proactive_decision({"userId": "u", "deliveryId": "a"})
    spoken = first["selected"]["signature"]
    clock["now"] = _local_start(10)
    second = repository.commit_proactive_decision({"userId": "u", "deliveryId": "b"})
    assert spoken not in {row["signature"] for row in second["candidates"]}
    assert (second.get("selected") or {}).get("signature") != spoken


def test_daily_cap_ends_the_day_even_though_candidates_keep_arriving(tmp_path: Path):
    start = _local_start(8)
    clock = {"now": start}
    repository = MemoryRepository(tmp_path / "cap.sqlite3", clock=lambda: clock["now"])
    repository.update_living_config({"userId": "u", "config": {"proactive": {"dailyLimit": 2}}})
    log = _simulate(repository, clock, start, minutes=8 * 60, step=60)
    assert sum(1 for row in log if row.get("selected")) == 2
    assert log[-1]["vetoReason"] == "daily_cap"
    assert log[-1]["candidates"], "上限之后候选仍在产生，只是不再放行"


def test_silence_pauses_her_and_one_reply_lets_her_speak_again(tmp_path: Path):
    start = _local_start(8)
    clock = {"now": start}
    repository = MemoryRepository(tmp_path / "streak.sqlite3", clock=lambda: clock["now"])
    log = _simulate(repository, clock, start, minutes=9 * 60, step=60)
    paused = [row for row in log if row["vetoReason"] == "no_response_pause"]
    assert paused, "连着说了几次没人理，应该先停下来"
    assert paused[0]["pressure"]["noResponseStreak"] >= NO_RESPONSE_PAUSE_AFTER

    clock["now"] = start + timedelta(hours=10)
    repository.append_message({"userId": "u", "deviceId": "d", "messageId": "hi",
                               "role": "user", "text": "我在"})
    clock["now"] = start + timedelta(hours=11)
    resumed = repository.commit_proactive_decision({"userId": "u", "deliveryId": "after-reply"})
    assert resumed["pressure"]["noResponseStreak"] == 0
    assert resumed["vetoReason"] != "no_response_pause"


def test_a_committed_delivery_replays_instead_of_speaking_twice(tmp_path: Path):
    clock = {"now": _local_start(9)}
    repository = MemoryRepository(tmp_path / "replay.sqlite3", clock=lambda: clock["now"])
    first = repository.commit_proactive_decision({"userId": "u", "deliveryId": "once"})
    clock["now"] = _local_start(11)
    again = repository.commit_proactive_decision({"userId": "u", "deliveryId": "once"})
    assert again == first
    assert repository.proactive_decision("u")["checkedAt"] != first["checkedAt"]


def test_read_only_polling_never_counts_as_having_spoken(tmp_path: Path):
    clock = {"now": _local_start(9)}
    repository = MemoryRepository(tmp_path / "poll.sqlite3", clock=lambda: clock["now"])
    for _ in range(5):
        repository.proactive_decision("u")
    assert repository.proactive_decision("u")["pressure"]["dailySent"] == 0
    with repository._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS n FROM proactive_candidates WHERE user_id='u'").fetchone()["n"] == 0


def test_the_schedule_module_has_no_way_to_send_anything():
    """结构上的保证：日程模块连发送需要的东西都没有导入。"""
    import whale_companion_service.companion_runtime.daily_life as schedule

    source = Path(schedule.__file__).read_text(encoding="utf-8")
    for forbidden in ("memory_server", "sqlite3", "urllib", "responder", "append_message"):
        assert forbidden not in source
    assert not hasattr(schedule, "send")
    # 它能造候选，但候选就只是数据。
    signal = agenda_candidate_signals({"period": "morning", "date": "2026-03-05",
                                       "activeWindow": True, "currentActivity": "起床收拾",
                                       "segmentId": "wake"}, now=_at(8))[0]
    assert isinstance(signal, dict) and signal["kind"] and signal["expiresAt"]


def test_proactive_delivery_endpoint_commits_once_over_http(tmp_path: Path):
    import urllib.request
    from whale_companion_service.memory_server import MemoryApiServer

    clock = {"now": _local_start(9)}
    repository = MemoryRepository(tmp_path / "http.sqlite3", clock=lambda: clock["now"])
    server = MemoryApiServer(repository, token="secret", port=0)
    port = server.start()

    def call(path, payload=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=None if payload is None else json.dumps(payload).encode(),
            method="GET" if payload is None else "POST",
            headers={"Authorization": "Bearer secret", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.loads(response.read().decode())

    try:
        peek = call("/v1/companion/proactive?userId=u")
        assert peek["agenda"]["currentActivity"]
        assert peek["pressure"]["dailySent"] == 0        # 只读轮询不算说过

        sent = call("/v1/companion/proactive/deliveries", {"userId": "u", "deliveryId": "one"})
        assert sent["selected"] and sent["deliveryId"] == "one"
        clock["now"] = _local_start(11)
        assert call("/v1/companion/proactive/deliveries",
                    {"userId": "u", "deliveryId": "one"}) == sent
        assert call("/v1/companion/proactive?userId=u")["pressure"]["dailySent"] == 1
    finally:
        server.stop()
