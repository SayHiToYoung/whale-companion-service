from __future__ import annotations

import urllib.request
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from whale_companion_service.dialogue_state import emotional_dialogue_state
from whale_companion_service.companion_mind import build_companion_mind
from whale_companion_service.companion_runtime import build_companion_frame
from whale_companion_service.companion_runtime.open_threads import build_open_threads
from whale_companion_service.persona_card import DEFAULT_PERSONA_CARD
from whale_companion_service.standing_knowledge import (
    derive_standing_knowledge,
    standing_knowledge_context,
)
from whale_companion_service.provider import ProviderConfig
from whale_companion_service.companion_llm import (
    PROMPT_VERSION,
    ModelCompanionResponder,
    build_model_context,
    model_reply_is_grounded,
    reply_has_style_violation,
)
from whale_companion_service.memory_protocol import (
    MemorySyncClient,
    ProtocolError,
    SyncTransportError,
    build_big_whale_opening,
    build_grounded_companion_reply,
    make_batch,
)
from whale_companion_service.memory_lifecycle import (
    decorate_memory_lifecycle,
    lifecycle_transition_from_text,
    memory_is_speakable,
)
from whale_companion_service.memory_server import (
    MemoryApiServer,
    MemoryConflictError,
    MemoryRepository,
)
from whale_companion_service.memory_retrieval import select_companion_memories
from whale_companion_service.insight_drafts import build_insight_drafts


TEST_NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _fact(memory_id: str = "fact-1", revision: int = 1, **overrides) -> dict:
    ended_at = TEST_NOW
    started_at = ended_at - timedelta(hours=1)
    value = {
        "id": memory_id,
        "revision": revision,
        "layer": "L1",
        "sourceType": "observed",
        "kind": "activity",
        "app": "Visual Studio Code",
        "context": "work",
        "durationSeconds": 3600,
        "startedAt": started_at.isoformat(),
        "endedAt": ended_at.isoformat(),
        "project": {"name": "小鲸"},
    }
    value.update(overrides)
    return value


def _batch(batch_id: str = "batch-1", memory: dict | None = None) -> dict:
    item = memory or _fact()
    return {
        "protocolVersion": 1,
        "userId": "user-1",
        "deviceId": "desktop-1",
        "batchId": batch_id,
        "latestRevision": item["revision"],
        "memories": [item],
    }


def test_protocol_rejects_layer_source_mismatch_and_remote_plain_http() -> None:
    bad = _fact(sourceType="user_stated")
    with pytest.raises(ProtocolError):
        make_batch(
            user_id="user-1",
            device_id="desktop-1",
            report={"batchId": "batch-1", "latestRevision": 1},
            memories=[bad],
        )
    with pytest.raises(ProtocolError):
        MemorySyncClient("http://example.com", "token")


def test_repository_batch_is_idempotent_and_conflicts_on_changed_payload(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "memory.sqlite3")
    first = repository.ingest_batch(_batch())
    duplicate = repository.ingest_batch(_batch())
    assert first["acceptedCount"] == 1
    assert duplicate["duplicate"] is True
    assert repository.stream("user-1")["memories"][0]["id"] == "fact-1"

    changed = _batch()
    changed["memories"][0]["durationSeconds"] = 7200
    with pytest.raises(MemoryConflictError):
        repository.ingest_batch(changed)


def test_debug_snapshot_preview_and_safe_test_cleanup(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "debug.sqlite3")
    repository.ingest_batch(_batch("debug-batch", _fact("debug_fact-1")))

    snapshot = repository.debug_snapshot("user-1")
    assert snapshot["memories"][0]["id"] == "debug_fact-1"
    assert snapshot["memories"][0]["server"]["serverSeq"] == 1
    assert snapshot["persona"]["promptVersion"]
    assert "大鲸" in snapshot["persona"]["systemPrompt"]
    assert snapshot["identity"] == {
        "identityVersion": "echo-identity-v1",
        "petId": "shenshen",
        "productName": "回声",
        "desktopName": "小鲸",
        "mobileName": "大鲸",
        "memoryOwner": "shared-companion-service",
    }
    assert snapshot["dialogueState"] == {"phase": "idle", "turn": 0}

    preview = repository.preview_opening("user-1")
    assert preview["text"]
    assert preview["focusMemoryId"] == "debug_fact-1"
    assert preview["persisted"] is False
    assert repository.messages("user-1")["messages"] == []

    with pytest.raises(ProtocolError):
        repository.delete_debug_memories("user-1", ["fact-real"])
    result = repository.delete_debug_memories("user-1", ["debug_fact-1"])
    assert result["deleted"] == 1
    assert repository.stream("user-1")["memories"] == []


def test_daily_episode_filters_debug_preview_and_phone_opening(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "episodes.sqlite3")
    old = _fact("old-day", 1, localDate="2026-09-04", durationSeconds=7200)
    today = _fact("today-meeting", 2, localDate="2026-09-07", kind="meeting",
                  context="meeting", durationSeconds=10800)
    repository.ingest_batch(_batch("old-batch", old))
    repository.ingest_batch(_batch("today-batch", today))
    repository.ingest_batch(_batch("unlisted-batch", _fact(
        "today-unlisted", 3, localDate="2026-09-07", durationSeconds=20000,
    )))
    manifest = repository.ingest_episode({
        "userId": "user-1", "episodeId": "daily:2026-09-07", "date": "2026-09-07",
        "revision": 2, "memoryIds": ["today-meeting"],
    })
    assert manifest["revision"] == 2

    snapshot = repository.debug_snapshot("user-1", "2026-09-07")
    assert [memory["id"] for memory in snapshot["memories"]] == ["today-meeting", "today-unlisted"]
    assert [episode["date"] for episode in snapshot["episodes"]] == ["2026-09-07", "2026-09-04"]
    assert snapshot["episodes"][0]["revision"] == 2

    preview = repository.preview_opening("user-1", "2026-09-07")
    assert preview["episodeDate"] == "2026-09-07"
    assert preview["focusMemoryId"] == "today-meeting"
    assert preview["memoryCount"] == 1

    opening = repository.claim_opening({
        "userId": "user-1", "deviceId": "phone-episode", "claimId": "episode-claim",
    })
    assert opening["shouldSend"] is True
    assert opening["episodeDate"] == "2026-09-07"
    assert opening["episodeId"] == "daily:2026-09-07"
    assert opening["episodeRevision"] == 2
    assert opening["focusMemoryId"] == "today-meeting"
    assert opening["latestMemory"]["id"] == "today-meeting"
    consumed = repository.debug_snapshot("user-1")["episodes"][0]
    assert consumed["id"] == "daily:2026-09-07"
    assert consumed["status"] == "consumed"
    assert consumed["consumedAt"]


