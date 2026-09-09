# -*- coding: utf-8 -*-
"""记忆模块第二阶段：分类型衰退、连续激活度、强化与纠正、进上下文前的净化、日/周摘要。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from whale_companion_service.companion_runtime import build_companion_frame
from whale_companion_service.companion_runtime.context_adapters import (
    memory_fragments,
    validated_memory_value,
)
from whale_companion_service.memory_activation import (
    BOUNDARY,
    HALF_LIFE_HOURS,
    RELATIONSHIP,
    SHARED_EXPERIENCE,
    SPEAKABLE_ACTIVATION,
    TRANSIENT_CONTEXT,
    USER_EMOTION,
    USER_FACT,
    classify_memory,
    memory_activation,
)
from whale_companion_service.memory_digest import (
    DIGEST_STATUS_PENDING,
    build_memory_digest,
    digest_periods,
)
from whale_companion_service.memory_lifecycle import (
    decorate_memory_lifecycle,
    lifecycle_transition_from_text,
    memory_is_speakable,
)
from whale_companion_service.memory_server import MemoryRepository

NOW = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)


def _at(**delta) -> str:
    return (NOW - timedelta(**delta)).isoformat()


def _observation(memory_id="obs-1", *, ended=None, **overrides) -> dict:
    """一段桌面观察；`ended` 同时决定开始与结束，避免造出结束在开始之前的假样本。"""
    ended_at = ended or _at(hours=1)
    started = datetime.fromisoformat(ended_at) - timedelta(hours=1)
    row = {"id": memory_id, "revision": 1, "layer": "L1", "sourceType": "observed",
           "kind": "activity", "app": "Visual Studio Code", "context": "work",
           "durationSeconds": 3600, "startedAt": started.isoformat(), "endedAt": ended_at}
    row.update(overrides)
    return row


def _emotion(memory_id="emo-1", **overrides) -> dict:
    row = {"id": memory_id, "revision": 1, "layer": "L3", "sourceType": "user_stated",
           "kind": "stated_emotion", "label": "frustrated", "quote": "我今天好烦",
           "occurredAt": _at(hours=1)}
    row.update(overrides)
    return row


def _batch(memories, batch_id="batch-1", user_id="user-1") -> dict:
    return {"protocolVersion": 1, "userId": user_id, "deviceId": "desktop-1",
            "batchId": batch_id, "latestRevision": max(m["revision"] for m in memories),
            "memories": memories}


# --- 1. 不同类型采用不同衰退策略 -------------------------------------------------

def test_each_memory_type_keeps_its_own_decay_speed():
    age = {"days": 2}
    readings = {
        TRANSIENT_CONTEXT: memory_activation(_observation(ended=_at(**age)), now=NOW),
        USER_EMOTION: memory_activation(_emotion(occurredAt=_at(**age)), now=NOW),
        SHARED_EXPERIENCE: memory_activation(
            _emotion(occurredAt=_at(**age)), {"memoryClass": SHARED_EXPERIENCE}, now=NOW),
        RELATIONSHIP: memory_activation(
            _emotion(occurredAt=_at(**age)), {"memoryClass": RELATIONSHIP}, now=NOW),
    }
    retentions = {name: reading["retention"] for name, reading in readings.items()}
    assert (retentions[TRANSIENT_CONTEXT] < retentions[USER_EMOTION]
            < retentions[SHARED_EXPERIENCE] < retentions[RELATIONSHIP])
    # 同样是"前天的事"：桌面上的一段专注该淡掉，用户亲口说的难过不该跟着一起淡掉。
    assert readings[TRANSIENT_CONTEXT]["speakable"] is False
    assert readings[USER_EMOTION]["speakable"] is True


def test_user_facts_and_boundaries_do_not_decay_with_ordinary_time():
    ancient = {"createdAt": _at(days=900)}
    fact = memory_activation({"id": "f", "predicate": "name", "value": "小羊", **ancient}, now=NOW)
    rule = memory_activation({"id": "b", "rule": "不要叫我学生", **ancient}, now=NOW)
    assert fact["memoryClass"] == USER_FACT and rule["memoryClass"] == BOUNDARY
    assert fact["retention"] == rule["retention"] == 1.0
    assert HALF_LIFE_HOURS[USER_FACT] is HALF_LIFE_HOURS[BOUNDARY] is None
    assert fact["speakable"] and rule["speakable"]


@pytest.mark.parametrize("memory,expected", [
    (_observation(), TRANSIENT_CONTEXT),
    ({"id": "c", "layer": "L2", "kind": "long_focus", "statement": "停留很久"}, TRANSIENT_CONTEXT),
    (_emotion(), USER_EMOTION),
    ({"id": "i", "layer": "L2", "kind": "confirmed_insight", "statement": "常碰 DSH"}, RELATIONSHIP),
    ({"id": "s", "layer": "L3", "kind": "milestone", "label": "happy"}, SHARED_EXPERIENCE),
])
def test_classification_follows_kind_then_layer(memory, expected):
    assert classify_memory(memory) == expected


def test_server_side_class_overrides_what_the_payload_claims_about_itself():
    # 只有服务端知道这条已经被两个人反复聊过；它说了算。
    assert classify_memory(_emotion(memoryClass="transient_context"),
                           {"memoryClass": SHARED_EXPERIENCE}) == SHARED_EXPERIENCE
    assert classify_memory(_observation(memoryClass="nonsense")) == TRANSIENT_CONTEXT


# --- 2. 连续 activation 取代固定过期 ---------------------------------------------

def test_activation_falls_continuously_instead_of_expiring_at_a_cliff():
    curve = [memory_activation(_emotion(occurredAt=_at(hours=hour)), now=NOW)["activation"]
             for hour in range(0, 145, 4)]
    assert curve == sorted(curve, reverse=True)
    # 相邻采样之间没有断崖，说明退场是渐变而不是到点作废。
    assert max(a - b for a, b in zip(curve, curve[1:])) < 0.05
    assert curve[0] > SPEAKABLE_ACTIVATION > curve[-1]


@pytest.mark.parametrize("stronger,weaker", [
    (dict(relevance=0.9), dict(relevance=0.0)),
    (dict(mention_count=4), dict(mention_count=0)),
])
def test_external_signals_raise_activation(stronger, weaker):
    memory = _emotion(occurredAt=_at(days=2))
    assert (memory_activation(memory, now=NOW, **stronger)["activation"]
            > memory_activation(memory, now=NOW, **weaker)["activation"])


def test_intrinsic_signals_raise_activation():
    aged = {"occurredAt": _at(days=2)}
    base = memory_activation(_emotion(label="tired", **aged), now=NOW)
    assert memory_activation(_emotion(label="angry", **aged), now=NOW)["activation"] > base["activation"]
    assert memory_activation(_emotion(importance=1.0, **aged), now=NOW)["activation"] > base["activation"]
    clue = {"id": "c", "layer": "L2", "kind": "long_focus", "statement": "x", "createdAt": _at(hours=6)}
    assert (memory_activation({**clue, "confidence": 0.9}, now=NOW)["activation"]
            > memory_activation(clue, now=NOW)["activation"])
    # 未完成的事比已经收尾的事更容易被想起来。
    assert (memory_activation(_emotion(**aged), now=NOW, status="active")["activation"]
            > memory_activation(_emotion(**aged), now=NOW, status="resolved")["activation"])


def test_activation_reading_is_auditable_and_reproducible():
    reading = memory_activation(_emotion(), {"mentionCount": 2}, now=NOW, relevance=0.4)
    assert reading == memory_activation(_emotion(), {"mentionCount": 2}, now=NOW, relevance=0.4)
    assert set(reading["components"]) == {
        "importance", "emotionalIntensity", "sourceConfidence",
        "topicRelevance", "reinforcement", "unresolved",
    }
    assert reading["threshold"] == SPEAKABLE_ACTIVATION
    assert json.loads(json.dumps(reading, allow_nan=False)) == reading


def test_lifecycle_view_reports_the_activation_that_faded_the_memory():
    faded = decorate_memory_lifecycle(_observation(ended=_at(days=3)), now=NOW)
    assert faded["lifecycle"]["status"] == "dormant"
    assert faded["lifecycle"]["freshness"] == "stale"
    assert faded["lifecycle"]["activation"] < SPEAKABLE_ACTIVATION
    assert faded["lifecycle"]["memoryClass"] == TRANSIENT_CONTEXT
    assert faded["lifecycle"]["halfLifeHours"] == HALF_LIFE_HOURS[TRANSIENT_CONTEXT]
    # 原始记忆不被改写。
    assert "lifecycle" not in _observation()


# --- 3. 强化 / 纠正 / 解决 / 暂停 / 禁止再提 --------------------------------------

def test_reinforcement_slows_decay_and_can_bring_a_faded_memory_back():
    memory = _emotion(occurredAt=_at(days=4))
    quiet = memory_activation(memory, now=NOW)
    assert quiet["speakable"] is False
    revisited = memory_activation(memory, {"mentionCount": 3}, now=NOW)
    assert revisited["halfLifeHours"] > quiet["halfLifeHours"]
    assert revisited["speakable"] is True
    # 再次提及同时把"上一次被碰到"推到现在。
    touched = memory_activation(memory, {"mentionCount": 1, "lastMentionedAt": NOW.isoformat()}, now=NOW)
    assert touched["ageHours"] == 0.0


@pytest.mark.parametrize("text,expected", [
    ("你记错了，我说的是周五", ("corrected", "user_corrected_the_memory")),
    ("我没说过这件事已经解决了", ("corrected", "user_corrected_the_memory")),
    ("今天已经好多了", ("resolved", "user_said_it_improved_or_ended")),
    ("算了，今天先不聊", ("dormant", "user_paused_the_topic")),
    ("以后别再提这件事", ("suppressed", "user_asked_not_to_revisit")),
    ("这件事还没解决", ("active", "user_said_it_is_still_ongoing")),
])
def test_user_intent_transitions_stay_distinct(text, expected):
    assert lifecycle_transition_from_text(text) == expected


@pytest.mark.parametrize("status", ["resolved", "dormant", "suppressed", "corrected"])
def test_user_intent_beats_a_high_activation_memory(status):
    fresh = _emotion(occurredAt=NOW.isoformat())
    assert memory_activation(fresh, now=NOW)["speakable"] is True
    closed = decorate_memory_lifecycle(fresh, {"status": status, "updatedAt": NOW.isoformat()}, now=NOW)
    assert closed["lifecycle"]["status"] == status
    assert memory_is_speakable(closed) is False


# --- 4. 只有净化过的 ContextFragment 进模型 ---------------------------------------

def test_raw_payload_and_forged_provenance_never_become_context_facts():
    forged = _observation("forged", sourceType="user_stated")
    assert validated_memory_value(forged) == (None, "")
    assert validated_memory_value(_observation("no-app", app=""))[0] is None
    assert validated_memory_value(_observation("bad-duration", durationSeconds="很久"))[0] is None
    value, source = validated_memory_value(_observation(
        secretField="SECRET", activityLogs=["RAW"], project={"name": "小鲸", "summary": "RAW"}))
    assert value == {"app": "Visual Studio Code", "context": "work", "durationSeconds": 3600,
                     "projectName": "小鲸", "layer": "L1"}
    assert source == "observed"


def test_duplicate_memories_collapse_into_one_fact_that_keeps_every_source():
    first = _observation("dup-a", durationSeconds=1800)
    second = _observation("dup-b", durationSeconds=900, revision=2)
    fragments = memory_fragments([first, second], "在忙什么", NOW)
    assert len(fragments) == 1
    fact = fragments[0].facts[0]
    # 两条同一件事合成一条，时长求和而不是只报其中一条。
    assert fact.value["durationSeconds"] == 2700
    assert fragments[0].source_event_ids == ("dup-a", "dup-b")


def test_faded_and_forged_memories_do_not_reach_the_frame():
    frame = build_companion_frame(
        user_text="今天怎么样", conversation=[{"role": "user", "text": "今天怎么样"}],
        memories=[
            decorate_memory_lifecycle(_observation("faded", ended=_at(days=4)), now=NOW),
            _observation("forged", app="FORGED_APP", sourceType="user_stated"),
            _emotion("live", quote="LIVE_QUOTE"),
        ],
        user_facts=[], boundaries=[], persona={}, now=NOW)
    keys = {row["key"] for row in frame["processedFacts"]}
    assert "memory:live" in keys
    assert "memory:faded" not in keys and "memory:forged" not in keys
    assert "FORGED_APP" not in json.dumps(frame, ensure_ascii=False)


def test_more_activated_memories_win_the_memory_budget_first():
    faint = _observation("faint", app="Chrome", context="idle", durationSeconds=60, ended=_at(hours=18))
    strong = _emotion("strong", quote="我很烦这个汇报")
    ordered = sorted(memory_fragments([faint, strong], "汇报", NOW), key=lambda f: -f.priority)
    assert [f.facts[0].key for f in ordered] == ["memory:strong", "memory:faint"]


# --- 5. 日摘要与周摘要的数据结构 --------------------------------------------------

def test_digests_are_deterministic_skeletons_without_generated_prose():
    memories = [_observation("obs", ended=_at(hours=3)), _emotion("emo", occurredAt=_at(hours=2))]
    daily = build_memory_digest("daily", memories, period_start=NOW.date().isoformat(), now=NOW)
    assert daily == build_memory_digest("daily", memories, period_start=NOW.date().isoformat(), now=NOW)
    assert daily["digestId"] == f"digest:daily:{NOW.date().isoformat()}"
    assert daily["periodStart"] == daily["periodEnd"] == NOW.date().isoformat()
    assert daily["memoryIds"] == ["emo", "obs"]
    assert daily["classCounts"] == {TRANSIENT_CONTEXT: 1, USER_EMOTION: 1}
    assert daily["emotionLabels"] == {"frustrated": 1}
    assert daily["openLoopMemoryIds"] == ["emo"]
    assert [row["memoryId"] for row in daily["highlights"]] == ["emo", "obs"]
    # 本阶段不生成摘要正文，也不调用任何模型。
    assert daily["summary"] == "" and daily["summarySource"] == ""
    assert daily["status"] == DIGEST_STATUS_PENDING


def test_weekly_digest_spans_seven_days_from_monday():
    inside = _observation("inside", ended=_at(days=2))
    outside = _observation("outside", ended=_at(days=20))
    week = digest_periods([inside])[1]
    assert week[0] == "weekly"
    weekly = build_memory_digest("weekly", [inside, outside], period_start=week[1], now=NOW)
    assert datetime.fromisoformat(weekly["periodStart"]).weekday() == 0
    assert weekly["memoryIds"] == ["inside"]
    assert weekly["isoWeek"].startswith("2026-W")
    with pytest.raises(ValueError):
        build_memory_digest("monthly", [], period_start=week[1], now=NOW)


# --- 6. 仓库集成 -----------------------------------------------------------------

def test_repository_persists_digests_without_calling_the_model(tmp_path: Path):
    class Responder:
        name, available, calls = "test", True, 0
        def reply(self, *args):
            Responder.calls += 1
            return "不应调用"
    repository = MemoryRepository(tmp_path / "digest.sqlite3", responder=Responder())
    result = repository.ingest_batch(_batch([_observation(ended=_at(hours=1))]))
    assert result["digestIds"] == sorted(result["digestIds"])
    digests = repository.memory_digests("user-1")["digests"]
    assert {row["kind"] for row in digests} == {"daily", "weekly"}
    assert all(row["summary"] == "" for row in digests)
    assert repository.memory_digests("user-1", "weekly")["digests"][0]["kind"] == "weekly"
    assert Responder.calls == 0
    # 重复摄入同一批不产生第二份摘要。
    repository.ingest_batch(_batch([_observation(ended=_at(hours=1))]))
    assert len(repository.memory_digests("user-1")["digests"]) == len(digests)


def test_repository_reinforcement_survives_a_memory_that_had_already_faded(tmp_path: Path):
    repository = MemoryRepository(tmp_path / "reinforce.sqlite3")
    repository.ingest_batch(_batch([_emotion(occurredAt=(
        datetime.now(timezone.utc) - timedelta(days=4)).isoformat())]))
    before = repository.stream("user-1")["memories"][0]["lifecycle"]
    assert before["status"] == "dormant" and before["reason"] == "aged_out"

    after = repository.reinforce_memory({"userId": "user-1", "memoryId": "emo-1"})["lifecycle"]
    assert after["status"] == "active"
    assert after["mentionCount"] == 1 and after["ageHours"] == 0.0
    assert after["activation"] > before["activation"]

    # 强化不能翻越用户边界。
    repository.update_lifecycle({"userId": "user-1", "memoryId": "emo-1",
                                 "status": "suppressed", "reason": "user_asked_not_to_revisit"})
    revived = repository.reinforce_memory({"userId": "user-1", "memoryId": "emo-1"})["lifecycle"]
    assert revived["status"] == "suppressed"


def test_user_correction_retires_the_memory_and_keeps_the_original_row(tmp_path: Path):
    repository = MemoryRepository(tmp_path / "corrected.sqlite3")
    repository.ingest_batch(_batch([_emotion(occurredAt=datetime.now(timezone.utc).isoformat())]))
    assert repository.claim_opening({
        "userId": "user-1", "deviceId": "phone-1", "claimId": "c1"})["focusMemoryId"] == "emo-1"

    result = repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "fix-1",
        "role": "user", "text": "你记错了，我说的是周五"})
    assert result["lifecycleUpdates"][0]["status"] == "corrected"
    assert result["lifecycleUpdates"][0]["reason"] == "user_corrected_the_memory"
    assert "记错" in result["assistantMessage"]["text"]

    stored = repository.stream("user-1")["memories"][0]
    assert stored["quote"] == "我今天好烦"          # 原始事件不可变
    assert stored["lifecycle"]["status"] == "corrected"
    assert memory_is_speakable(stored) is False


def test_a_revisited_emotion_becomes_a_shared_experience_and_is_kept_far_longer(tmp_path: Path):
    repository = MemoryRepository(tmp_path / "shared.sqlite3")
    repository.append_message({"userId": "user-1", "deviceId": "phone-1", "messageId": "a1",
                               "role": "user", "text": "我很焦虑，明天的汇报还没准备好"})
    first = repository.stream("user-1")["memories"][0]["lifecycle"]
    assert first["memoryClass"] == USER_EMOTION

    repository.append_message({"userId": "user-1", "deviceId": "phone-1", "messageId": "a2",
                               "role": "user", "text": "我还是很焦虑，这件事还在继续"})
    promoted = repository.stream("user-1")["memories"][0]["lifecycle"]
    # 被两个人反复聊过的事不再只是那天的情绪。
    assert promoted["memoryClass"] == SHARED_EXPERIENCE
    assert promoted["halfLifeHours"] > first["halfLifeHours"]
    assert promoted["mentionCount"] >= 1


def test_user_saying_it_is_still_going_on_puts_a_faded_memory_back_in_context(tmp_path: Path):
    repository = MemoryRepository(tmp_path / "still-open.sqlite3")
    stale = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    repository.ingest_batch(_batch([_emotion(occurredAt=stale)]))
    repository.claim_opening({"userId": "user-1", "deviceId": "phone-1", "claimId": "c1"})
    assert repository.stream("user-1")["memories"][0]["lifecycle"]["status"] == "dormant"

    repository.append_message({"userId": "user-1", "deviceId": "phone-1", "messageId": "still-1",
                               "role": "user", "text": "那件事还没解决"})
    lifecycle = repository.stream("user-1")["memories"][0]["lifecycle"]
    assert lifecycle["status"] == "dormant"  # 没有对话焦点时不改状态
    repository.update_lifecycle({"userId": "user-1", "memoryId": "emo-1", "status": "active",
                                 "reason": "user_said_it_is_still_ongoing"})
    revived = repository.stream("user-1")["memories"][0]["lifecycle"]
    assert revived["status"] == "active" and revived["freshness"] == "fresh"
    assert memory_is_speakable(revived) is True


def test_reinforce_and_digest_endpoints_are_reachable_over_http(tmp_path: Path):
    from whale_companion_service.memory_protocol import MemorySyncClient
    from whale_companion_service.memory_server import MemoryApiServer

    server = MemoryApiServer(MemoryRepository(tmp_path / "http.sqlite3"), token="secret", port=0)
    port = server.start()
    try:
        client = MemorySyncClient(f"http://127.0.0.1:{port}", "secret")
        client.post_batch(_batch([_emotion(occurredAt=(
            datetime.now(timezone.utc) - timedelta(days=4)).isoformat())]))
        digests = client.memory_digests("user-1")["digests"]
        assert {row["kind"] for row in digests} == {"daily", "weekly"}
        assert client.memory_digests("user-1", "daily")["digests"][0]["summary"] == ""

        assert client.stream("user-1")["memories"][0]["lifecycle"]["status"] == "dormant"
        reinforced = client.reinforce_memory(user_id="user-1", memory_id="emo-1")
        assert reinforced["lifecycle"]["status"] == "active"
        assert client.stream("user-1")["memories"][0]["lifecycle"]["mentionCount"] == 1
        snapshot_digests = MemoryRepository(tmp_path / "http.sqlite3").debug_snapshot("user-1")
        assert snapshot_digests["memoryDigests"] == digests
    finally:
        server.stop()
