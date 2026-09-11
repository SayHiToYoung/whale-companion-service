from __future__ import annotations

import io
import json
import socket
import urllib.error

import pytest

from whale_companion_service.companion_llm import (
    MAX_MODEL_RESPONSE_BYTES,
    CompanionModelError,
    ModelCompanionResponder,
    REJECTION_CODES,
    model_reply_rejection,
)
from whale_companion_service.memory_server import MemoryRepository
from whale_companion_service.provider import ProviderConfig


def _responder() -> ModelCompanionResponder:
    return ModelCompanionResponder(ProviderConfig(
        provider_id="test",
        name="Test provider",
        base_url="https://model.example/v1",
        model="test-model",
        api_key="test-only-key",
        timeout=3,
    ))


class _Response:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int) -> bytes:
        return self.raw


def _assert_call_code(monkeypatch, effect, expected: str) -> None:
    responder = _responder()

    def fake_urlopen(*_args, **_kwargs):
        if isinstance(effect, BaseException):
            raise effect
        return _Response(effect)

    monkeypatch.setattr("whale_companion_service.companion_llm.urllib.request.urlopen", fake_urlopen)
    with pytest.raises(CompanionModelError) as caught:
        responder._call_chat([], temperature=0.0, max_tokens=80)
    assert caught.value.reason_code == expected


def test_provider_failures_have_stable_codes_without_using_error_text(monkeypatch):
    _assert_call_code(monkeypatch, socket.timeout("unstable timeout text"), "provider_timeout")
    _assert_call_code(monkeypatch, b"{}", "provider_invalid_response")
    _assert_call_code(
        monkeypatch,
        b'{"choices":[{"message":{"content":{"unexpected":true}}}]}',
        "provider_invalid_response",
    )
    _assert_call_code(
        monkeypatch, b"x" * (MAX_MODEL_RESPONSE_BYTES + 1), "empty_or_oversize_reply",
    )
    http_error = urllib.error.HTTPError(
        "https://model.example", 503, "unstable provider text", {},
        io.BytesIO(b'{"error":{"message":"must not become a reason code"}}'),
    )
    _assert_call_code(monkeypatch, http_error, "provider_unavailable")
    gateway_timeout = urllib.error.HTTPError(
        "https://model.example", 504, "arbitrary", {}, io.BytesIO(b"secret body"),
    )
    _assert_call_code(monkeypatch, gateway_timeout, "provider_timeout")


def test_empty_content_and_missing_configuration_have_stable_codes(monkeypatch):
    empty = json.dumps({"choices": [{"message": {"content": ""}}]}).encode()
    monkeypatch.setattr(
        "whale_companion_service.companion_llm.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(empty),
    )
    with pytest.raises(CompanionModelError) as caught:
        _responder().reply([], [{"role": "user", "text": "hello"}])
    assert caught.value.reason_code == "empty_or_oversize_reply"

    unavailable = ModelCompanionResponder(ProviderConfig(
        base_url="https://model.example", model="test-model", api_key="",
    ))
    with pytest.raises(CompanionModelError) as caught:
        unavailable.reply([], [{"role": "user", "text": "hello"}])
    assert caught.value.reason_code == "provider_unavailable"


class _CodedFailure:
    name = "coded/test"
    available = True

    def __init__(self, code: str) -> None:
        self.code = code

    def reply(self, *_args, **_kwargs):
        raise CompanionModelError(reason_code=self.code, stage="provider")

    def judge_json(self, *, fallback, **_kwargs):
        return dict(fallback), "fallback"


class _TextResponder:
    name = "text/test"
    available = True

    def __init__(self, text: str) -> None:
        self.text = text

    def reply(self, *_args, **_kwargs):
        return self.text


def test_reply_trace_debug_snapshot_and_health_expose_content_free_diagnostics(tmp_path):
    repo = MemoryRepository(tmp_path / "observability.sqlite3")
    for index, code in enumerate((
        "provider_unavailable", "provider_timeout", "provider_invalid_response",
    )):
        repo.responder = _CodedFailure(code)
        result = repo.append_message({
            "userId": "u", "deviceId": "d", "messageId": f"m-{index}",
            "role": "user", "text": f"neutral turn {index}",
        })
        assert result["replySource"] == "fallback"

    snapshot = repo.debug_snapshot("u")
    latest = snapshot["replyTraces"][0]
    assert latest["reasonCode"] == "provider_invalid_response"
    assert latest["rejectionReason"] == "CompanionModelError"
    assert latest["fallbackClassification"] == "provider_failure"
    assert set(latest["companionFrame"]) == {"version", "generationBlocked"}
    assert "processedFacts" not in latest["companionFrame"]
    assert latest["contextSummary"]["processedFactCount"] > 0
    assert "userText" not in latest
    assert "query" not in latest["memoryRetrieval"]
    assert "candidate" not in latest

    diagnostics = snapshot["replyDiagnostics"]
    assert diagnostics["reasonCodes"] == list(REJECTION_CODES)
    assert diagnostics["reasonCodeCounts"]["provider_unavailable"] == 1
    assert diagnostics["reasonCodeCounts"]["provider_timeout"] == 1
    assert diagnostics["reasonCodeCounts"]["provider_invalid_response"] == 1

    status = repo.health_status()["companion"]
    assert status["lastRejectionCode"] == "provider_invalid_response"
    assert status["lastRejectionStage"] == "provider"
    assert status["diagnostics"]["reasonCodeCounts"] == diagnostics["reasonCodeCounts"]