def test_opening_claim_is_recoverable_and_never_repeats_while_waiting(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "memory.sqlite3")
    repository.ingest_batch(_batch())

    first = repository.claim_opening({
        "userId": "user-1", "deviceId": "phone-1", "claimId": "claim-1",
    })
    retry = repository.claim_opening({
        "userId": "user-1", "deviceId": "phone-1", "claimId": "claim-1",
    })
    blocked = repository.claim_opening({
        "userId": "user-1", "deviceId": "phone-1", "claimId": "claim-2",
    })
    assert first["shouldSend"] is True
    assert "小鲸" in first["text"]
    assert retry["messageId"] == first["messageId"]
    assert retry["duplicateClaim"] is True
    assert blocked["shouldSend"] is False
    assert blocked["reason"] == "awaiting_user_reply"
    assert blocked["pendingAssistant"]["messageId"] == first["messageId"]

    repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "reply-1",
        "role": "user", "text": "忙完了",
    })
    waiting = repository.claim_opening({
        "userId": "user-1", "deviceId": "phone-1", "claimId": "claim-3",
    })
    assert waiting["shouldSend"] is False
    assert waiting["reason"] == "awaiting_user_reply"


def test_phone_reply_only_writes_l3_when_user_states_emotion(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "memory.sqlite3")
    neutral = repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "neutral-1",
        "role": "user", "text": "今天开了三个小时的会",
    })
    emotional = repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "emotion-1",
        "role": "user", "text": "今天开了三个小时的会，我好烦啊",
    })
    duplicate = repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "emotion-1",
        "role": "user", "text": "今天开了三个小时的会，我好烦啊",
    })
    assert neutral["emotionRecorded"] is False
    assert emotional["emotionRecorded"] is True
    assert duplicate["emotionMemoryId"] == emotional["emotionMemoryId"]
    memories = repository.stream("user-1")["memories"]
    assert len(memories) == 1
    assert memories[0]["layer"] == "L3"
    assert memories[0]["label"] == "frustrated"


def test_lifecycle_policy_separates_freshness_state_and_explicit_intent() -> None:
    now = datetime.now(timezone.utc)
    clue = {
        "id": "clue-1", "revision": 1, "layer": "L2", "sourceType": "derived",
        "kind": "long_focus", "statement": "在同一步停留了很久",
        "createdAt": now.isoformat(),
    }
    pending = decorate_memory_lifecycle(clue, now=now)
    assert pending["lifecycle"]["status"] == "pending_confirmation"
    assert pending["lifecycle"]["freshness"] == "fresh"
    assert memory_is_speakable(pending) is True

    old_emotion = {
        "id": "emotion-old", "revision": 2, "layer": "L3",
        "sourceType": "user_stated", "kind": "stated_emotion",
        "label": "frustrated", "occurredAt": (now - timedelta(days=4)).isoformat(),
    }
    dormant = decorate_memory_lifecycle(old_emotion, now=now)
    assert dormant["lifecycle"]["storedStatus"] == "active"
    assert dormant["lifecycle"]["status"] == "dormant"
    assert dormant["lifecycle"]["reason"] == "aged_out"
    assert memory_is_speakable(dormant) is False

    reaffirmed = decorate_memory_lifecycle(old_emotion, {
        "status": "active",
        "reason": "user_said_it_is_still_ongoing",
        "sourceType": "user_explicit",
        "updatedAt": now.isoformat(),
    }, now=now)
    assert reaffirmed["lifecycle"]["status"] == "active"
    assert reaffirmed["lifecycle"]["freshness"] == "fresh"

    assert lifecycle_transition_from_text("今天好多了") == (
        "resolved", "user_said_it_improved_or_ended",
    )
    assert lifecycle_transition_from_text("以后别再提这件事") == (
        "suppressed", "user_asked_not_to_revisit",
    )
    assert lifecycle_transition_from_text("算了，今天先不聊") == (
        "dormant", "user_paused_the_topic",
    )
    assert lifecycle_transition_from_text("这件事还没解决") == (
        "active", "user_said_it_is_still_ongoing",
    )


def test_user_can_resolve_focused_emotion_without_rewriting_original_memory(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "lifecycle.sqlite3")
    emotion = {
        "id": "emotion-focus", "revision": 1, "layer": "L3",
        "sourceType": "user_stated", "kind": "stated_emotion",
        "label": "frustrated", "quote": "我今天好烦",
        "occurredAt": datetime.now(timezone.utc).isoformat(),
    }
    repository.ingest_batch(_batch(memory=emotion))
    opening = repository.claim_opening({
        "userId": "user-1", "deviceId": "phone-1", "claimId": "emotion-opening",
    })
    assert opening["shouldSend"] is True
    assert opening["focusMemoryId"] == "emotion-focus"

    result = repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "emotion-better",
        "role": "user", "text": "今天已经好多了",
    })
    assert result["lifecycleUpdates"][0]["memoryId"] == "emotion-focus"
    assert result["lifecycleUpdates"][0]["status"] == "resolved"
    assert "松" in result["assistantMessage"]["text"]
    duplicate = repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "emotion-better",
        "role": "user", "text": "今天已经好多了",
    })
    assert duplicate["duplicate"] is True
    assert duplicate["lifecycleUpdates"] == result["lifecycleUpdates"]

    stored = repository.stream("user-1")["memories"][0]
    assert stored["quote"] == "我今天好烦"
    assert stored["label"] == "frustrated"
    assert stored["lifecycle"]["status"] == "resolved"


def test_emotional_thread_and_memory_mention_form_a_relationship_loop(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "relationship.sqlite3")
    created = repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "feeling-1",
        "role": "user", "text": "我今天有点焦虑，周五还要做汇报",
    })
    memory_id = created["emotionMemoryId"]
    snapshot = repository.debug_snapshot("user-1")
    thread = snapshot["emotionalThreads"][0]
    assert thread["memory_id"] == memory_id
    assert thread["status"] == "acknowledged"
    assert thread["mention_count"] == 1
    assert thread["episode_id"].startswith("daily:")
    assert snapshot["memoryMentions"][0]["mention_type"] == "reply"

    resolved = repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "feeling-better",
        "role": "user", "text": "这个已经解决了，我好多了",
    })
    assert resolved["lifecycleUpdates"][0]["status"] == "resolved"
    final = repository.debug_snapshot("user-1")
    assert final["emotionalThreads"][0]["status"] == "resolved"
    assert final["emotionalThreads"][0]["mention_count"] == 1


