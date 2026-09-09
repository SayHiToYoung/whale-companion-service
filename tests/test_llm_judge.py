from __future__ import annotations

from datetime import datetime, timezone

from whale_companion_service.companion_llm import _extract_json_object
from whale_companion_service.companion_runtime.companion_emotion import (
    empty_companion_emotion,
    judge_companion_emotion,
)
from whale_companion_service.companion_runtime.affinity import judge_affinity_event
from whale_companion_service.companion_runtime.proactive import judge_proactive_decision
from whale_companion_service.companion_runtime.expression_learning import judge_expression_candidate
from whale_companion_service.companion_runtime.daily_life import judge_refinement

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _judge(result):
    return lambda task, context, fields, fallback: (result, "model")


def test_extract_json_object_plain():
    assert _extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_json_object_with_fence_and_noise():
    text = "好的，结果是：\n```json\n{\"mood\": \"有点开心\", \"valence\": 0.5}\n```\n希望有帮助"
    assert _extract_json_object(text)["mood"] == "有点开心"


def test_judge_emotion_uses_model_and_inertia():
    current = empty_companion_emotion()
    state, source = judge_companion_emotion(
        current, "今天好开心", "happy",
        _judge({"mood": "被她带动得很开心", "valence": 0.9, "arousal": 0.7}), now=NOW,
    )
    assert source == "model"
    assert state["mood"] == "被她带动得很开心"
    # 惰性：0.6 * 0.9 + 0.4 * 0.0 = 0.54
    assert 0.4 < state["valence"] < 0.9


def test_judge_emotion_fallback_without_judge():
    state, source = judge_companion_emotion(empty_companion_emotion(), "谢谢你真好", "", None, now=NOW)
    assert source == "fallback"
    assert state["mood"] == "被你这句话暖到了"


def test_judge_affinity_uses_model_delta():
    event, source = judge_affinity_event("随便聊聊", "familiar", _judge({"delta": 5, "reason": "聊得投机"}))
    assert source == "model"
    assert event["delta"] == 5
    assert event["kind"] == "warm"


def test_judge_affinity_fallback_regex():
    event, source = judge_affinity_event("谢谢你真好", "familiar", None)
    assert source == "fallback"
    assert event["kind"] == "warm"


def test_judge_affinity_zero_delta_means_none():
    event, source = judge_affinity_event("哦", "familiar", _judge({"delta": 0, "reason": ""}))
    assert event is None


def test_judge_proactive_speaks_or_defers():
    candidate = {"kind": "daily_greeting", "content": "早安", "score": 40}
    # 模型决定不开口
    decision, source = judge_proactive_decision([candidate], {}, _judge({"shouldSpeak": False, "kind": "", "content": ""}))
    assert source == "model"
    assert decision["shouldSpeak"] is False
    assert decision["reason"] == "llm_deferred"
    # 模型决定开口，用自己的话
    decision, source = judge_proactive_decision([candidate], {}, _judge({"shouldSpeak": True, "kind": "daily_greeting", "content": "早，昨晚睡得好吗"}))
    assert decision["shouldSpeak"] is True
    assert decision["content"] == "早，昨晚睡得好吗"


def test_judge_proactive_fallback_picks_highest():
    candidate = {"kind": "daily_greeting", "content": "早安", "score": 40}
    decision, source = judge_proactive_decision([candidate], {}, None)
    assert source == "fallback"
    assert decision["shouldSpeak"] is True
    assert decision["content"] == "早安"


def test_judge_expression_uses_model():
    candidates, source = judge_expression_candidate("这句话我今天说了十遍", _judge({"learnable": True, "pattern": "说了十遍", "scene": "casual", "instruction": "表达强调"}))
    assert source == "model"
    assert candidates[0]["pattern"] == "说了十遍"
    assert candidates[0]["status"] == "pending"


def test_judge_expression_rejects():
    candidates, source = judge_expression_candidate("我住在北京朝阳区", _judge({"learnable": False, "pattern": "", "scene": "casual", "instruction": ""}))
    assert candidates == []


def test_judge_refinement_uses_model():
    segment = {"start": "10:00", "end": "11:00", "activity": "上午的日常"}
    refined, source = judge_refinement(segment, {"energy": 80}, _judge({"summary": "在慢悠悠地过上午", "location": "", "todayEvent": "看到一个好笑的东西", "proactiveImpulse": "想跟你分享"}))
    assert source == "model"
    assert refined["summary"] == "在慢悠悠地过上午"
    assert refined["proactiveEvents"][0]["topic"] == "想跟你分享"


def test_judge_refinement_fallback_template():
    segment = {"start": "10:00", "end": "11:00", "activity": "上午的日常", "messageSeed": "上午有一搭没一搭地过着", "mood": "平稳"}
    refined, source = judge_refinement(segment, {}, None)
    assert source == "fallback"
    assert "上午" in refined["summary"]


class _FakeResponder:
    """一个可注入的 judge：judge_json 返回固定结果，reply 不可用。"""
    name = "fake"

    def __init__(self, judge_result):
        self._judge_result = judge_result
        self.judge_calls = 0

    @property
    def available(self):
        return True

    def reply(self, memories, conversation, profile_memory=None):
        raise RuntimeError("not used")

    def judge_json(self, *, task, context, fields, fallback):
        self.judge_calls += 1
        result = dict(self._judge_result)
        # 按 task 返回固定 delta
        if "亲近" in task or "疏远" in task:
            result = {"delta": 9, "reason": "fake warmth"}
        elif "说法" in task:
            result = {"learnable": False, "pattern": "", "scene": "casual", "instruction": ""}
        return result, "model"


def test_repository_keeps_state_deterministic_when_judge_available(tmp_path):
    from pathlib import Path as _P
    from whale_companion_service.memory_server import MemoryRepository
    repo = MemoryRepository(_P(tmp_path) / "llm.sqlite3", responder=_FakeResponder({}))
    repo.append_message({"userId": "u", "deviceId": "d", "messageId": "m1", "role": "user", "text": "随便聊聊"})
    # Phase one: an available judge cannot choose state changes or proactive sends.
    state = repo.affinity_state("u")
    assert state["score"] == 0.0
    assert state["interaction"] == "relaxed"
    payload = {"userId": "u", "deviceId": "d", "messageId": "m2", "role": "user", "text": "谢谢你真好"}
    repo.append_message(payload)
    repo.append_message(payload)
    assert repo.affinity_state("u")["score"] == 2.0
    repo.proactive_decision("u")
    assert repo.responder.judge_calls == 0
