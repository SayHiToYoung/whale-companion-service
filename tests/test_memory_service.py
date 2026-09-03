from __future__ import annotations

import urllib.request
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from whale_companion_service.provider import ProviderConfig
from whale_companion_service.companion_llm import (
    ModelCompanionResponder,
    build_model_context,
    model_reply_is_grounded,
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

    def reply(self, memories: list[dict], conversation: list[dict]) -> str:
        self.calls += 1
        self.last_memories = memories
        self.last_conversation = conversation
        return self.text


class _FailingResponder(_StubResponder):
    def reply(self, memories: list[dict], conversation: list[dict]) -> str:
        self.calls += 1
        raise RuntimeError("provider unavailable")


def test_repository_uses_model_once_and_falls_back_safely(tmp_path: Path) -> None:
    responder = _StubResponder("我记得，小鲸记录的是一个小时。你想从哪段聊起？")
    repository = MemoryRepository(tmp_path / "model.sqlite3", responder=responder)
    repository.ingest_batch(_batch())
    payload = {
        "userId": "user-1", "deviceId": "phone-1", "messageId": "model-user-1",
        "role": "user", "text": "你记得我今天做了什么吗？",
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
    assert model_status["promptVersion"] == "big-whale-human-v3-lifecycle"
    assert model_status["lastReplySource"] == "model"
    assert model_status["lastReplyAt"]

    fallback = _FailingResponder()
    fallback_repository = MemoryRepository(tmp_path / "fallback.sqlite3", responder=fallback)
    fallback_repository.ingest_batch(_batch())
    result = fallback_repository.append_message({**payload, "messageId": "fallback-user-1"})
    assert result["replySource"] == "fallback"
    assert "小鲸" in result["assistantMessage"]["text"]
    assert fallback.calls == 1
    fallback_status = fallback_repository.companion_status()
    assert fallback_status["modelEnabled"] is True
    assert fallback_status["lastReplySource"] == "fallback"
    assert fallback_status["lastReplyAt"]


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
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
            health = json.loads(response.read().decode("utf-8"))
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/media/ojingjing.jpg", timeout=2
        ) as response:
            icon = response.read()
        assert "她记得白天发生的事" in page
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
        assert health["companion"]["promptVersion"] == "big-whale-human-v3-lifecycle"
        assert health["companion"]["lastReplySource"] == "model"
    finally:
        memory_server.stop()
        model_server.shutdown()
        model_server.server_close()
        model_thread.join(2)