def test_explicit_continuity_merges_emotion_and_keeps_need_and_follow_up(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "continuity.sqlite3")
    repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "anxious-1",
        "role": "user", "text": "我很焦虑，明天的汇报还没准备好，不需要建议，陪陪我",
    })
    repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "anxious-2",
        "role": "user", "text": "我还是很焦虑，明天再问我",
    })
    snapshot = repository.debug_snapshot("user-1")
    assert len(snapshot["emotionalThreads"]) == 1
    thread = snapshot["emotionalThreads"][0]
    assert thread["need"] == "listening"
    assert thread["follow_up_hint"] == "明天再问我"
    assert thread["last_user_message_id"] == "anxious-2"
    assert thread["mention_count"] == 2


def test_ambiguous_lifecycle_words_do_not_update_without_conversation_focus(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "no-focus.sqlite3")
    repository.ingest_batch(_batch())
    result = repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "ambiguous-done",
        "role": "user", "text": "这个解决了",
    })
    assert result["lifecycleUpdates"] == []
    assert repository.stream("user-1")["memories"][0]["lifecycle"]["status"] == "active"


def test_suppressed_memory_is_removed_from_next_model_context(tmp_path: Path) -> None:
    responder = _StubResponder("好，以后不主动提。")
    repository = MemoryRepository(tmp_path / "suppressed.sqlite3", responder=responder)
    repository.ingest_batch(_batch())
    opening = repository.claim_opening({
        "userId": "user-1", "deviceId": "phone-1", "claimId": "fact-opening",
    })
    assert opening["shouldSend"] is True

    result = repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "do-not-revisit",
        "role": "user", "text": "以后别再提这件事",
    })
    assert result["lifecycleUpdates"][0]["status"] == "suppressed"
    assert responder.last_memories == []
    assert repository.stream("user-1")["memories"][0]["lifecycle"]["status"] == "suppressed"


def test_stale_unread_memory_fades_without_repeating_claim(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "stale.sqlite3")
    old_end = datetime.now(timezone.utc) - timedelta(days=5)
    repository.ingest_batch(_batch(memory=_fact(
        startedAt=(old_end - timedelta(hours=1)).isoformat(),
        endedAt=old_end.isoformat(),
    )))
    first = repository.claim_opening({
        "userId": "user-1", "deviceId": "phone-1", "claimId": "stale-claim-1",
    })
    second = repository.claim_opening({
        "userId": "user-1", "deviceId": "phone-1", "claimId": "stale-claim-2",
    })
    assert first == {"shouldSend": False, "reason": "no_speakable_memory"}
    assert second == {"shouldSend": False, "reason": "no_new_memory"}
    lifecycle = repository.stream("user-1")["memories"][0]["lifecycle"]
    assert lifecycle["status"] == "dormant"
    assert lifecycle["freshness"] == "stale"


def test_explicit_active_update_refreshes_an_old_memory(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "reactivated.sqlite3")
    old_time = datetime.now(timezone.utc) - timedelta(days=5)
    emotion = {
        "id": "old-but-active", "revision": 1, "layer": "L3",
        "sourceType": "user_stated", "kind": "stated_emotion",
        "label": "frustrated", "quote": "我好烦",
        "occurredAt": old_time.isoformat(),
    }
    repository.ingest_batch(_batch(memory=emotion))
    repository.update_lifecycle({
        "userId": "user-1", "memoryId": "old-but-active", "status": "active",
        "reason": "user_said_it_is_still_ongoing",
    })
    refreshed = repository.stream("user-1")["memories"][0]
    assert refreshed["lifecycle"]["status"] == "active"
    assert refreshed["lifecycle"]["freshness"] == "fresh"
    opening = repository.claim_opening({
        "userId": "user-1", "deviceId": "phone-1", "claimId": "reactivated-opening",
    })
    assert opening["shouldSend"] is True
    assert opening["focusMemoryId"] == "old-but-active"


def test_translation_uses_longest_observed_group_and_does_not_invent_progress() -> None:
    text, focus_id = build_big_whale_opening([
        _fact("short", 1, durationSeconds=600, project={"name": "短项目"}),
        _fact("long", 2, durationSeconds=5400, project={"name": "鲸鱼应用"}),
    ])
    assert focus_id == "long"
    assert "鲸鱼应用" in text
    assert "1 小时 30 分钟" in text
    assert "不知道进展" in text
    assert "完成了" not in text

    emotion_text, _ = build_big_whale_opening([{
        "id": "emotion-1", "revision": 3, "layer": "L3",
        "sourceType": "user_stated", "kind": "stated_emotion",
        "label": "frustrated", "occurredAt": "2026-09-03T03:00:00+00:00",
    }])
    assert emotion_text.startswith("我还记得你")
    assert "小鲸说" not in emotion_text


def test_grounded_reply_only_uses_stated_emotion_and_asks_when_unclear() -> None:
    neutral = build_grounded_companion_reply("今天开了三个小时的会", [])
    emotional = build_grounded_companion_reply(
        "今天开了三个小时的会，我好烦啊", [], emotion_label="frustrated"
    )
    remembered = build_grounded_companion_reply("你记得我今天做了什么吗", [_fact()])
    assert "会" in neutral
    assert "？" in neutral
    assert "好烦" not in neutral
    assert "烦" in emotional
    assert "会议本身" in emotional
    assert "小鲸" in remembered
    assert "完成了" not in remembered

    generic = build_grounded_companion_reply("我只是想说一件新事情", [{
        "id": "old-emotion", "revision": 4, "layer": "L3",
        "sourceType": "user_stated", "kind": "stated_emotion",
        "label": "frustrated", "occurredAt": "2026-09-02T03:00:00+00:00",
    }])
    assert "有点烦" not in generic

    stopped = build_grounded_companion_reply("算了，不想说了", [])
    assert "？" not in stopped
    assert "不问" in stopped or "不说" in stopped or "先放这儿" in stopped

    sigh = build_grounded_companion_reply("唉……", [])
    assert "怎么" in sigh
    assert sigh.count("？") == 1
    assert "陪着" not in sigh


def test_short_emotional_dialogue_keeps_topic_then_respects_stop() -> None:
    first_conversation = [
        {"role": "user", "text": "唉"},
        {"role": "assistant", "text": "怎么了，是发生什么了吗？"},
        {"role": "user", "text": "工作上的事"},
    ]
    first = build_grounded_companion_reply(
        "工作上的事", [], conversation=first_conversation,
    )
    assert "这件事" in first and "工作上的事" not in first
    assert "叹气" in first
    assert first.count("？") == 1

    second_conversation = first_conversation + [
        {"role": "assistant", "text": first},
        {"role": "user", "text": "感觉自己没做好"},
    ]
    second = build_grounded_companion_reply(
        "感觉自己没做好", [], conversation=second_conversation,
    )
    assert "这件事" in second and "感觉自己没做好" not in second
    assert "怎么了" not in second
    assert second.count("？") == 1

    stopped = build_grounded_companion_reply(
        "算了，我不想聊了", [], conversation=second_conversation + [
            {"role": "assistant", "text": second},
            {"role": "user", "text": "算了，我不想聊了"},
        ],
    )
    assert "？" not in stopped
    assert "不说" in stopped or "不问" in stopped or "先放" in stopped


