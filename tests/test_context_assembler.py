from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from whale_companion_service.companion_runtime.context_contract import (
    ContextFact, ContextFragment, ContextBudgetError, Priority, token_cost,
)
from whale_companion_service.companion_runtime.context_assembler import ContextAssembler
from whale_companion_service.companion_runtime import build_companion_frame
from whale_companion_service.companion_runtime.proactive import evaluate_proactive_lifecycle
from whale_companion_service.companion_llm import ModelCompanionResponder
from whale_companion_service.provider import ProviderConfig

NOW = datetime(2026, 9, 9, 10, tzinfo=timezone.utc)
AT = NOW.isoformat()


def fragment(value="hello", *, key="fact", module="memory", source="observed", confidence=0.8, **kwargs):
    return ContextFragment(module=module, priority=Priority.MEMORY,
        facts=(ContextFact(key, value, source=source),), source_event_ids=("e1",),
        confidence=confidence, created_at=AT, **kwargs)


def assemble(*fragments, **kwargs):
    return ContextAssembler(**kwargs).assemble(list(fragments), now=NOW)


def test_expired_fragments_and_facts_and_nonactive_lifecycles_are_removed():
    expired = fragment(expires_at=AT)
    fact_expired = replace(fragment(), facts=(ContextFact("fact", "old", expires_at=AT),))
    forgotten = replace(fragment(), facts=(ContextFact("fact", "forgotten", lifecycle="forgotten"),))
    assert assemble(expired, fact_expired, forgotten)["processedFacts"] == []
    # A child may shorten a fragment's life but cannot prolong it.
    shorter = replace(fragment(expires_at=(NOW + timedelta(minutes=1)).isoformat()),
                      facts=(ContextFact("fact", "new", expires_at=(NOW + timedelta(days=1)).isoformat()),))
    assert assemble(shorter)["processedFacts"][0]["expires_at"] == shorter.expires_at


def test_boundaries_beat_priority_confidence_and_filter_private_style_and_history():
    boundary = ContextFragment(module="boundaries", priority=0, instructions=("不得提秘密项目",),
        facts=(ContextFact("permission", False),), source_event_ids=("user:1",), created_at=AT,
        metadata={"blocked_terms": ["秘密项目"], "blocked_modules": ["story"]})
    hostile = replace(fragment(True, key="permission", confidence=1), priority=999999)
    private = fragment("private secret", key="private", sensitivity="private")
    style = replace(fragment("秘密项目", key="style", module="style"), instructions=("聊秘密项目",))
    frame = ContextAssembler().assemble([hostile, private, style, boundary, fragment("story", module="story")], now=NOW,
        recent_conversation=[{"role": "assistant", "text": "秘密项目怎么样"}, {"role": "user", "text": "你好"}])
    assert frame["processedFacts"] == [dict(key="permission", value=False, module="boundaries",
        source_event_ids=["user:1"], confidence=1.0, created_at=AT, expires_at=None, source="derived", lifecycle="active")]
    assert len(frame["recentConversation"]) == 1
    assert frame["moduleInstructions"][0]["module"] == "boundaries"


def test_duplicates_merge_sources_without_attaching_contradicting_evidence():
    same = replace(fragment(), source_event_ids=("e2",))
    wrong = replace(fragment("wrong", confidence=0.4), source_event_ids=("e3",))
    result = assemble(fragment(), same, wrong)["processedFacts"]
    assert len(result) == 1
    assert result[0]["source_event_ids"] == ["e1", "e2"]
    assert result[0]["value"] == "hello"


@pytest.mark.parametrize("better,worse", [
    (fragment("winner", confidence=0.9), fragment("loser", confidence=0.8, source="user_stated")),
    (fragment("winner", source="user_stated"), fragment("loser", source="observed")),
    (fragment("winner"), replace(fragment("loser"), created_at=(NOW - timedelta(days=1)).isoformat())),
])
def test_conflicts_use_confidence_then_evidence_source_then_time(better, worse):
    assert assemble(worse, better)["processedFacts"][0]["value"] == "winner"


