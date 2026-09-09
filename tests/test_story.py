# -*- coding: utf-8 -*-
"""第五阶段：最小故事线——分支、暂停、恢复、完成、防重复与跨会话。"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from whale_companion_service.companion_runtime.proactive import (
    CANDIDATE_BASE_SCORE,
    evaluate_proactive_lifecycle,
)
from whale_companion_service.companion_runtime.story import (
    ARC_STATUSES,
    BEAT_COOLDOWN_HOURS,
    REFUSAL_COOLDOWN_HOURS,
    ArcStoryProvider,
    StoryArc,
    advance_arc,
    apply_story_gates,
    arc_age_days,
    choose_branch,
    eligible_templates,
    refuse_arc,
    select_current_arcs,
    start_arc,
    story_candidate_signals,
    story_templates,
)
from whale_companion_service.memory_server import MemoryRepository

NOW = datetime(2026, 5, 1, 12, tzinfo=timezone.utc)
OPEN = {"allowSharedMemory": True, "allowProactiveCare": True}


def _later(**delta) -> datetime:
    return NOW + timedelta(**delta)


def _gates(arcs, *, permissions=None, boundary_state="open", period="daytime", now=None):
    return apply_story_gates(arcs, permissions=permissions or OPEN,
                             boundary_state=boundary_state, period=period, now=now or NOW)


# --- 1. StoryArc 数据结构与模板 -----------------------------------------------------

def test_story_arc_carries_every_required_field():
    arc = start_arc("shared:getting_to_know", now=NOW, memory_ids=["m1"])
    assert set(arc.to_dict()) >= {
        "id", "type", "premise", "status", "current_beat", "prerequisites",
        "related_memory_ids", "next_eligible_at", "completed_beats", "available_branches"}
    assert arc.status in ARC_STATUSES
    assert arc.related_memory_ids == ("m1",)
    assert StoryArc.from_dict(json.loads(json.dumps(arc.to_dict()))) == arc


def test_there_are_exactly_two_shared_and_two_life_templates():
    templates = story_templates()
    assert [row["type"] for row in templates].count("shared") == 2
    assert [row["type"] for row in templates].count("companion_life") == 2
    assert {row["weight"] for row in templates} == {"main", "light"}
    for row in templates:
        assert 3 <= len(row["beats"]) <= 4, row["id"]      # 骨架，不是内容系统
        assert row["beats"][-1]["advance_when"] == ()      # 每条线都有终点


# --- 2. 进度只由真实事件驱动 --------------------------------------------------------

def test_a_beat_never_advances_without_the_event_that_earns_it():
    arc = replace(start_arc("shared:getting_to_know", now=NOW), next_eligible_at="")
    assert advance_arc(arc, {"knownFactCount": 0}, now=NOW).current_beat == "no_name"
    # 只有真的知道了一件关于用户的事，这一格才走得动。
    moved = advance_arc(arc, {"knownFactCount": 1}, now=NOW)
    assert moved.current_beat == "first_facts" and moved.completed_beats == ("no_name",)


def test_unknown_conditions_never_open_the_gate():
    arc = replace(start_arc("shared:getting_to_know", now=NOW), next_eligible_at="")
    invented = replace(arc, current_beat="no_name")
    # 认不出的条件一律不满足：未知不等于放行。
    from whale_companion_service.companion_runtime.story import conditions_met
    assert conditions_met(({"signal": "vibes_are_good"},), {"vibes_are_good": True}) is False
    assert advance_arc(invented, {}, now=NOW).current_beat == "no_name"


def test_the_cooldown_stops_the_story_from_becoming_a_progress_bar():
    arc = start_arc("shared:getting_to_know", now=NOW)
    assert advance_arc(arc, {"knownFactCount": 9}, now=NOW).current_beat == "no_name"
    ready = advance_arc(arc, {"knownFactCount": 9}, now=_later(hours=BEAT_COOLDOWN_HOURS))
    assert ready.current_beat == "first_facts"


def test_prerequisites_decide_which_templates_may_start():
    # 一开场只有两条够格：认识彼此这条线，和她窗台上那盆东西。
    assert [row["id"] for row in eligible_templates([], {})] == [
        "shared:getting_to_know", "life:the_window_plant"]
    grown = eligible_templates([], {"relationshipStage": "familiar", "threadMentions": 2,
                                    "timelineEvents": 1})
    assert len(grown) == 4


def test_life_story_advances_on_elapsed_days_not_on_conversation():
    arc = start_arc("life:the_window_plant", now=NOW)
    assert arc_age_days(arc, _later(days=3)) == pytest.approx(3.0)
    still = advance_arc(arc, {"daysSinceStart": 2}, now=_later(days=2))
    assert still.current_beat == "planted"
    sprouted = advance_arc(arc, {"daysSinceStart": 3}, now=_later(days=3))
    assert sprouted.current_beat == "sprout"
    # 走过一格之后，"起了多少天"不会跟着归零。
    assert arc_age_days(sprouted, _later(days=3)) == pytest.approx(3.0)


# --- 3. 最多一条主线 + 一条轻支线 ----------------------------------------------------

def test_only_one_main_and_one_light_arc_are_ever_current():
    arcs = [start_arc(row["id"], now=NOW) for row in story_templates()]
    current = select_current_arcs(arcs)
    assert len(current) == 2
    assert {arc.weight for arc in current} == {"main", "light"}
    assert len(ArcStoryProvider(arcs).context_fragments(now=NOW)) == 2


# --- 4. 只输出当前节点 --------------------------------------------------------------

def test_the_context_gets_the_current_beat_and_none_of_the_history():
    arc = advance_arc(replace(start_arc("shared:getting_to_know", now=NOW), next_eligible_at=""),
                      {"knownFactCount": 1}, now=NOW)
    fragment = ArcStoryProvider([arc]).context_fragments(now=NOW)[0]
    value = fragment.facts[0].value
    assert value["currentBeat"]["id"] == "first_facts"
    assert set(value) == {"arcId", "type", "premise", "currentBeat", "availableBranches"}
    encoded = json.dumps(fragment.to_dict(), ensure_ascii=False)
    assert "completed_beats" not in encoded and "no_name" not in encoded
    assert "advance_when" not in encoded and "prerequisites" not in encoded


def test_related_memories_travel_as_provenance_not_as_content():
    arc = start_arc("shared:the_unfinished_thing", now=NOW, memory_ids=["mem-1", "mem-2"])
    fragment = ArcStoryProvider([arc]).context_fragments(now=NOW)[0]
    assert fragment.source_event_ids == ("mem-1", "mem-2")
    assert "mem-1" not in json.dumps(fragment.facts[0].value, ensure_ascii=False)


# --- 5. 故事只能造候选 --------------------------------------------------------------

def test_story_beats_become_candidates_and_go_through_the_same_gates():
    arc = start_arc("life:the_window_plant", now=NOW)
    signals = story_candidate_signals([arc], now=_later(hours=BEAT_COOLDOWN_HOURS))
    assert signals and signals[0]["kind"] == "story_beat"
    assert signals[0]["expiresAt"] and signals[0]["signature"].startswith("story_beat:")
    assert "story_beat" in CANDIDATE_BASE_SCORE
    # 候选就只是数据：里面没有任何"发送"的能力。
    assert not {"send", "deliver", "recipient"} & set(signals[0])

    result = evaluate_proactive_lifecycle(now=_later(hours=BEAT_COOLDOWN_HOURS), activity="idle",
                                          period="daytime", relationship_stage="familiar",
                                          signals=signals)
    assert result["selected"]["kind"] == "story_beat"
    blocked = evaluate_proactive_lifecycle(now=_later(hours=BEAT_COOLDOWN_HOURS), activity="idle",
                                           period="late_night", relationship_stage="familiar",
                                           signals=signals)
    assert blocked["vetoReason"] == "quiet_hours"


def test_the_story_module_has_no_way_to_send_anything():
    import whale_companion_service.companion_runtime.story as story

    source = Path(story.__file__).read_text(encoding="utf-8")
    for forbidden in ("memory_server", "sqlite3", "urllib", "responder", "append_message"):
        assert forbidden not in source


def test_a_beat_that_is_still_cooling_offers_no_candidate():
    arc = start_arc("life:the_window_plant", now=NOW)
    assert story_candidate_signals([arc], now=NOW) == []


# --- 6. 拒绝 / 免打扰 / 权限不足时暂停 ------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {"permissions": {"allowSharedMemory": False, "allowProactiveCare": True}},
    {"boundary_state": "restricted"},
    {"period": "late_night"},
])
def test_a_shared_arc_pauses_when_it_is_not_welcome(kwargs):
    arc = start_arc("shared:getting_to_know", now=NOW)
    assert _gates([arc], **kwargs)[0].status == "paused"


def test_her_own_life_story_does_not_need_permission_to_reference_your_shared_past():
    arc = start_arc("life:the_window_plant", now=NOW)
    stranger = {"allowSharedMemory": False, "allowProactiveCare": False}
    assert _gates([arc], permissions=stranger)[0].status == "active"
    # 但用户划下的线照样把它停住。
    assert _gates([arc], permissions=stranger, boundary_state="guarded")[0].status == "paused"


def test_a_paused_arc_resumes_when_the_condition_lifts():
    arc = start_arc("shared:getting_to_know", now=NOW)
    paused = _gates([arc], period="late_night")[0]
    resumed = _gates([paused], now=_later(hours=9))[0]
    assert resumed.status == "active" and resumed.current_beat == paused.current_beat
    assert resumed.completed_beats == paused.completed_beats


def test_refusing_a_story_pauses_it_for_a_while_instead_of_deleting_it():
    arc = start_arc("shared:getting_to_know", now=NOW)
    refused = refuse_arc(arc, now=NOW)
    assert refused.status == "paused"
    # 隔天又端上来不算尊重，得等满冷却。
    assert _gates([refused], now=_later(hours=24))[0].status == "paused"
    assert _gates([refused], now=_later(hours=REFUSAL_COOLDOWN_HOURS + 1))[0].status == "active"
    # 它没有被抹掉，走过的路还在。
    assert refused.current_beat == arc.current_beat


def test_a_paused_arc_never_advances_or_speaks():
    arc = refuse_arc(replace(start_arc("life:the_window_plant", now=NOW), next_eligible_at=""), now=NOW)
    assert advance_arc(arc, {"daysSinceStart": 99}, now=_later(days=99)) == arc
    assert story_candidate_signals([arc], now=_later(days=99)) == []


# --- 7. 分支与完成 ------------------------------------------------------------------

def test_a_branching_beat_waits_for_the_user_to_pick():
    arc = replace(start_arc("shared:the_unfinished_thing", now=NOW), next_eligible_at="")
    assert [row["id"] for row in arc.available_branches] == ["ask", "wait"]
    # 条件够了也不自己走——她不替用户决定故事往哪边去。
    assert advance_arc(arc, {"threadMentions": 99}, now=NOW).current_beat == "reopened"

    asked = choose_branch(arc, "ask", now=NOW)
    waited = choose_branch(arc, "wait", now=NOW)
    assert asked.current_beat == "checking_in" and waited.current_beat == "holding"
    assert asked.completed_beats == ("reopened",) and asked.available_branches == ()


def test_both_branches_converge_on_the_same_ending():
    for branch, beat in (("ask", "checking_in"), ("wait", "holding")):
        arc = choose_branch(replace(start_arc("shared:the_unfinished_thing", now=NOW),
                                    next_eligible_at=""), branch, now=NOW)
        assert arc.current_beat == beat
        resolved = advance_arc(arc, {"threadResolved": True}, now=_later(hours=BEAT_COOLDOWN_HOURS))
        assert resolved.current_beat == "let_go"
        done = advance_arc(resolved, {}, now=_later(hours=BEAT_COOLDOWN_HOURS * 2))
        assert done.status == "completed" and done.available_branches == ()


def test_a_completed_arc_stays_completed_and_stops_talking():
    arc = replace(start_arc("life:the_window_plant", now=NOW), current_beat="growing",
                  next_eligible_at="")
    done = advance_arc(arc, {}, now=NOW)
    assert done.status == "completed"
    assert advance_arc(done, {"daysSinceStart": 999}, now=_later(days=999)) == done
    assert _gates([done])[0].status == "completed"        # 闸门不会把它复活
    assert select_current_arcs([done]) == []
    assert story_candidate_signals([done], now=_later(days=1)) == []


# --- 8. 仓库集成：跨会话与防重复 ------------------------------------------------------

def _repo(tmp_path: Path, name: str, hour: int = 10):
    local = datetime.now().astimezone().tzinfo
    clock = {"now": datetime(2026, 5, 1, hour, 0, tzinfo=local)}
    return MemoryRepository(tmp_path / name, clock=lambda: clock["now"]), clock


def test_story_progress_survives_a_restart(tmp_path: Path):
    path = tmp_path / "cross.sqlite3"
    local = datetime.now().astimezone().tzinfo
    clock = {"now": datetime(2026, 5, 1, 10, 0, tzinfo=local)}
    first = MemoryRepository(path, clock=lambda: clock["now"])
    first.story_state("u")
    clock["now"] += timedelta(days=4)
    before = first.story_state("u")
    plant = next(row for row in before["arcs"] if row["id"] == "life:the_window_plant")
    assert plant["current_beat"] == "sprout" and list(plant["completed_beats"]) == ["planted"]

    # 换一个进程重新打开同一个库：故事接着上次那一格。
    second = MemoryRepository(path, clock=lambda: clock["now"])
    reopened = next(row for row in second.story_state("u")["arcs"]
                    if row["id"] == "life:the_window_plant")
    assert reopened["current_beat"] == "sprout"
    assert reopened["started_at"] == plant["started_at"]


def test_the_repository_never_runs_more_than_one_arc_per_weight(tmp_path: Path):
    repository, clock = _repo(tmp_path, "slots.sqlite3")
    repository.adjust_affinity({"userId": "u", "score": 650.0})
    for step in range(6):
        clock["now"] += timedelta(days=1)
        state = repository.story_state("u")
    weights = [row["weight"] for row in state["arcs"]
               if row["status"] in {"active", "paused"}]
    assert weights.count("main") <= 1 and weights.count("light") <= 1
    assert len(state["current"]) <= 2


def test_a_story_beat_is_only_delivered_once(tmp_path: Path):
    repository, clock = _repo(tmp_path, "once.sqlite3", hour=9)
    repository.adjust_affinity({"userId": "u", "score": 650.0})
    delivered = []
    for step in range(36):
        clock["now"] += timedelta(hours=1)
        # 用户偶尔回一句，否则未回应闸门会先把所有候选拦下来，测不到故事这条路。
        # 回完再隔一小时才做决策，免得最小间隔把每一拍都挡掉。
        if step % 3 == 0:
            repository.append_message({"userId": "u", "deviceId": "d",
                                       "messageId": f"r{step}", "role": "user", "text": "嗯"})
        clock["now"] += timedelta(hours=1)
        result = repository.commit_proactive_decision({"userId": "u", "deliveryId": f"d{step}"})
        selected = result.get("selected") or {}
        if selected.get("kind") == "story_beat":
            delivered.append(selected["signature"])
    assert delivered, "一天多下来一格故事都没讲，仿真没覆盖到这条路"
    assert len(delivered) == len(set(delivered))      # 同一格不会讲第二遍


def test_the_user_can_pick_a_branch_and_refuse_a_line(tmp_path: Path):
    repository, clock = _repo(tmp_path, "act.sqlite3")
    repository.story_state("u")
    arc_id = "life:the_window_plant"
    refused = repository.act_on_story({"userId": "u", "arcId": arc_id, "action": "refuse"})
    assert refused["arc"]["status"] == "paused"
    clock["now"] += timedelta(hours=1)
    assert next(row for row in repository.story_state("u")["arcs"]
                if row["id"] == arc_id)["status"] == "paused"

    with pytest.raises(Exception):
        repository.act_on_story({"userId": "u", "arcId": arc_id,
                                 "action": "choose", "branchId": "nope"})
    with pytest.raises(Exception):
        repository.act_on_story({"userId": "u", "arcId": "life:nonexistent", "action": "refuse"})


def test_a_drawn_line_pauses_every_story_the_same_turn(tmp_path: Path):
    repository, clock = _repo(tmp_path, "line.sqlite3")
    repository.adjust_affinity({"userId": "u", "score": 650.0})
    assert repository.story_state("u")["current"]

    clock["now"] += timedelta(minutes=10)
    repository.append_message({"userId": "u", "deviceId": "d", "messageId": "stop",
                               "role": "user", "text": "以后别再主动找我了"})
    state = repository.story_state("u")
    assert state["current"] == []
    assert all(row["status"] == "paused" for row in state["arcs"])
    decision = repository.commit_proactive_decision({"userId": "u", "deliveryId": "after"})
    assert not any(row["kind"] == "story_beat" for row in decision["candidates"])


def test_story_endpoints_are_reachable_over_http(tmp_path: Path):
    import urllib.request
    from whale_companion_service.memory_server import MemoryApiServer

    repository, _clock = _repo(tmp_path, "http.sqlite3")
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
        state = call("/v1/companion/story?userId=u")
        assert state["arcs"] and state["current"]
        acted = call("/v1/companion/story/actions",
                     {"userId": "u", "arcId": state["current"][0], "action": "refuse"})
        assert acted["arc"]["status"] == "paused"
    finally:
        server.stop()