def test_memory_question_breaks_old_emotional_thread_and_answers_facts() -> None:
    conversation = [
        {"role": "user", "text": "唉"},
        {"role": "assistant", "text": "怎么了，是发生什么了吗？"},
        {"role": "user", "text": "你知道我今天干嘛了不"},
    ]
    reply = build_grounded_companion_reply(
        "你知道我今天干嘛了不", [_fact()], conversation=conversation,
    )
    assert "小鲸" in reply or "较了挺久" in reply
    assert "叹气" not in reply
    assert "最戳" not in reply
    assert emotional_dialogue_state(conversation)["phase"] == "idle"

    repair = build_grounded_companion_reply(
        "我问你啊", [_fact()], conversation=conversation + [
            {"role": "assistant", "text": "错误回复"},
            {"role": "user", "text": "我问你啊"},
        ],
    )
    assert "小鲸" in repair
    assert "最戳" not in repair

    unknown = build_grounded_companion_reply("你知道我今天干嘛了不", [])
    assert "没收到" in unknown


def test_companion_mind_uses_context_before_emotion_default() -> None:
    conversation = [
        {"role": "user", "text": "那个方案我还是不太喜欢"},
        {"role": "assistant", "text": "是太规整了，还是方向本身就不对？"},
        {"role": "user", "text": "就感觉不像我"},
    ]
    mind = build_companion_mind("就感觉不像我", conversation)
    assert mind["action"] == "interpret_from_recent_topic"
    assert mind["topicAnchor"] == "那个方案我还是不太喜欢"
    assert mind["previousCompanionTurn"].startswith("是太规整")

    question = build_companion_mind("那你觉得呢？", conversation + [
        {"role": "assistant", "text": "嗯。"},
        {"role": "user", "text": "那你觉得呢？"},
    ])
    assert question["action"] == "answer_directly"


def test_companion_frame_keeps_modules_separate_and_current_scene_first() -> None:
    conversation = [
        {"role": "assistant", "text": "你今天跟 DSH 较了挺久。"},
        {"role": "user", "text": "？"},
    ]
    frame = build_companion_frame(
        user_text="？", conversation=conversation, memories=[_fact()],
        user_facts=[], boundaries=[], persona=DEFAULT_PERSONA_CARD,
    )
    assert frame["sharedScene"]["referent"] == "previous_companion_turn"
    assert frame["turnDecision"]["primaryAction"] == "respond_to_own_previous_turn"
    assert frame["turnDecision"]["askQuestion"] is False
    assert frame["emotion"]["userEmotion"] == "unknown"
    assert frame["emotion"]["mayStateAsFact"] is False
    assert frame["speechStyle"]["allowAcknowledgementPrefix"] is False
    assert frame["safety"]["doNotInventUserPsychology"] is True


def test_repository_runs_emotional_continuity_without_creating_l3(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "short-dialogue.sqlite3")
    first = repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "sigh-1",
        "role": "user", "text": "唉",
    })
    second = repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "detail-1",
        "role": "user", "text": "工作上的事",
    })
    stopped = repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "stop-1",
        "role": "user", "text": "算了，我不想聊了",
    })

    assert "怎么" in first["assistantMessage"]["text"]
    assert second["assistantMessage"]["text"]
    assert "最戳" not in second["assistantMessage"]["text"]
    assert "？" not in stopped["assistantMessage"]["text"]
    assert repository.stream("user-1")["memories"] == []


def test_reply_is_atomic_idempotent_and_visible_in_history(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "memory.sqlite3")
    repository.ingest_batch(_batch())
    payload = {
        "userId": "user-1", "deviceId": "phone-1", "messageId": "reply-1",
        "role": "user", "text": "今天忙完了",
    }
    first = repository.append_message(payload)
    duplicate = repository.append_message(payload)
    assert first["assistantMessage"]["messageId"] == duplicate["assistantMessage"]["messageId"]
    assert duplicate["duplicate"] is True
    history = repository.messages("user-1")["messages"]
    assert [message["role"] for message in history] == ["user", "assistant"]
    assert len({message["messageId"] for message in history}) == 2


class _StubResponder:
    name = "测试模型"
    available = True

    def __init__(self, text: str = "模型记得这件事。") -> None:
        self.text = text
        self.calls = 0
        self.last_memories = []
        self.last_conversation = []

    def reply(self, memories: list[dict], conversation: list[dict], profile_memory=None) -> str:
        self.calls += 1
        self.last_memories = memories
        self.last_conversation = conversation
        self.last_profile_memory = profile_memory
        return self.text


class _FailingResponder(_StubResponder):
    def reply(self, memories: list[dict], conversation: list[dict], profile_memory=None) -> str:
        self.calls += 1
        raise RuntimeError("provider unavailable")


def test_memory_retrieval_merges_relevant_important_and_recent_buckets() -> None:
    memories = [
        _fact(memory_id="dsh-old", app="Visual Studio Code", title="DSH 项目", durationSeconds=300,
              endedAt="2026-09-01T10:00:00+00:00"),
        _fact(memory_id="meeting-important", app="腾讯会议", context="meeting", durationSeconds=10800,
              endedAt="2026-09-02T10:00:00+00:00"),
        _fact(memory_id="recent-browser", app="Chrome", title="新闻", durationSeconds=60,
              endedAt="2026-09-08T10:00:00+00:00"),
    ]
    selected, trace = select_companion_memories(memories, "DSH 今天进展怎么样")
    selected_ids = [row["id"] for row in selected]
    assert selected_ids[0] == "dsh-old"
    assert "meeting-important" in selected_ids
    assert "recent-browser" in selected_ids
    assert trace["strategy"] == "relevant_important_recent_v2_blended"
    assert trace["bucketMemoryIds"]["relevant"][0] == "dsh-old"
    assert trace["contextChars"] <= trace["contextBudgetChars"]