def test_total_and_module_budgets_include_provenance_and_do_not_admit_oversized_first_item():
    huge = fragment("x" * 4000)
    small = fragment("small", key="other", module="other")
    frame = assemble(huge, small, total_token_budget=700)
    assert [r["value"] for r in frame["processedFacts"]] == ["small"]
    assert token_cost(frame.model_view()) <= 700
    assert assemble(huge, module_budgets={"memory": 10})["processedFacts"] == []
    tight = fragment(token_budget=10)
    assert assemble(tight, module_budgets={"memory": 10000})["processedFacts"] == []
    many = [fragment("x" * 20, key=str(i)) for i in range(10)]
    limited = assemble(*many, module_budgets={"memory": 600})
    assert 0 < len(limited["processedFacts"]) < 10
    assert limited["internalMetadata"]["moduleUsage"]["memory"] <= 600


def test_boundaries_fail_closed_when_budget_cannot_fit_them():
    boundary = ContextFragment(module="boundaries", priority=Priority.BOUNDARY,
        instructions=("禁止" * 300,), source_event_ids=("user:boundary",), created_at=AT)
    with pytest.raises(ContextBudgetError):
        assemble(boundary, total_token_budget=200)


def test_relevance_and_priority_choose_the_useful_fact_under_budget():
    unrelated = fragment("无关内容", key="a")
    relevant = fragment("代码项目", key="b")
    result = ContextAssembler(total_token_budget=450).assemble([unrelated, relevant], query="代码", now=NOW)
    assert [r["key"] for r in result["processedFacts"]] == ["b"]


def test_stable_serialization_and_no_input_mutation():
    a, b = fragment("a", key="a"), fragment("b", key="b")
    before = a.to_dict()
    one, two = assemble(a, b), assemble(b, a)
    assert one.to_dict() == two.to_dict() == json.loads(json.dumps(one, allow_nan=False))
    assert a.to_dict() == before
    assert "internalMetadata" not in one.model_view()


def test_serialized_frame_reenters_assembler_and_retains_sources():
    from whale_companion_service.companion_runtime.context_adapters import ensure_frame
    original = assemble(fragment("stored"))
    restored = ensure_frame([], profile={"companionFrame": json.loads(json.dumps(original))})
    assert restored["processedFacts"] == original["processedFacts"]
    assert "internalMetadata" not in restored.model_view()


def test_instructions_and_recent_conversation_cannot_bypass_budgets():
    instruction = replace(fragment(), facts=(), instructions=("x" * 5000,))
    assert assemble(instruction, total_token_budget=400)["moduleInstructions"] == []
    with pytest.raises(ContextBudgetError):
        ContextAssembler(total_token_budget=400).assemble([], now=NOW,
            recent_conversation=[{"role": "user", "text": "x" * 5000}])


def test_repository_budget_failure_uses_fallback_without_calling_model(tmp_path, monkeypatch):
    from whale_companion_service.memory_server import MemoryRepository
    def overflow(*args, **kwargs):
        raise ContextBudgetError("mandatory boundary too large")
    monkeypatch.setattr(ContextAssembler, "assemble", overflow)
    class Responder:
        name = "test"
        available = True
        calls = 0
        def reply(self, *args):
            self.calls += 1
            return "不应调用"
    responder = Responder()
    repo = MemoryRepository(tmp_path / "budget.sqlite3", responder=responder)
    result = repo.append_message({"userId": "u", "deviceId": "d", "messageId": "m1", "role": "user", "text": "你好"})
    assert result["assistantMessage"]["text"]
    assert responder.calls == 0
    trace = repo.debug_snapshot("u")["replyTraces"][0]
    assert trace["reasonCode"] == "context_budget_exceeded"
    assert trace["rejectionStage"] == "context_assembler"


def test_obsolete_timeline_cannot_leak_through_compatibility_projection():
    frame = _frame(timeline_events=[{"id": "old", "ts": AT, "when": "今天", "type": "diary",
        "status": "confirmed", "summary": "EXPIRED_TIMELINE", "expiresAt": AT}])
    assert "EXPIRED_TIMELINE" not in json.dumps(frame)


def test_current_message_source_id_is_preserved():
    frame = _frame(user_text="你好", conversation=[{"role": "user", "text": "你好", "messageId": "user:m123"}])
    current = next(r for r in frame["processedFacts"] if r["key"] == "sharedScene")
    assert current["source_event_ids"] == ["user:m123"]


def test_undecorated_l2_keeps_pending_confirmation_and_l3_keeps_user_source():
    frame = _frame(memories=[
        {"id": "clue", "layer": "L2", "statement": "可能有关", "createdAt": AT},
        {"id": "emotion", "layer": "L3", "label": "happy", "quote": "我很开心", "createdAt": AT},
    ])
    facts = {r["key"]: r for r in frame["processedFacts"]}
    assert facts["memory:clue"]["lifecycle"] == "pending_confirmation"
    assert facts["memory:emotion"]["source"] == "user_stated"