def test_single_and_multiple_firewall_hits_keep_stable_primary_and_parallel_rules():
    conversation = [{"role": "user", "text": "今天想找点轻松的事做"}]
    single = model_reply_rejection("你现在肯定很难过。", [], conversation)
    assert single is not None
    assert single.code == "emotion_without_evidence"
    assert single.matched_codes == ("emotion_without_evidence",)
    assert single.matched_rules == ("emotion_requires_current_user_evidence",)

    multiple = model_reply_rejection(
        "我看见你桌面开着软件。你现在肯定很难过。", [], conversation,
    )
    assert multiple is not None
    assert multiple.code == "unsupported_desktop_claim"
    assert multiple.rule == "desktop_observation_requires_l1"
    assert multiple.matched_codes == (
        "unsupported_desktop_claim", "emotion_without_evidence",
    )
    assert multiple.matched_rules == (
        "desktop_observation_requires_l1", "emotion_requires_current_user_evidence",
    )
    assert len(multiple.matched_codes) == len(multiple.matched_rules)


def test_health_aggregates_every_code_from_one_rejected_candidate(tmp_path):
    repo = MemoryRepository(tmp_path / "multi.sqlite3")
    repo.responder = _TextResponder(
        "我看见你桌面开着软件。你现在肯定很难过。",
    )
    result = repo.append_message({
        "userId": "u", "deviceId": "d", "messageId": "m",
        "role": "user", "text": "今天想找点轻松的事做",
    })
    assert result["replySource"] == "fallback"
    diagnostics = repo.health_status()["companion"]["diagnostics"]
    assert diagnostics["reasonCodeCounts"]["unsupported_desktop_claim"] == 1
    assert diagnostics["reasonCodeCounts"]["emotion_without_evidence"] == 1
    assert diagnostics["fallbackClassificationCounts"]["output_rejection"] == 1


def test_unknown_output_firewall_code_is_conservatively_attributed_without_error_text():
    error = CompanionModelError(
        "must not classify from this prose",
        reason_code="future_rule_code",
        stage="output_firewall",
        rule="future_grounding_rule",
    )
    assert error.reason_code == "unknown_grounding_violation"
    assert error.matched_reason_codes == ("unknown_grounding_violation",)
    assert error.matched_rules == ("future_grounding_rule",)


def test_health_diagnostics_are_bounded_read_only_and_support_legacy_reason(tmp_path):
    repo = MemoryRepository(tmp_path / "bounded.sqlite3")
    with repo._connect() as db:
        for index in range(1005):
            trace = {
                "reasonCode": "provider_timeout",
                "matchedReasonCodes": ["provider_timeout"],
                "fallbackClassification": "provider_failure",
                "finalSource": "fallback",
            }
            db.execute(
                """INSERT INTO reply_traces
                   (user_id, reply_message_id, trace_json, created_at) VALUES (?, ?, ?, ?)""",
                ("u", f"r-{index}", json.dumps(trace), f"2026-01-01T00:{index:04d}:00Z"),
            )
        db.execute(
            """INSERT INTO reply_traces
               (user_id, reply_message_id, trace_json, created_at) VALUES (?, ?, ?, ?)""",
            ("legacy", "legacy-r", json.dumps({
                "rejectionReason": "grounding_violation", "finalSource": "fallback",
            }), "2026-01-02T00:00:00Z"),
        )
    with repo._connect() as db:
        before = "\n".join(db.iterdump())
    health = repo.health_status()["companion"]["diagnostics"]
    with repo._connect() as db:
        after = "\n".join(db.iterdump())
    assert before == after
    assert health["sampleSize"] == health["sampleLimit"] == 1000
    assert sum(health["reasonCodeCounts"].values()) == 1000
    assert health["reasonCodeCounts"]["provider_timeout"] == 999
    assert health["reasonCodeCounts"]["unknown_grounding_violation"] == 1
    legacy = repo._reply_diagnostics(user_id="legacy")
    assert legacy["reasonCodeCounts"]["unknown_grounding_violation"] == 1