def test_insight_drafts_require_cross_event_evidence_and_never_enter_context() -> None:
    memories = [
        _fact(memory_id="dsh-day-1", project={"name": "DSH"}, endedAt="2026-09-07T10:00:00+08:00"),
        _fact(memory_id="dsh-day-2", project={"name": "DSH"}, endedAt="2026-09-08T10:00:00+08:00"),
        _fact(memory_id="single", project={"name": "一次性项目"}, endedAt="2026-09-08T11:00:00+08:00"),
    ]
    drafts = build_insight_drafts(memories)
    assert len(drafts) == 1
    assert drafts[0]["headline"] == "最近反复投入在 DSH"
    assert drafts[0]["evidenceMemoryIds"] == ["dsh-day-1", "dsh-day-2"]
    assert drafts[0]["status"] == "draft"
    assert drafts[0]["mayEnterModelContext"] is False


def test_confirmed_insight_enters_model_context_and_rejected_insight_does_not(tmp_path: Path) -> None:
    responder = _StubResponder("你最近确实总在碰 DSH。")
    repository = MemoryRepository(tmp_path / "insight-review.sqlite3", responder=responder)
    first = _fact(memory_id="dsh-day-1", revision=1, project={"name": "DSH"}, endedAt="2026-09-07T10:00:00+08:00")
    second = _fact(memory_id="dsh-day-2", revision=2, project={"name": "DSH"}, endedAt="2026-09-08T10:00:00+08:00")
    repository.ingest_batch({
        "protocolVersion": 1, "userId": "user-1", "deviceId": "desktop-1",
        "batchId": "insight-source", "latestRevision": 2, "memories": [first, second],
    })
    draft = repository.debug_snapshot("user-1")["insightDrafts"][0]
    confirmed = repository.review_insight({"userId": "user-1", "insightId": draft["id"], "action": "confirm"})
    assert confirmed["mayEnterModelContext"] is True
    repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "after-confirm",
        "role": "user", "text": "DSH 最近怎么样",
    })
    assert any(row.get("kind") == "confirmed_insight" for row in responder.last_memories)

    rejected = repository.review_insight({"userId": "user-1", "insightId": draft["id"], "action": "reject"})
    assert rejected["mayEnterModelContext"] is False
    snapshot = repository.debug_snapshot("user-1")
    assert snapshot["insightDrafts"][0]["status"] == "rejected"


def test_repository_uses_model_once_and_falls_back_safely(tmp_path: Path) -> None:
    responder = _StubResponder("我记得，小鲸记录的是一个小时。你想从哪段聊起？")
    repository = MemoryRepository(tmp_path / "model.sqlite3", responder=responder)
    repository.ingest_batch(_batch())
    payload = {
        "userId": "user-1", "deviceId": "phone-1", "messageId": "model-user-1",
        "role": "user", "text": "今天事情很多，想和你聊聊",
    }
    first = repository.append_message(payload)
    duplicate = repository.append_message(payload)
    assert first["replySource"] == "model"
    assert first["assistantMessage"]["text"].startswith("我记得")
    assert duplicate["replySource"] == "stored"
    assert responder.calls == 1
    assert responder.last_memories[0]["id"] == "fact-1"
    assert responder.last_conversation[-1] == {"role": "user", "text": payload["text"]}
    model_status = repository.companion_status()
    assert model_status["modelEnabled"] is True
    assert model_status["promptVersion"] == PROMPT_VERSION
    assert model_status["lastReplySource"] == "model"
    assert model_status["lastReplyAt"]

    fallback = _FailingResponder()
    fallback_repository = MemoryRepository(tmp_path / "fallback.sqlite3", responder=fallback)
    fallback_repository.ingest_batch(_batch())
    result = fallback_repository.append_message({**payload, "messageId": "fallback-user-1"})
    assert result["replySource"] == "fallback"
    assert result["assistantMessage"]["text"]
    assert fallback.calls == 1
    fallback_status = fallback_repository.companion_status()
    assert fallback_status["modelEnabled"] is True
    assert fallback_status["lastReplySource"] == "fallback"
    assert fallback_status["lastReplyAt"]


def test_direct_memory_question_uses_grounded_model_route(tmp_path: Path) -> None:
    responder = _StubResponder("知道，你今天主要在 DSH 项目上忙。")
    repository = MemoryRepository(tmp_path / "memory-route.sqlite3", responder=responder)
    repository.ingest_batch(_batch())
    result = repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "memory-question-1",
        "role": "user", "text": "你知道我今天干嘛了不",
    })
    assert result["replySource"] == "model"
    assert "DSH" in result["assistantMessage"]["text"]
    assert "叹气" not in result["assistantMessage"]["text"]
    assert responder.calls == 1
    trace = repository.debug_snapshot("user-1")["replyTraces"][0]
    assert trace["route"] == "model_first"
    assert trace["intent"] == "memory_check"
    assert trace["modelAttempted"] is True
    assert trace["modelAccepted"] is True
    assert trace["finalSource"] == "model"


def test_prompt_to_speak_has_content_without_service_closure(tmp_path: Path) -> None:
    responder = _StubResponder(
        "嗯，那我说。你好像总是一个人扛着。现在想聊点轻松的吗，还是待着也行。"
    )
    repository = MemoryRepository(tmp_path / "natural-turn.sqlite3", responder=responder)
    repository.ingest_batch(_batch())
    result = repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "speak-1",
        "role": "user", "text": "说话",
    })
    reply = result["assistantMessage"]["text"]
    assert result["replySource"] == "fallback"
    assert not reply.startswith("嗯，那我说")
    assert "一个人扛" not in reply
    assert "还是" not in reply
    assert "小鲸" in reply
    assert responder.calls == 1
    first_trace = repository.debug_snapshot("user-1")["replyTraces"][0]
    assert first_trace["intent"] == "prompt_to_speak"
    assert first_trace["rejectionReason"] == "style_violation"

    reaction = repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "speak-reaction-1",
        "role": "user", "text": "？",
    })
    assert reaction["replySource"] == "fallback"
    assert reaction["assistantMessage"]["text"] == "怎么，我刚才那句很怪？"
    assert "答不上来" not in reaction["assistantMessage"]["text"]
    assert responder.calls == 2


def test_style_firewall_rejects_service_like_companion_turns() -> None:
    assert reply_has_style_violation("嗯，那我说。今天挺忙的。", "说话")
    assert reply_has_style_violation("你好像总是一个人扛着。", "说话")
    assert reply_has_style_violation("想聊点轻松的吗，还是待着也行。", "说话")
    assert not reply_has_style_violation("你今天跟 DSH 较上劲挺久。", "说话")


