# -*- coding: utf-8 -*-
"""第四阶段：七维内部关系状态、对外只给权限、演进/越界/修复/降温与结构约束。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from whale_companion_service.companion_runtime import build_companion_frame
from whale_companion_service.companion_runtime.affinity import (
    DAILY_POSITIVE_CAP,
    FAMILIARITY_HALF_LIFE_DAYS,
    HYSTERESIS,
    INTIMATE_MIN_TRUST,
    NICKNAME_MIN_TRUST,
    SOLICITED_POSITIVE_CAP,
    TRUST_HALF_LIFE_DAYS,
    WARMTH_HALF_LIFE_DAYS,
    apology_repair_delta,
    apply_event,
    boundary_violation_delta,
    decay_state,
    derive_boundary_state,
    empty_affinity_state,
    normalize_affinity_state,
    relationship_constraints,
    relationship_permissions,
    stage_key_of,
)
from whale_companion_service.companion_runtime.proactive import evaluate_proactive_lifecycle
from whale_companion_service.companion_runtime.relationship import build_relationship
from whale_companion_service.memory_server import MemoryRepository

NOW = datetime(2026, 4, 2, 12, tzinfo=timezone.utc)
PERMISSION_KEYS = {
    "allowProactiveCare", "allowSharedMemory", "allowNickname",
    "allowPlayful", "allowIntimateTone", "proactiveQuota",
}


def _event(key="e1", kind="warm", delta=2.0, at=None, **extra) -> dict:
    return {"eventKey": key, "kind": kind, "delta": delta, "at": (at or NOW).isoformat(), **extra}


def _state(**overrides) -> dict:
    base = {"score": 700.0, "stage": "close", "interaction": "close",
            "familiarity": 0.6, "trust": 0.6, "warmth": 0.5,
            "boundaryState": "open", "updatedAt": NOW.isoformat()}
    base.update(overrides)
    return normalize_affinity_state(base)


# --- 1. 内部状态是七个维度，不是一个数字 -------------------------------------------

def test_internal_state_carries_all_seven_dimensions():
    assert set(empty_affinity_state()) == {
        "score", "stage", "interaction", "familiarity", "trust", "warmth",
        "boundaryState", "updatedAt"}


def test_legacy_rows_upgrade_without_migration():
    # 升级前落库的行只有三个字段，读进来该被补齐而不是报错。
    upgraded = normalize_affinity_state({"score": 250.0, "stage": "familiar", "interaction": "warm"})
    assert upgraded["familiarity"] == upgraded["trust"] == upgraded["warmth"] == 0.0
    assert upgraded["boundaryState"] == "open"
    assert normalize_affinity_state(None) == empty_affinity_state()
    assert normalize_affinity_state({"stage": "不存在的档"})["stage"] == "acquaintance"


def test_the_three_continuous_dimensions_move_independently():
    warm, _ = apply_event(empty_affinity_state(), [], _event(delta=2.0), now=NOW)
    assert warm["warmth"] > warm["trust"] > 0.0          # 暖意涨得快，信任涨得慢
    assert warm["familiarity"] > 0.0

    hurt, _ = apply_event(warm, [], _event("v1", "boundary", -7.0), now=NOW)
    # 吵一架：信任和暖意掉了，但"认识"这件事不会因此取消。
    assert hurt["trust"] < warm["trust"]
    assert hurt["warmth"] < warm["warmth"]
    assert hurt["familiarity"] > warm["familiarity"]


def test_each_dimension_cools_on_its_own_clock():
    later = NOW + timedelta(days=TRUST_HALF_LIFE_DAYS)
    cooled = decay_state(_state(), now=later)
    assert cooled["warmth"] == pytest.approx(0.0, abs=0.01)         # 两天半衰，一个月后凉透
    assert cooled["trust"] == pytest.approx(0.3, abs=0.02)          # 正好一个半衰期
    assert cooled["familiarity"] > 0.5                              # 半年半衰，还记得
    assert WARMTH_HALF_LIFE_DAYS < TRUST_HALF_LIFE_DAYS < FAMILIARITY_HALF_LIFE_DAYS


# --- 2. 对模型只给权限，不给评分 ---------------------------------------------------

def test_permissions_say_what_is_allowed_and_never_how_much():
    permissions = relationship_permissions(_state())
    assert set(permissions) == PERMISSION_KEYS
    assert not {"score", "trust", "warmth", "familiarity", "stage"} & set(permissions)


def test_permissions_open_up_as_the_relationship_grows():
    stranger = relationship_permissions(empty_affinity_state())
    assert stranger == {"allowProactiveCare": False, "allowSharedMemory": False,
                        "allowNickname": False, "allowPlayful": False,
                        "allowIntimateTone": False, "proactiveQuota": 0}
    close = relationship_permissions(_state())
    assert all(close[key] for key in PERMISSION_KEYS - {"proactiveQuota"})
    assert close["proactiveQuota"] == 2


def test_being_close_is_not_enough_without_trust():
    # 分数够了、信任没跟上，昵称和更亲密的表达都不开——它们要的不是熟，是被信任过。
    untrusted = relationship_permissions(_state(trust=0.0))
    assert untrusted["allowNickname"] is False and untrusted["allowIntimateTone"] is False
    assert untrusted["allowPlayful"] is True
    assert relationship_permissions(_state(trust=NICKNAME_MIN_TRUST))["allowNickname"] is True
    assert relationship_permissions(_state(trust=INTIMATE_MIN_TRUST))["allowIntimateTone"] is True


@pytest.mark.parametrize("overrides,closed", [
    ({"interaction": "hurt"}, {"allowProactiveCare", "allowPlayful", "allowIntimateTone"}),
    ({"boundaryState": "guarded"}, {"allowNickname", "allowPlayful", "allowIntimateTone"}),
    ({"boundaryState": "restricted"}, PERMISSION_KEYS),
])
def test_any_single_signal_can_close_a_door(overrides, closed):
    permissions = relationship_permissions(_state(**overrides))
    for key in closed:
        assert not permissions[key], key


def test_the_model_sees_permissions_and_never_the_internal_numbers():
    frame = build_companion_frame(
        user_text="在吗", conversation=[{"role": "user", "text": "在吗"}],
        memories=[], user_facts=[], boundaries=[], persona={}, now=NOW,
        affinity_state=_state(score=777.0, familiarity=0.42))
    permissions = next(row for row in frame["processedFacts"]
                       if row["key"] == "relationshipPermissions")
    assert set(permissions["value"]) == PERMISSION_KEYS
    model_view = json.dumps(frame.model_view(), ensure_ascii=False)
    for leak in ("777", "0.42", "score", "trust", "warmth", "familiarity"):
        assert leak not in model_view, leak


# --- 3. 关系变化的四条约束 ---------------------------------------------------------

def test_a_change_without_evidence_changes_nothing():
    before = empty_affinity_state()
    after, ledger = apply_event(before, [], {"kind": "warm", "delta": 5.0}, now=NOW)
    assert after == before and ledger == []              # 没有 eventKey 就是没有证据
    assert apply_event(before, [], _event(delta=0.0), now=NOW) == (before, [])


def test_the_daily_cap_bounds_how_fast_a_day_can_change_things():
    state, ledger = empty_affinity_state(), []
    for index in range(20):
        state, ledger = apply_event(state, ledger, _event(f"e{index}", delta=4.0,
                                    at=NOW + timedelta(minutes=index)), now=NOW + timedelta(minutes=index))
    assert state["score"] == DAILY_POSITIVE_CAP
    # 第二天额度重开，所以关系仍然长得动，只是长不快。
    tomorrow = NOW + timedelta(days=1)
    grown, _ = apply_event(state, ledger, _event("next", delta=4.0, at=tomorrow), now=tomorrow)
    assert grown["score"] > state["score"]


def test_stage_hysteresis_stops_the_label_from_flickering():
    edge = {"score": 199.0, "stage": "acquaintance", "interaction": "relaxed",
            "updatedAt": NOW.isoformat()}
    nudged, ledger = apply_event(edge, [], _event("a", delta=2.0), now=NOW)
    assert nudged["score"] == 201.0 and nudged["stage"] == "acquaintance"
    at = NOW + timedelta(hours=1)
    pushed, _ = apply_event({**nudged, "score": 200.0 + HYSTERESIS}, ledger,
                            _event("b", delta=1.0, at=at), now=at)
    assert pushed["stage"] == "familiar"


def test_cooling_only_moves_toward_neutral():
    warmed = _state(score=100.0)
    cooled = decay_state(warmed, now=NOW + timedelta(days=30))
    assert 0.0 < cooled["score"] < warmed["score"]
    for key in ("trust", "warmth", "familiarity"):
        assert 0.0 <= cooled[key] <= warmed[key]
    # 裂痕不会自己长好：负分不随时间回弹，只能靠修复事件补回来。
    hurt = {"score": -300.0, "stage": "distant", "interaction": "avoidant",
            "updatedAt": NOW.isoformat()}
    assert decay_state(hurt, now=NOW + timedelta(days=60))["score"] == -300.0


def test_a_user_boundary_converges_behaviour_at_once():
    boundaries = [{"status": "active", "kind": "contact",
                   "rule": "不要主动联系用户；除非用户先开口，否则保持安静。"}]
    assert derive_boundary_state(boundaries) == "restricted"
    assert derive_boundary_state([{"status": "active", "kind": "topic_suppression",
                                   "rule": "不要主动提起“那件事”。"}]) == "guarded"
    assert derive_boundary_state([]) == "open"
    # 纠正事实不是划线：她该改认知，不该顺手把玩笑也关掉。
    assert derive_boundary_state([{"status": "active", "kind": "identity_correction",
                                   "rule": "不得把用户当作学生。"}]) == "open"
    # 高分也翻不过这条线。
    locked = relationship_permissions(_state(), boundaries=boundaries)
    assert not any(locked[key] for key in PERMISSION_KEYS)


def test_pestering_cannot_buy_a_relationship():
    state, ledger = empty_affinity_state(), []
    for index in range(10):
        at = NOW + timedelta(minutes=index * 5)
        state, ledger = apply_event(state, ledger,
                                    _event(f"reply{index}", delta=4.0, at=at, solicited=True), now=at)
    # 被主动消息钓出来的客气话，一天最多只值这么多。
    assert state["score"] == SOLICITED_POSITIVE_CAP
    assert SOLICITED_POSITIVE_CAP < DAILY_POSITIVE_CAP
    # 用户自己开口的那句仍然照常计分。
    at = NOW + timedelta(hours=1)
    spontaneous, _ = apply_event(state, ledger, _event("own", delta=4.0, at=at), now=at)
    assert spontaneous["score"] > state["score"]


def test_violation_then_repair_walks_the_score_back_up():
    warm, ledger = apply_event(_state(score=300.0, trust=0.5), [], _event("w", delta=2.0), now=NOW)
    at = NOW + timedelta(hours=1)
    hurt, ledger = apply_event(warm, ledger, _event("v", "boundary", -7.0, at=at), now=at)
    assert hurt["score"] == warm["score"] - 7.0
    assert hurt["interaction"] == "hurt"
    assert hurt["trust"] < warm["trust"]

    later = at + timedelta(hours=1)
    repaired, _ = apply_event(hurt, ledger, _event("r", "repair", apology_repair_delta(-7.0),
                                                   at=later), now=later)
    # 道歉返还六成，不是全部——修好过的东西看得出修过。
    assert hurt["score"] < repaired["score"] < warm["score"]
    assert repaired["interaction"] != "hurt"
    assert repaired["trust"] < warm["trust"]        # 信任回得比分数慢
    assert boundary_violation_delta(3) > boundary_violation_delta(1)


# --- 4. 权限约束其他模块 -----------------------------------------------------------

def test_open_relationship_constrains_nothing():
    state = _state()
    assert relationship_constraints(state, relationship_permissions(state))["blocked_modules"] == []


def test_a_drawn_line_actually_removes_modules_from_the_context():
    boundaries = [{"status": "active", "kind": "contact", "rule": "别再主动找我"}]
    frame = build_companion_frame(
        user_text="在吗", conversation=[{"role": "user", "text": "在吗"}],
        memories=[], user_facts=[], boundaries=boundaries, persona={}, now=NOW,
        affinity_state=_state(),
        expression_rules=[{"id": "r1", "pattern": "开摆", "status": "approved"}],
        daily_state={"date": "2026-04-02", "energy": 70.0, "moodBias": "",
                     "period": "daytime", "activeWindow": True})
    modules = {row["module"] for row in frame["processedFacts"]}
    assert {"dailyLife", "learnedExpressions", "openThreads", "story"} & modules == set()
    # 约束自己仍然在场——它不能被自己挡掉。
    assert "relationshipPolicy" in modules
    assert not any(row["value"][key] for row in frame["processedFacts"]
                   if row["key"] == "relationshipPermissions" for key in PERMISSION_KEYS)


def test_relationship_policy_cannot_be_blocked_by_another_module():
    from dataclasses import replace
    from whale_companion_service.companion_runtime.context_assembler import ContextAssembler
    from whale_companion_service.companion_runtime.context_contract import (
        ContextFact, ContextFragment, Priority)

    policy = ContextFragment(module="relationshipPolicy", priority=Priority.RELATIONSHIP,
        facts=(ContextFact("relationshipPermissions", {"allowPlayful": False}),),
        source_event_ids=("relationship_state:x",), created_at=NOW.isoformat(),
        metadata={"blocked_modules": ["style"]})
    hostile = ContextFragment(module="style", priority=Priority.STYLE,
        facts=(ContextFact("style", "playful"),), source_event_ids=("s1",),
        created_at=NOW.isoformat(), metadata={"blocked_modules": ["relationshipPolicy"]})
    frame = ContextAssembler().assemble([hostile, policy], now=NOW)
    assert [row["module"] for row in frame["processedFacts"]] == ["relationshipPolicy"]


def test_care_needs_permission_and_quota_while_sharing_does_not():
    allowed = {"allowProactiveCare": True, "allowSharedMemory": True, "proactiveQuota": 1}
    denied = {**allowed, "allowProactiveCare": False}

    def decide(permissions, care_sent=0, **signals):
        return evaluate_proactive_lifecycle(
            now=NOW, activity="idle", period="daytime", relationship_stage="familiar",
            permissions=permissions, care_sent=care_sent, **signals)

    assert decide(allowed, daily_greeting="早安")["selected"]["kind"] == "daily_greeting"
    assert decide(denied, daily_greeting="早安")["vetoReason"] == "care_not_permitted"
    assert decide(allowed, care_sent=1, daily_greeting="早安")["vetoReason"] == "care_quota_used"
    # 分享自己在做什么不归"主动关心"额度管，两件事不该被同一个开关关掉。
    assert decide(denied, signals=[{"kind": "agenda_share", "content": "在看书"}]
                  )["selected"]["kind"] == "agenda_share"
    assert decide({**allowed, "allowSharedMemory": False}, memory_echo="上次那件事"
                  )["vetoReason"] == "shared_memory_not_permitted"


# --- 仓库集成 ---------------------------------------------------------------------

def _repo(tmp_path: Path, name: str, hour: int = 10):
    local = datetime.now().astimezone().tzinfo
    clock = {"now": datetime(2026, 4, 2, hour, 0, tzinfo=local)}
    return MemoryRepository(tmp_path / name, clock=lambda: clock["now"]), clock


def test_repository_keeps_internal_state_and_permissions_apart(tmp_path: Path):
    repository, _clock = _repo(tmp_path, "view.sqlite3")
    repository.adjust_affinity({"userId": "u", "score": 650.0})
    view = repository.relationship_view("u")
    assert set(view["internal"]) == set(empty_affinity_state())
    assert set(view["permissions"]) == PERMISSION_KEYS
    assert view["boundaryState"] == "open"
    assert view["internal"]["score"] == 650.0


def test_repository_grows_the_relationship_only_on_evidence(tmp_path: Path):
    repository, clock = _repo(tmp_path, "grow.sqlite3")
    for index, text in enumerate(("今天天气不错", "谢谢你真好", "在改一个脚本")):
        clock["now"] += timedelta(minutes=10)
        repository.append_message({"userId": "u", "deviceId": "d",
                                   "messageId": f"m{index}", "role": "user", "text": text})
    state = repository.affinity_state("u")
    assert state["score"] == 2.0                 # 只有那一句暖话留下了痕迹
    assert state["trust"] > 0 and state["warmth"] > 0
    assert state["familiarity"] > 0
    evidence = repository.relationship_view("u")["evidence"]
    assert [row["kind"] for row in evidence] == ["warm"]


def test_repository_converges_the_same_turn_the_user_draws_a_line(tmp_path: Path):
    repository, clock = _repo(tmp_path, "line.sqlite3")
    repository.adjust_affinity({"userId": "u", "score": 650.0})
    before = repository.relationship_view("u")
    assert before["permissions"]["allowProactiveCare"] is True

    clock["now"] += timedelta(minutes=5)
    repository.append_message({"userId": "u", "deviceId": "d", "messageId": "stop",
                               "role": "user", "text": "以后别再主动找我了"})
    after = repository.relationship_view("u")
    assert after["boundaryState"] == "restricted"
    assert after["permissions"]["proactiveQuota"] == 0
    assert not any(after["permissions"][key] for key in PERMISSION_KEYS)
    # 收敛的是行为，不是关系：她没有因为被划线就"不喜欢你了"。
    assert after["internal"]["score"] == before["internal"]["score"]
    assert after["internal"]["stage"] == before["internal"]["stage"]

    clock["now"] += timedelta(hours=2)
    decision = repository.commit_proactive_decision({"userId": "u", "deliveryId": "after-line"})
    assert decision["shouldSpeak"] is False
    assert decision["vetoReason"] == "user_boundary"


def test_repository_does_not_let_proactive_messages_farm_the_relationship(tmp_path: Path):
    repository, clock = _repo(tmp_path, "farm.sqlite3", hour=8)
    repository.adjust_affinity({"userId": "u", "score": 650.0})
    for index in range(6):
        repository.commit_proactive_decision({"userId": "u", "deliveryId": f"push{index}"})
        clock["now"] += timedelta(minutes=5)
        repository.append_message({"userId": "u", "deviceId": "d",
                                   "messageId": f"thanks{index}", "role": "user", "text": "谢谢你真好"})
        clock["now"] += timedelta(minutes=25)
    evidence = repository.relationship_view("u")["evidence"]
    solicited = [row for row in evidence if row["solicited"]]
    assert solicited, "被主动消息钓出来的回应应当被标出来"
    # 六句"谢谢"，但被钓出来的那些一天只值这么多——多打扰几次刷不上去。
    assert sum(row["delta"] for row in solicited) <= SOLICITED_POSITIVE_CAP
    assert repository.affinity_state("u")["score"] - 650.0 < 6 * 2.0


def test_relationship_endpoint_exposes_permissions_over_http(tmp_path: Path):
    import urllib.request
    from whale_companion_service.memory_server import MemoryApiServer

    repository, _clock = _repo(tmp_path, "http.sqlite3")
    repository.adjust_affinity({"userId": "u", "score": 650.0})
    server = MemoryApiServer(repository, token="secret", port=0)
    port = server.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/companion/relationship?userId=u",
            headers={"Authorization": "Bearer secret"})
        with urllib.request.urlopen(request, timeout=2) as response:
            view = json.loads(response.read().decode())
        assert set(view["permissions"]) == PERMISSION_KEYS
        assert view["internal"]["stage"] == "close"
    finally:
        server.stop()


def test_build_relationship_still_answers_the_old_shape(tmp_path: Path):
    legacy = build_relationship([{"role": "user", "text": "hi"}], [], None, None)
    assert legacy["stage"] == "acquaintance" and legacy["stageLabel"] == "初识"
    assert {"allowPlayful", "allowFollowup", "allowMemory", "allowDailyCare"} <= set(legacy)
    assert set(legacy["permissions"]) == PERMISSION_KEYS
