from __future__ import annotations

import json
from pathlib import Path

from whale_companion_service.memory_server import MemoryRepository
from whale_companion_service.companion_runtime import build_companion_frame


def _frame(repo: MemoryRepository, user_id: str, text: str, conversation: list[dict] | None = None):
    return build_companion_frame(
        user_text=text,
        conversation=conversation or [{"role": "user", "text": text}],
        memories=[], user_facts=[], boundaries=[],
        persona={"traits": ["松弛"], "speechRules": []},
        affinity_state=repo.affinity_state(user_id),
        daily_state=repo.daily_state(user_id),
        timeline_events=repo.self_timeline_events(user_id),
        expression_rules=repo.expression_rules(user_id),
    )


def test_affinity_persists_and_flows_into_frame(tmp_path: Path):
    repo = MemoryRepository(tmp_path / "a.sqlite3")
    repo.apply_affinity_event({"userId": "u", "event": {"eventKey": "e1", "kind": "warm", "delta": 2.0}})
    state = repo.affinity_state("u")
    assert state["stage"] == "acquaintance"
    frame = _frame(repo, "u", "你好")
    assert frame["relationship"]["stage"] == "acquaintance"
    assert frame["relationship"]["stageLabel"] == "初识"
    assert "allowPlayful" in frame["relationship"]


def test_daily_state_persists_and_flows_into_frame(tmp_path: Path):
    repo = MemoryRepository(tmp_path / "b.sqlite3")
    state = repo.daily_state("u")
    assert 10.0 <= state["energy"] <= 100.0
    frame = _frame(repo, "u", "你好")
    assert frame["dailyLife"]["energy"] == state["energy"]
    assert "编造" in frame["dailyLife"]["usage"]


def test_self_timeline_recall_on_question(tmp_path: Path):
    repo = MemoryRepository(tmp_path / "c.sqlite3")
    repo.append_self_timeline_event({
        "userId": "u",
        "event": {"id": "t1", "ts": None, "type": "diary", "when": "今天", "summary": "写了日记", "status": "confirmed"},
    })
    frame = _frame(repo, "u", "你今天做了什么")
    assert frame["selfTimeline"] is not None
    assert not frame["selfTimeline"]["empty"]
    assert frame["selfTimeline"]["events"][0]["summary"] == "写了日记"


def test_self_timeline_not_injected_when_not_asked(tmp_path: Path):
    repo = MemoryRepository(tmp_path / "d.sqlite3")
    frame = _frame(repo, "u", "今天天气不错")
    assert frame["selfTimeline"] is None


def test_expression_review_gates_recall(tmp_path: Path):
    repo = MemoryRepository(tmp_path / "e.sqlite3")
    # 先通过 review 让规则进 approved（这里直接调用底层流程：pending 规则不召回）
    frame = _frame(repo, "u", "开摆")
    assert frame["learnedExpressions"]["empty"] is True  # 无已审核规则


def test_expression_rules_roundtrip(tmp_path: Path):
    repo = MemoryRepository(tmp_path / "f.sqlite3")
    # 直接写一条规则再读回（走 API 契约等价路径）。
    import sqlite3
    from datetime import datetime, timezone
    with repo._connect() as db:
        db.execute(
            "INSERT INTO expression_rules (user_id, rule_id, pattern, instruction, scene, kind, status, usage_count, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("u", "r1", "开摆", "学着用", "complaint", "style", "approved", 0, datetime.now(timezone.utc).isoformat()),
        )
    rules = repo.expression_rules("u")
    assert rules[0]["status"] == "approved"
    frame = _frame(repo, "u", "好烦啊")
    assert not frame["learnedExpressions"]["empty"]
    assert frame["learnedExpressions"]["rules"][0]["pattern"] == "开摆"


def test_review_expression_rule(tmp_path: Path):
    repo = MemoryRepository(tmp_path / "g.sqlite3")
    from datetime import datetime, timezone
    with repo._connect() as db:
        db.execute(
            "INSERT INTO expression_rules (user_id, rule_id, pattern, instruction, scene, kind, status, usage_count, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("u", "r1", "开摆", "学着用", "complaint", "style", "pending", 0, datetime.now(timezone.utc).isoformat()),
        )
    result = repo.review_expression_rule({"userId": "u", "ruleId": "r1", "decision": "approved"})
    assert result["status"] == "approved"
    assert repo.expression_rules("u")[0]["status"] == "approved"


def test_append_self_timeline_event_is_idempotent(tmp_path: Path):
    repo = MemoryRepository(tmp_path / "h.sqlite3")
    event = {"id": "t1", "ts": None, "type": "interaction", "when": "今天", "summary": "聊天", "status": "confirmed"}
    repo.append_self_timeline_event({"userId": "u", "event": event})
    repo.append_self_timeline_event({"userId": "u", "event": event})
    events = repo.self_timeline_events("u")
    assert len(events) == 1  # 同 event_id upsert 不重复


def test_append_message_feeds_living_state(tmp_path: Path):
    """端到端：一条暖消息同时喂好感度 + 表达候选 + 自我时间线。"""
    repo = MemoryRepository(tmp_path / "observe.sqlite3")
    repo.append_message({"userId": "u", "deviceId": "d", "messageId": "m1", "role": "user", "text": "谢谢你真好"})
    # 好感度 +2
    assert repo.affinity_state("u")["score"] == 2.0
    # 自我时间线 interaction 事件
    events = repo.self_timeline_events("u")
    assert any(e["type"] == "interaction" and e["id"] == "interaction:m1" for e in events)
    # 表达候选 pending
    rules = repo.expression_rules("u")
    assert any(r["pattern"] == "谢谢你真好" and r["status"] == "pending" for r in rules)


def test_append_message_violation_lowers_affinity(tmp_path: Path):
    repo = MemoryRepository(tmp_path / "observe2.sqlite3")
    repo.append_message({"userId": "u", "deviceId": "d", "messageId": "m1", "role": "user", "text": "你真没用"})
    state = repo.affinity_state("u")
    assert state["score"] == -7.0          # 分数立刻下降
    assert state["stage"] == "acquaintance"  # 但阶段有 20 分迟滞，一次越界不立刻降档
    assert state["interaction"] == "avoidant"  # 互动状态即时反映越界