def test_explicit_profile_facts_boundaries_conflicts_and_actions(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "profile.sqlite3")
    repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "profile-1",
        "role": "user", "text": "我叫宇杨，我已经工作了，不是学生",
    })
    profile = repository.profile_memories("user-1")
    current = {row["predicate"]: row for row in profile["userFacts"] if row["status"] == "confirmed"}
    assert current["name"]["value"] == "宇杨"
    assert current["name"]["evidence"][0]["quote"].startswith("我叫宇杨")
    assert current["life_stage"]["value"] == "working"
    assert any("不得把用户当作学生" in row["rule"] for row in profile["boundaries"])

    repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "profile-2",
        "role": "user", "text": "我叫小宇",
    })
    names = [row for row in repository.profile_memories("user-1")["userFacts"] if row["predicate"] == "name"]
    assert {row["status"] for row in names} == {"confirmed", "corrected"}
    old = next(row for row in names if row["value"] == "宇杨")
    assert any(item["type"] == "contradiction" for item in old["evidence"])
    new = next(row for row in names if row["value"] == "小宇")
    repository.update_profile_memory({"userId": "user-1", "memoryType": "userFact", "id": new["id"], "action": "pause"})
    assert next(row for row in repository.profile_memories("user-1")["userFacts"] if row["id"] == new["id"])["status"] == "paused"
    repository.update_profile_memory({"userId": "user-1", "memoryType": "userFact", "id": new["id"], "action": "delete"})
    assert next(row for row in repository.profile_memories("user-1")["userFacts"] if row["id"] == new["id"])["status"] == "forgotten"


def test_profile_memory_reaches_model_and_student_boundary_blocks_reply(tmp_path: Path) -> None:
    responder = _StubResponder("你明天有早课吗？")
    repository = MemoryRepository(tmp_path / "profile-model.sqlite3", responder=responder)
    result = repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "boundary-1",
        "role": "user", "text": "我已经工作了，不是学生",
    })
    assert responder.last_profile_memory["userFacts"][0]["value"] == "working"
    assert responder.last_profile_memory["boundaries"]
    assert not model_reply_is_grounded(
        "你明天有早课吗？", [], [{"role": "user", "text": "我已经工作了，不是学生"}],
        responder.last_profile_memory,
    )
    assert result["replySource"] == "fallback"


def test_persona_card_draft_preview_publish_and_rollback(tmp_path: Path) -> None:
    responder = _StubResponder("这句带着草稿人设。")
    repository = MemoryRepository(tmp_path / "persona.sqlite3", responder=responder)
    initial = repository.persona_card("user-1")
    assert initial["active"]["name"] == "大鲸"
    draft = dict(initial["active"])
    draft["traits"] = ["锋利", "话少"]
    saved = repository.update_persona_card({
        "userId": "user-1", "action": "saveDraft", "card": draft,
    })
    assert saved["draftVersion"] == 2
    assert saved["active"]["traits"] != draft["traits"]

    preview = repository.preview_persona({
        "userId": "user-1", "text": "随便说一句", "useDraft": True,
    })
    assert preview["persisted"] is False
    assert preview["using"] == "draft"
    assert responder.last_profile_memory["personaCard"]["traits"] == ["锋利", "话少"]
    assert repository.messages("user-1")["messages"] == []

    published = repository.update_persona_card({"userId": "user-1", "action": "publish"})
    assert published["activeVersion"] == 2
    assert published["active"]["traits"] == ["锋利", "话少"]
    rolled = repository.update_persona_card({"userId": "user-1", "action": "rollback", "version": 1})
    assert rolled["activeVersion"] == 3
    assert rolled["active"]["name"] == "大鲸"


def test_model_adapter_sends_grounded_context_to_openai_compatible_api(monkeypatch) -> None:
    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps({
                "choices": [{"message": {"content": "我知道的只有小鲸记下的这些。"}}]
            }, ensure_ascii=False).encode("utf-8")

    def fake_urlopen(request, **kwargs):
        captured["request"] = request
        captured["kwargs"] = kwargs
        return _Response()

    monkeypatch.setattr(
        "whale_companion_service.companion_llm.urllib.request.urlopen", fake_urlopen
    )
    config = ProviderConfig(
        "test", name="Mock", base_url="https://model.example/v1",
        model="mock-chat", api_key="server-secret", timeout=12,
    )
    responder = ModelCompanionResponder(config)
    result = responder.reply([_fact()], [{"role": "user", "text": "我今天做了什么？"}])
    request_body = json.loads(captured["request"].data.decode("utf-8"))
    assert result == "我知道的只有小鲸记下的这些。"
    assert request_body["messages"][-1]["content"] == "我今天做了什么？"
    assert "事实底线" in request_body["messages"][0]["content"]
    assert "最多问一个问题" in request_body["messages"][0]["content"]
    assert "不等于拒绝交流" in request_body["messages"][0]["content"]
    assert '<dialogue_state>{"phase":"idle","turn":0}</dialogue_state>' in request_body["messages"][0]["content"]
    assert "Visual Studio Code" in request_body["messages"][0]["content"]
    assert captured["request"].get_header("Authorization") == "Bearer server-secret"
    assert captured["kwargs"]["timeout"] == 12


def test_model_context_exposes_only_memory_whitelist() -> None:
    context = json.loads(build_model_context([_fact(secretField="must-not-leak")]))
    encoded = json.dumps(context, ensure_ascii=False)
    assert "must-not-leak" not in encoded
    assert "Visual Studio Code" in encoded


def test_deepseek_companion_request_disables_thinking(monkeypatch) -> None:
    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps({
                "choices": [{"message": {"content": "嗯，你继续。"}}]
            }, ensure_ascii=False).encode("utf-8")

    def fake_urlopen(request, **_kwargs):
        captured.update(json.loads(request.data.decode("utf-8")))
        return _Response()

    monkeypatch.setattr(
        "whale_companion_service.companion_llm.urllib.request.urlopen", fake_urlopen
    )
    responder = ModelCompanionResponder(ProviderConfig(
        "deepseek", base_url="https://api.deepseek.com",
        model="deepseek-v4-flash", api_key="secret", max_tokens=2048,
    ))
    assert responder.reply([], [{"role": "user", "text": "今天事情很多"}])
    assert captured["thinking"] == {"type": "disabled"}
    assert captured["max_tokens"] == 320


def test_model_fact_firewall_rejects_unstated_emotion_duration_and_progress() -> None:
    conversation = [{"role": "user", "text": "今天事情很多"}]
    memories = [_fact()]
    assert model_reply_is_grounded("我在听，你想先聊哪一件？", memories, conversation)
    assert not model_reply_is_grounded("听起来你今天很累。", memories, conversation)
    assert not model_reply_is_grounded("你今天忙了 8 小时。", memories, conversation)
    assert not model_reply_is_grounded("你的项目已经完成了。", memories, conversation)
    assert model_reply_is_grounded("项目做完了吗？", memories, conversation)