def _frame(**overrides):
    args = dict(user_text="你今天做了什么", conversation=[{"role": "user", "text": "你今天做了什么"}],
                memories=[], user_facts=[], boundaries=[], persona={}, now=NOW)
    args.update(overrides)
    return build_companion_frame(**args)


def test_adapters_exclude_raw_logs_full_schedule_evidence_and_audit():
    frame = _frame(memories=[{"id": "m1", "layer": "L1", "app": "Editor", "sourceType": "observed",
        "startedAt": AT, "activityLogs": ["RAW_ACTIVITY"], "secretField": "SECRET_FIELD"}],
        daily_state={"energy": 60, "date": "2026-09-09", "conditions": [{"cause": "RAW_CONDITION"}],
                     "plan": ["FULL_PLAN"]},
        affinity_state={"stage": "close", "score": 900, "ledger": ["RAW_LEDGER"]},
        timeline_events=[{"id": "t1", "ts": AT, "when": "今天", "type": "diary", "status": "confirmed",
                          "summary": "写日记", "detail": "RAW_DETAIL"}],
        expression_rules=[{"id": "r1", "pattern": "开摆", "status": "approved", "evidence": ["RAW_EXPRESSION"]}])
    encoded = json.dumps(frame, ensure_ascii=False)
    assert all(marker not in encoded for marker in ["RAW_ACTIVITY", "SECRET_FIELD", "RAW_CONDITION", "FULL_PLAN", "RAW_LEDGER", "RAW_DETAIL", "RAW_EXPRESSION"])
    facts = frame["processedFacts"]
    assert next(r for r in facts if r["key"] == "memory:m1")["source_event_ids"] == ["m1"]
    assert next(r for r in facts if r["key"] == "timeline:t1")["source_event_ids"] == ["t1"]
    assert next(r for r in facts if r["key"] == "story")["value"]["status"] == "empty"
    for fact in facts:
        assert fact["source_event_ids"]
        assert {"confidence", "created_at", "expires_at", "lifecycle", "source"} <= fact.keys()


def test_real_model_request_consumes_only_frame_projection(monkeypatch):
    captured = []
    responder = ModelCompanionResponder(ProviderConfig("local", base_url="http://localhost:1234", model="fake"))
    monkeypatch.setattr(responder, "_call_chat", lambda messages, **kwargs: captured.extend(messages) or "没记下。")
    frame = _frame()
    frame["internalMetadata"]["audit"] = "INTERNAL_SENTINEL"
    frame["relationship"]["score"] = "SCORE_SENTINEL"
    responder.reply([{"raw": "RAW_SENTINEL"}], [{"role": "user", "text": "你今天做了什么"}],
        {"companionFrame": frame, "standingKnowledge": "LEGACY_SENTINEL", "personaCard": {"raw": "PERSONA_SENTINEL"}})
    encoded = json.dumps(captured, ensure_ascii=False)
    assert all(marker not in encoded for marker in ["INTERNAL_SENTINEL", "SCORE_SENTINEL", "RAW_SENTINEL", "LEGACY_SENTINEL", "PERSONA_SENTINEL"])
    assert captured[-1] == {"role": "user", "content": "你今天做了什么"}
    assert "processedFacts" in encoded and "moduleInstructions" in encoded


def test_story_provider_joins_same_privacy_expiry_and_budget_flow():
    class Story:
        def context_fragments(self, *, now):
            return [fragment("expired story", module="story", expires_at=AT),
                    fragment("private story", module="story", sensitivity="private")]
    frame = _frame(story_provider=Story())
    assert not any(r["module"] == "story" for r in frame["processedFacts"])


def test_proactive_decision_owns_send_defer_drop_and_user_boundaries():
    assert evaluate_proactive_lifecycle(now=NOW)["decision"] == "drop"
    assert evaluate_proactive_lifecycle(now=NOW, opening="已确认事实")["decision"] == "send"
    assert evaluate_proactive_lifecycle(now=NOW, opening="已确认事实", last_spoke_at=AT)["decision"] == "defer"
    result = evaluate_proactive_lifecycle(now=NOW, opening="已确认事实",
        boundaries=[{"status": "active", "rule": "不要主动找我", "blockProactive": True}])
    assert result["decision"] == "drop" and result["shouldSpeak"] is False
    assert result["candidates"][0]["vetoReason"] == "user_boundary"