def test_http_client_auth_batch_stream_and_opening(tmp_path: Path) -> None:
    server = MemoryApiServer(
        MemoryRepository(tmp_path / "memory.sqlite3"), token="secret", port=0
    )
    port = server.start()
    try:
        client = MemorySyncClient(f"http://127.0.0.1:{port}", "secret")
        ack = client.post_batch(_batch())
        assert ack["accepted"] is True
        assert client.stream("user-1")["memories"][0]["id"] == "fact-1"
        opening = client.claim_opening(
            user_id="user-1", device_id="phone-1", claim_id="claim-http-1"
        )
        assert opening["shouldSend"] is True
        reply = client.post_message(
            user_id="user-1", device_id="phone-1", message_id="reply-http-1",
            role="user", text="我忙完了",
        )
        assert reply["assistantMessage"]["role"] == "assistant"
        assert [row["role"] for row in client.messages("user-1")["messages"]] == [
            "assistant", "user", "assistant",
        ]
        lifecycle = client.update_lifecycle(
            user_id="user-1", memory_id="fact-1", status="resolved",
            reason="user_confirmed_done",
        )
        assert lifecycle["lifecycle"]["status"] == "resolved"
        assert client.stream("user-1")["memories"][0]["lifecycle"]["status"] == "resolved"

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
            page = response.read().decode("utf-8")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/debug/", timeout=2) as response:
            debug_page = response.read().decode("utf-8")
        debug_request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/debug/state?userId=user-1",
            headers={"Authorization": "Bearer secret"},
        )
        with urllib.request.urlopen(debug_request, timeout=2) as response:
            debug_state = json.loads(response.read().decode("utf-8"))
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
            health = json.loads(response.read().decode("utf-8"))
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/media/ojingjing.jpg", timeout=2
        ) as response:
            icon = response.read()
        assert "她记得白天发生的事" in page
        assert "回声闭环控制台" in debug_page
        assert "统一身份" in debug_page
        assert "当前理解与动作" in debug_page
        assert debug_state["memories"][0]["id"] == "fact-1"
        assert "大鲸" in debug_state["persona"]["systemPrompt"]
        assert health["companion"]["modelEnabled"] is False
        assert len(icon) > 1000

        bad_client = MemorySyncClient(f"http://127.0.0.1:{port}", "wrong")
        with pytest.raises(SyncTransportError, match="401"):
            bad_client.stream("user-1")
    finally:
        server.stop()


def test_phone_http_request_reaches_openai_compatible_model(tmp_path: Path) -> None:
    captured = {}

    class ModelHandler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            return

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            captured["path"] = self.path
            captured["body"] = json.loads(self.rfile.read(length).decode("utf-8"))
            body = json.dumps({
                "choices": [{"message": {"content": "我在听。你想先从哪件事说起？"}}]
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    model_server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    model_thread = threading.Thread(target=model_server.serve_forever, daemon=True)
    model_thread.start()
    config = ProviderConfig(
        "local-model", name="Local model",
        base_url=f"http://127.0.0.1:{model_server.server_address[1]}",
        model="test-chat", timeout=5,
    )
    memory_server = MemoryApiServer(
        MemoryRepository(
            tmp_path / "model-e2e.sqlite3",
            responder=ModelCompanionResponder(config),
        ),
        token="secret",
        port=0,
    )
    memory_port = memory_server.start()
    try:
        client = MemorySyncClient(f"http://127.0.0.1:{memory_port}", "secret")
        response = client.post_message(
            user_id="user-1", device_id="phone-1", message_id="model-e2e-user-1",
            role="user", text="今天有不少事情想说",
        )
        assert response["replySource"] == "model"
        assert response["assistantMessage"]["text"] == "我在听。你想先从哪件事说起？"
        assert captured["path"] == "/v1/chat/completions"
        assert captured["body"]["messages"][-1]["content"] == "今天有不少事情想说"
        with urllib.request.urlopen(
            f"http://127.0.0.1:{memory_port}/health", timeout=2
        ) as health_response:
            health = json.loads(health_response.read().decode("utf-8"))
            assert health["companion"]["promptVersion"] == PROMPT_VERSION
        assert health["companion"]["lastReplySource"] == "model"
    finally:
        memory_server.stop()
        model_server.shutdown()
        model_server.server_close()
        model_thread.join(2)


def _thread(**overrides: object) -> dict:
    base = {
        "threadId": "thread:m1", "label": "frustrated", "topic": "会开了一下午",
        "status": "acknowledged", "need": "vent", "followUpHint": "",
        "mentionCount": 1, "openedAt": "", "lastMentionedAt": "", "updatedAt": "",
    }
    base.update(overrides)
    return base


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def test_open_threads_carry_recent_emotion_and_let_stale_ones_fade() -> None:
    fresh = build_open_threads([_thread(updatedAt=_ago(0.5))])
    assert fresh["openCount"] == 1
    assert fresh["carriedEmotion"] == "frustrated"
    assert fresh["carriedFromThread"] == "thread:m1"
    assert fresh["threads"][0]["daysSinceLastTouch"] == pytest.approx(0.5, abs=0.05)

    # 情绪会过去：隔了太久就不再拿旧线程理解今天这句话。
    stale = build_open_threads([_thread(updatedAt=_ago(9))])
    assert stale["openCount"] == 1
    assert stale["carriedEmotion"] == ""
    assert stale["pendingFollowUps"] == []

    # 已经闭合的线程不进入当前帧。
    closed = build_open_threads([_thread(status="resolved", updatedAt=_ago(0.1))])
    assert closed["openCount"] == 0
    assert closed["carriedEmotion"] == ""


def test_open_threads_keep_user_commitments_longer_than_emotion() -> None:
    rows = [_thread(followUpHint="过两天再问我", updatedAt=_ago(5))]
    state = build_open_threads(rows)
    # 情绪已经不带了，但用户亲口交代的约定还在。
    assert state["carriedEmotion"] == ""
    assert [(item["threadId"], item["hint"]) for item in state["pendingFollowUps"]] == [
        ("thread:m1", "过两天再问我")
    ]
    # 约定也在变淡，只是比情绪慢。
    assert 0.25 <= state["pendingFollowUps"][0]["weight"] < 1.0
    assert build_open_threads([_thread(followUpHint="过两天再问我", updatedAt=_ago(30))])["pendingFollowUps"] == []


def test_carried_emotion_never_becomes_a_stateable_fact() -> None:
    frame = build_companion_frame(
        user_text="在改一个脚本", conversation=[{"role": "user", "text": "在改一个脚本"}],
        memories=[], user_facts=[], boundaries=[], persona=DEFAULT_PERSONA_CARD,
        threads=[_thread(updatedAt=_ago(0.5))],
    )
    emotion = frame["emotion"]
    assert emotion["userEmotion"] == "unknown"
    assert emotion["confidence"] == 0.0
    assert emotion["source"] == "carried_thread"
    assert emotion["carriedEmotion"] == "frustrated"
    assert emotion["mayStateAsFact"] is False
    # 带回来的情绪必须仍然受"不得推断情绪"约束。
    assert frame["safety"]["doNotInferEmotion"] is True
    assert frame["relationship"]["openThreadCount"] == 1


def test_user_stated_emotion_this_turn_outranks_the_carried_thread() -> None:
    frame = build_companion_frame(
        user_text="我今天很开心", conversation=[{"role": "user", "text": "我今天很开心"}],
        memories=[], user_facts=[], boundaries=[], persona=DEFAULT_PERSONA_CARD,
        threads=[_thread(label="frustrated", updatedAt=_ago(0.2))],
    )
    emotion = frame["emotion"]
    assert emotion["userEmotion"] == "happy"
    assert emotion["confidence"] == 1.0
    assert emotion["carriedEmotion"] == ""
    assert emotion["mayStateAsFact"] is True
    assert frame["safety"]["doNotInferEmotion"] is False


def test_open_thread_survives_into_a_later_unrelated_turn(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "carry.sqlite3")
    repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "anxious-1",
        "role": "user", "text": "我很焦虑，明天的汇报还没准备好，过两天再问我",
    })
    # 换了一个完全无关的话题，线程仍然应该跟过来。
    repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "later-1",
        "role": "user", "text": "在改一个脚本",
    })
    snapshot = repository.debug_snapshot("user-1")
    open_threads = snapshot["companionFrame"]["openThreads"]
    assert open_threads["openCount"] == 1
    assert open_threads["carriedEmotion"] == "anxious"
    assert [item["hint"] for item in open_threads["pendingFollowUps"]] == ["过两天再问我"]
    assert snapshot["companionFrame"]["relationship"]["commitments"][0]["hint"] == "过两天再问我"
    # 帧带来了背景，但没有把它升级成"用户此刻很焦虑"。
    assert snapshot["companionFrame"]["emotion"]["mayStateAsFact"] is False
    assert "openThreads" in snapshot["persona"]["modelContext"]


def test_carry_weight_fades_gradually_instead_of_expiring() -> None:
    weights = [
        build_open_threads([_thread(updatedAt=_ago(days))])["carriedWeight"]
        for days in (0.0, 1.5, 3.0)
    ]
    # 半衰期 1.5 天：今天 1.0，明天后半 0.5，第三天 0.25 —— 逐级变淡而不是到期作废。
    assert weights == [1.0, 0.5, 0.25]
    assert build_open_threads([_thread(updatedAt=_ago(4))])["carriedEmotion"] == ""


def test_barely_relevant_memory_no_longer_outranks_an_important_recent_one() -> None:
    memories = [
        # "今天" 里的二元组会在标题上偶然命中，旧口径下这条会被顶到第一。
        _fact(memory_id="coincidence", app="Chrome", title="今天天气", durationSeconds=30,
              endedAt="2026-09-01T10:00:00+00:00"),
        _fact(memory_id="long-meeting", app="腾讯会议", context="meeting", durationSeconds=10800,
              endedAt="2026-09-08T10:00:00+00:00"),
    ]
    selected, trace = select_companion_memories(memories, "今天怎么样")
    assert trace["bucketMemoryIds"]["relevant"][0] == "coincidence"
    assert [row["id"] for row in selected][0] == "long-meeting"
    assert trace["blendedScores"]["long-meeting"] > trace["blendedScores"]["coincidence"]


def test_standing_knowledge_only_aggregates_evidenced_rows() -> None:
    entries = derive_standing_knowledge(
        facts=[
            {"id": "fact-name", "predicate": "name", "value": "小羊", "status": "confirmed",
             "evidence": [{"type": "support"}, {"type": "support"}]},
            # candidate 还没坐实，不进常驻层。
            {"id": "fact-maybe", "predicate": "name", "value": "阿羊", "status": "candidate",
             "evidence": [{"type": "support"}]},
            # 没有固化措辞的 predicate 留在召回层。
            {"id": "fact-other", "predicate": "favorite_color", "value": "蓝", "status": "confirmed",
             "evidence": []},
        ],
        boundaries=[
            {"id": "b-1", "status": "active", "rule": "不要称呼用户为“宝宝”。"},
            {"id": "b-2", "status": "paused", "rule": "不要提起考试。"},
        ],
        threads=[
            {"threadId": "t-1", "need": "listening"},
            {"threadId": "t-2", "need": "listening"},
            {"threadId": "t-3", "need": "unknown"},
        ],
    )
    texts = {entry["text"] for entry in entries}
    assert "用户叫小羊。" in texts
    assert "不要称呼用户为“宝宝”。" in texts
    assert "用户说过这类事他要的是有人听着，不是建议。" in texts
    assert not any("阿羊" in text or "蓝" in text or "考试" in text for text in texts)

    listening = next(entry for entry in entries if "有人听着" in entry["text"])
    # 同一种需求来自两条线程，证据合并而不是各记一条。
    assert listening["basedOn"] == ["t-1", "t-2"]
    assert listening["sourceCount"] == 2
    assert all(entry["basedOn"] for entry in entries), "每条常驻认知都必须能指回来源"


def test_standing_knowledge_context_is_background_not_a_topic() -> None:
    context = standing_knowledge_context(derive_standing_knowledge(
        facts=[{"id": "f", "predicate": "life_stage", "value": "working",
                "status": "confirmed", "evidence": [{"type": "support"}]}],
        boundaries=[], threads=[],
    ))
    assert "不要主动把它当话题提起" in context["usage"]
    assert context["plates"] == [
        {"title": "TA的事", "entries": ["用户已经在工作，不是学生。"]}
    ]


def test_standing_knowledge_reaches_the_model_context(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "standing.sqlite3")
    repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "intro-1",
        "role": "user", "text": "我叫小羊，别再叫我宝宝",
    })
    snapshot = repository.debug_snapshot("user-1")
    texts = {entry["text"] for entry in snapshot["standingKnowledge"]}
    assert "用户叫小羊。" in texts
    assert any("宝宝" in text for text in texts)
    # 证据仍然指回真实的事实与边界行。
    sources = {source for entry in snapshot["standingKnowledge"] for source in entry["basedOn"]}
    known = {row["id"] for row in snapshot["userFacts"]} | {row["id"] for row in snapshot["boundaries"]}
    assert sources <= known
    assert "standingKnowledge" in snapshot["persona"]["modelContext"]
    assert "小羊" in snapshot["persona"]["modelContext"]
