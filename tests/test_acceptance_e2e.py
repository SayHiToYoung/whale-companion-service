# -*- coding: utf-8 -*-
"""发布候选验收：架构不变量（目标 1–10）+ 12 个端到端陪伴场景。

这份文件是 docs/ACCEPTANCE.md 里那张矩阵的可执行版本。每个测试的名字对应矩阵里
的一行，断言的是「架构承诺」而不是某次实现的细节：原始事件不进模型、模块只能产候选、
组装器是唯一入口、事实带着来源活着、闸门链拦得住。

场景测试全部走仓库的真实读写路径，并用可注入时钟把时间推着走——
「第二天自然承接」这种事，不快进一天是验不出来的。
"""
from __future__ import annotations

import inspect
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from whale_companion_service import companion_runtime
from whale_companion_service.companion_runtime import build_companion_frame
from whale_companion_service.companion_runtime.context_assembler import ContextAssembler
from whale_companion_service.companion_runtime.context_contract import (
    ContextFragment,
    Priority,
)
from whale_companion_service.companion_runtime import daily_life, proactive, story
from whale_companion_service.companion_runtime.proactive import (
    apply_proactive_gates,
    build_proactive_candidates,
    evaluate_proactive_lifecycle,
)
from whale_companion_service.memory_server import MemoryRepository

NOW = datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc)
PERSONA = {"traits": ["松弛"], "speechRules": []}


class Clock:
    """可推进的时钟。仿真一整天不靠改系统时间。"""

    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> datetime:
        self.now += timedelta(**delta)
        return self.now


def repo_at(tmp_path: Path, name: str, clock: Clock) -> MemoryRepository:
    return MemoryRepository(tmp_path / f"{name}.sqlite3", clock=clock)


def say(repo: MemoryRepository, message_id: str, text: str, *, device: str = "desktop",
        role: str = "user") -> dict:
    return repo.append_message({
        "userId": "u", "deviceId": device, "messageId": message_id, "role": role, "text": text,
    })


def observed(memory_id: str, *, app: str, now: datetime, hours: float = 2.0, **extra) -> dict:
    """一条桌面观察，外加调用方想塞进来的任何原始字段。"""
    return {
        "id": memory_id, "layer": "L1", "sourceType": "observed", "kind": "activity",
        "app": app, "context": "work", "durationSeconds": int(hours * 3600),
        "startedAt": (now - timedelta(hours=hours)).isoformat(), "endedAt": now.isoformat(),
        **extra,
    }


def frame_for(text: str, *, memories=(), user_facts=(), boundaries=(), now: datetime = NOW,
              conversation=None, **extra):
    history = conversation or [{"role": "user", "text": text, "messageId": "msg-1"}]
    return build_companion_frame(
        user_text=text, conversation=history, memories=list(memories),
        user_facts=list(user_facts), boundaries=list(boundaries), persona=PERSONA,
        now=now, **extra)


def blob(frame) -> str:
    return json.dumps(frame.model_view(), ensure_ascii=False)


def care_permissions(quota: int = 5) -> dict:
    return {"allowProactiveCare": True, "allowSharedMemory": True, "proactiveQuota": quota}


# ===========================================================================
# 目标 1：原始事件只进存储和模块处理层，不直接进模型上下文
# ===========================================================================

RAW_NOISE = {
    "rawEvents": [{"window": "薪资谈判.docx", "keystrokes": 913}],
    "activityLogs": ["10:02 打开 1Password"],
    "audit": {"deviceSerial": "C02XYZ1234"},
    "screenshotPath": "/Users/u/Desktop/screen.png",
    "ledger": [1, 2, 3],
    "evidence": [{"messageId": "e1", "note": "内部评估"}],
}


def test_goal1_raw_event_payload_never_reaches_model_context():
    """原始活动样本、审计字段和客户端塞进来的任意 payload 都不进模型。"""
    memory = observed("m1", app="Visual Studio Code", now=NOW, project={"name": "小鲸"}, **RAW_NOISE)
    text = blob(frame_for("今天有点累", memories=[memory]))
    for leak in ("rawEvents", "activityLogs", "1Password", "C02XYZ1234", "screenshotPath",
                 "薪资谈判", "913", "ledger", "内部评估"):
        assert leak not in text, f"原始事件字段 {leak!r} 泄漏进了模型上下文"


def test_goal1_memory_is_projected_to_whitelisted_fields_only():
    memory = observed("m1", app="Visual Studio Code", now=NOW, project={"name": "小鲸"}, **RAW_NOISE)
    facts = [r for r in frame_for("在忙", memories=[memory]).model_view()["processedFacts"]
             if r["module"] == "memory"]
    assert len(facts) == 1
    assert set(facts[0]["value"]) <= {"app", "context", "durationSeconds", "projectName", "layer"}


def test_goal1_memory_layer_cannot_claim_stronger_evidence_than_its_source():
    """一条桌面观察自称 user_stated，整条丢弃——不许凭空升一级证据强度。"""
    forged = observed("m1", app="VS Code", now=NOW)
    forged["sourceType"] = "user_stated"
    facts = [r for r in frame_for("在忙", memories=[forged]).model_view()["processedFacts"]
             if r["module"] == "memory"]
    assert facts == []


def test_goal1_model_view_exposes_only_the_four_protocol_keys():
    view = frame_for("你好").model_view()
    assert set(view) == {"version", "recentConversation", "processedFacts", "moduleInstructions"}
    assert view["version"] == "companion-frame-v5"


def test_goal1_internal_scores_are_not_in_model_view():
    """内部评分（好感分、carryWeight、激活度）只用于排序，不给模型看。"""
    view = frame_for("你好", affinity_state={"score": 640.0, "trust": 0.8}).model_view()
    text = json.dumps(view, ensure_ascii=False)
    for private in ("carryWeight", "activation", "\"score\"", "knownFactCount", "userTurnCount"):
        assert private not in text


# ===========================================================================
# 目标 2：memory / daily life / relationship / story 通过统一 ContextFragment 输出
# ===========================================================================

def test_goal2_every_module_emits_context_fragments():
    from whale_companion_service.companion_runtime.context_adapters import collect_context_fragments

    fragments, _recent, _legacy = collect_context_fragments(
        user_text="你今天做了什么",
        conversation=[{"role": "user", "text": "你今天做了什么", "messageId": "msg-1"}],
        memories=[observed("m1", app="VS Code", now=NOW)],
        user_facts=[], boundaries=[], persona=PERSONA,
        daily_state={"date": NOW.date().isoformat(), "energy": 70.0, "conditions": []},
        story_provider=story.ArcStoryProvider([story.start_arc(
            story.story_templates()[0]["id"], now=NOW)]),
        now=NOW)
    assert fragments and all(isinstance(f, ContextFragment) for f in fragments)
    modules = {f.module for f in fragments}
    for required in ("memory", "dailyLife", "relationship", "story"):
        assert required in modules, f"{required} 没有以 ContextFragment 形式输出"


def test_goal2_fragment_producers_return_fragments_not_prompt_text():
    """模块产出的是结构化片段，不是拼好的提示词字符串。"""
    produced = (
        daily_life.daily_life_fragments(
            daily_life.advance_agenda(now=NOW, chronotype=None, daily_state=None),
            now=NOW, source="daily_state:test")
        + story.story_fragments(story.ArcStoryProvider([]), now=NOW)
    )
    assert produced
    for fragment in produced:
        assert isinstance(fragment, ContextFragment)
        assert not isinstance(fragment, str)


# ===========================================================================
# 目标 3：ContextAssembler 是唯一上下文组装入口
# ===========================================================================

_RUNTIME_DIR = Path(companion_runtime.__file__).parent
# context_contract.py 定义 CompanionFrame 本身；组装器造帧；另两个是它的入口。
_ASSEMBLY_ALLOWED = {"context_contract.py", "context_assembler.py", "orchestrator.py",
                     "context_adapters.py"}


def test_goal3_orchestrator_assembles_through_the_assembler():
    source = inspect.getsource(companion_runtime.orchestrator)
    assert ".assemble(" in source
    assert "ContextAssembler" in source


def test_goal3_no_module_builds_a_frame_outside_the_assembler():
    """除了组装器自己和它的两个入口，任何模块都不许直接造 CompanionFrame。"""
    offenders = []
    for path in sorted(_RUNTIME_DIR.glob("*.py")):
        if path.name in _ASSEMBLY_ALLOWED:
            continue
        if "CompanionFrame(" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert offenders == [], f"这些模块绕过组装器直接造帧：{offenders}"


def test_goal3_assembler_performs_no_state_writes_or_model_calls():
    source = inspect.getsource(companion_runtime.context_assembler)
    for forbidden in ("INSERT", "UPDATE ", "DELETE", "urllib", "requests", "_call_chat", "judge"):
        assert forbidden not in source, f"组装器里出现了 {forbidden!r}"


def test_goal3_assembler_output_is_deterministic():
    memories = [observed(f"m{i}", app=f"App{i}", now=NOW) for i in range(5)]
    first = blob(frame_for("在忙", memories=memories))
    second = blob(frame_for("在忙", memories=list(reversed(memories))))
    assert first == second, "同一批输入换个顺序就组装出不同上下文"


# ===========================================================================
# 目标 4：所有事实保留来源、置信度和生命周期
# ===========================================================================

_PROVENANCE = ("source_event_ids", "confidence", "created_at", "expires_at", "source", "lifecycle")


def test_goal4_every_processed_fact_carries_full_provenance():
    view = frame_for(
        "今天累", memories=[observed("m1", app="VS Code", now=NOW)],
        user_facts=[{"factId": "f1", "predicate": "name", "value": "阿哲", "status": "confirmed",
                     "confidence": 1.0, "sourceMessageId": "msg-f1", "updatedAt": NOW.isoformat()}],
    ).model_view()
    assert view["processedFacts"]
    for row in view["processedFacts"]:
        for field in _PROVENANCE:
            assert field in row, f"{row['key']} 缺少 {field}"
        assert row["source_event_ids"], f"{row['key']} 没有来源"
        assert 0.0 <= row["confidence"] <= 1.0
        assert row["lifecycle"]


def test_goal4_unattributed_facts_never_reach_the_model():
    orphan = ContextFragment(module="memory", priority=Priority.MEMORY,
                             facts=(companion_runtime.context_contract.ContextFact("ghost", {"a": 1}),),
                             source_event_ids=(), created_at=NOW.isoformat())
    frame = ContextAssembler().assemble([orphan], now=NOW)
    assert frame["processedFacts"] == []


def test_goal4_expired_facts_are_dropped_at_assembly_time():
    stale = ContextFragment(module="memory", priority=Priority.MEMORY,
                            facts=(companion_runtime.context_contract.ContextFact("old", {"a": 1}),),
                            source_event_ids=("e1",), created_at=(NOW - timedelta(days=2)).isoformat(),
                            expires_at=(NOW - timedelta(hours=1)).isoformat())
    assert ContextAssembler().assemble([stale], now=NOW)["processedFacts"] == []


def test_goal4_non_speakable_lifecycle_is_excluded():
    """用户说过「别再提」的记忆不会因为轮询而重新出现。"""
    memory = observed("m1", app="健身房", now=NOW)
    memory["lifecycle"] = {"status": "suppressed", "storedStatus": "suppressed"}
    facts = [r for r in frame_for("在忙", memories=[memory]).model_view()["processedFacts"]
             if r["module"] == "memory"]
    assert facts == []


def test_goal4_conflicting_evidence_is_not_merged_into_the_winner():
    fact = companion_runtime.context_contract.ContextFact
    weak = ContextFragment(module="memory", priority=Priority.MEMORY,
                           facts=(fact("k", "旧值", source="derived"),), source_event_ids=("e-old",),
                           confidence=0.4, created_at=NOW.isoformat())
    strong = ContextFragment(module="memory", priority=Priority.MEMORY,
                             facts=(fact("k", "新值", source="user_stated"),), source_event_ids=("e-new",),
                             confidence=0.95, created_at=NOW.isoformat())
    rows = ContextAssembler().assemble([weak, strong], now=NOW)["processedFacts"]
    assert len(rows) == 1
    assert rows[0]["value"] == "新值"
    assert rows[0]["source_event_ids"] == ["e-new"], "矛盾证据被并进了胜出事实"


# ===========================================================================
# 目标 5：用户边界、事实正确性和当前场景拥有最高优先级
# ===========================================================================

def test_goal5_boundary_outranks_every_other_priority():
    assert Priority.BOUNDARY > Priority.CURRENT > Priority.OPEN_THREAD > Priority.MEMORY
    assert Priority.MEMORY > Priority.RELATIONSHIP > Priority.DAILY_LIFE > Priority.STORY


def test_goal5_boundary_blocks_the_suppressed_topic_from_memory():
    memory = observed("m9", app="健身房打卡", now=NOW, context="life")
    boundary = {"boundaryId": "b1", "kind": "topic_suppression", "rule": "不要主动提起“健身房”。",
                "status": "active", "sourceMessageId": "msg-b1", "createdAt": NOW.isoformat()}
    view = frame_for("在忙", memories=[memory], boundaries=[boundary]).model_view()
    assert [r for r in view["processedFacts"] if r["module"] == "memory"] == []
    assert any("健身房" in r["text"] for r in view["moduleInstructions"] if r["module"] == "boundaries")


def test_goal5_identity_correction_blocks_the_corrected_frame_of_reference():
    clue = {"id": "m10", "layer": "L2", "sourceType": "derived", "kind": "clue",
            "statement": "用户提到寒暑假安排", "startedAt": NOW.isoformat(), "endedAt": NOW.isoformat()}
    boundary = {"boundaryId": "b2", "kind": "identity_correction", "status": "active",
                "rule": "不得把用户当作学生，也不要询问早课、考试、作业、学校或寒暑假安排。",
                "sourceMessageId": "msg-b2", "createdAt": NOW.isoformat()}
    view = frame_for("今天累", memories=[clue], boundaries=[boundary]).model_view()
    assert [r for r in view["processedFacts"] if r["module"] == "memory"] == []


def test_goal5_a_blocked_module_cannot_block_the_boundary_that_constrains_it():
    """被约束的模块不能反过来把约束它的控制模块挡掉。"""
    fact = companion_runtime.context_contract.ContextFact
    rogue = ContextFragment(module="memory", priority=Priority.MEMORY,
                            facts=(fact("k", "v"),), source_event_ids=("e1",),
                            created_at=NOW.isoformat(),
                            metadata={"blocked_modules": ["boundaries"], "blocked_terms": ["安静"]})
    guard = ContextFragment(module="boundaries", priority=Priority.BOUNDARY,
                            instructions=("除非用户先开口，否则保持安静。",),
                            source_event_ids=("b1",), created_at=NOW.isoformat())
    frame = ContextAssembler().assemble([rogue, guard], now=NOW)
    assert any(r["module"] == "boundaries" for r in frame["moduleInstructions"])


def test_goal5_current_utterance_is_mandatory_context():
    view = frame_for("我现在很急，这个 bug 上线前必须修完").model_view()
    assert view["recentConversation"][-1]["role"] == "user"
    assert "bug" in view["recentConversation"][-1]["content"]


def test_goal5_oversized_mandatory_context_blocks_generation_instead_of_dropping_the_boundary():
    """装不下时宁可不生成，也不悄悄把边界丢掉。"""
    from whale_companion_service.companion_runtime.context_adapters import build_reply_frame

    frame = build_reply_frame(
        user_text="你好" * 2000,
        conversation=[{"role": "user", "text": "你好" * 2000, "messageId": "m1"}],
        memories=[], user_facts=[], boundaries=[], persona=PERSONA, now=NOW,
        assembler=ContextAssembler(total_token_budget=200))
    assert frame.generation_blocked


# ===========================================================================
# 目标 6：日程和故事只能产生主动候选，不能直接发送消息
# ===========================================================================

_SEND_WORDS = re.compile(r"\b(send_message|post_message|deliver_message|append_message|commit_proactive)\b")


def test_goal6_agenda_and_story_have_no_send_path():
    for module in (daily_life, story):
        source = inspect.getsource(module)
        assert not _SEND_WORDS.search(source), f"{module.__name__} 里出现了发送调用"
        assert "urllib" not in source and "sqlite3" not in source


def test_goal6_agenda_signals_are_candidates_with_windows():
    state = daily_life.advance_agenda(now=NOW, chronotype=None, daily_state=None)
    signals = daily_life.agenda_candidate_signals({**state, "conditions": []}, now=NOW)
    for signal in signals:
        assert signal["kind"] in proactive.CANDIDATE_BASE_SCORE
        assert signal["signature"]
        assert signal["expiresAt"], "日程候选没有有效期，等于可以永远排队"
        assert "shouldSpeak" not in signal


def test_goal6_story_signals_are_candidates_only():
    arc = story.start_arc(story.story_templates()[0]["id"], now=NOW)
    for signal in story.story_candidate_signals([arc], now=NOW):
        assert signal["kind"] == "story_beat"
        assert signal["expiresAt"]
        assert "shouldSpeak" not in signal


def test_goal6_frame_tells_the_model_modules_may_not_send():
    policy = next(r for r in frame_for("你好").model_view()["processedFacts"]
                  if r["key"] == "proactivePolicy")
    assert policy["value"]["modulesMaySend"] is False
    assert policy["value"]["decisionOwner"] == "proactive"


def test_goal6_agenda_candidate_still_passes_the_full_gate_chain():
    """日程候选和别的候选走同一条链——深夜一样被拦。"""
    state = daily_life.advance_agenda(now=NOW, chronotype=None, daily_state=None)
    signals = daily_life.agenda_candidate_signals({**state, "conditions": []}, now=NOW)
    result = evaluate_proactive_lifecycle(
        now=NOW, signals=signals, period="late_night", relationship_stage="close",
        permissions=care_permissions())
    assert result["shouldSpeak"] is False
    assert result["vetoReason"] == "quiet_hours"


# ===========================================================================
# 目标 7：主动候选全部经过频率、免打扰、未回应、关系权限、过期和重复检查
# ===========================================================================

GATE_CASES = [
    ("too_soon", dict(since_spoke_minutes=1.0)),
    ("quiet_hours", dict(period="late_night")),
    ("daily_cap", dict(daily_sent=8, daily_limit=8)),
    ("no_response_pause", dict(no_response_streak=proactive.NO_RESPONSE_PAUSE_AFTER)),
    ("relationship_boundary", dict(relationship_stage="distant")),
    ("interaction_converged", dict(interaction_state="hurt")),
    ("user_still_chatting", dict(activity="active", silence_minutes=0.5)),
    ("user_resting", dict(rest_until=(NOW + timedelta(minutes=30)).isoformat())),
    ("token_hard_limit", dict(token_remaining=0.0)),
    ("care_not_permitted", dict(permissions={"allowProactiveCare": False, "proactiveQuota": 0,
                                             "allowSharedMemory": True})),
    ("care_quota_used", dict(permissions=care_permissions(1), care_sent=1)),
]


@pytest.mark.parametrize("expected,kwargs", GATE_CASES, ids=[c[0] for c in GATE_CASES])
def test_goal7_each_gate_vetoes_with_its_own_reason(expected, kwargs):
    candidates = build_proactive_candidates(daily_greeting="早安")
    for candidate in candidates:
        candidate["score"] = 100.0
    options = dict(relationship_stage="close", permissions=care_permissions(), now=NOW)
    options.update(kwargs)
    gated = apply_proactive_gates(candidates, **options)
    assert gated[0]["vetoReason"] == expected


def test_goal7_low_value_candidate_is_dropped_not_queued():
    candidates = build_proactive_candidates(daily_greeting="早安")
    for candidate in candidates:
        candidate["score"] = 1.0
    gated = apply_proactive_gates(candidates, relationship_stage="close",
                                  permissions=care_permissions(), now=NOW)
    assert gated[0]["vetoReason"] == "low_value"


def test_goal7_expired_window_is_dropped_and_future_window_is_deferred():
    result = evaluate_proactive_lifecycle(
        now=NOW, relationship_stage="close", permissions=care_permissions(),
        signals=[
            {"kind": "agenda_share", "content": "已经过去的事", "signature": "past",
             "expiresAt": (NOW - timedelta(minutes=1)).isoformat()},
            {"kind": "agenda_share", "content": "还没到的事", "signature": "future",
             "windowStart": (NOW + timedelta(hours=1)).isoformat(),
             "expiresAt": (NOW + timedelta(hours=2)).isoformat()},
        ])
    by_signature = {c["signature"]: c for c in result["candidates"]}
    assert by_signature["past"]["decision"] == "drop"
    assert by_signature["future"]["decision"] == "defer"


def test_goal7_sent_candidate_never_returns_to_the_queue():
    fresh = build_proactive_candidates(daily_greeting="早安")
    stored = [{**fresh[0], "status": "sent", "createdAt": NOW.isoformat()}]
    assert proactive.reconcile_candidates(fresh, stored, now=NOW) == []


def test_goal7_deferred_candidate_keeps_its_original_age():
    """重算不许把年龄清零，否则一条候选可以永远不老。"""
    fresh = build_proactive_candidates(daily_greeting="早安")
    born = (NOW - timedelta(minutes=90)).isoformat()
    stored = [{**fresh[0], "status": "deferred", "createdAt": born}]
    queued = proactive.reconcile_candidates(fresh, stored, now=NOW)
    assert queued[0]["createdAt"] == born
    assert queued[0]["ageMinutes"] == pytest.approx(90.0, abs=0.1)


def test_goal7_user_boundary_outranks_relationship_permission():
    result = evaluate_proactive_lifecycle(
        now=NOW, relationship_stage="deeply_bonded", permissions=care_permissions(),
        daily_greeting="早安",
        boundaries=[{"kind": "contact", "status": "active", "blockProactive": True,
                     "rule": "不要主动联系用户；除非用户先开口，否则保持安静。"}])
    assert result["shouldSpeak"] is False
    assert result["candidates"][0]["vetoReason"] == "user_boundary"


def test_goal7_gate_order_is_stable_and_audited():
    result = evaluate_proactive_lifecycle(
        now=NOW, relationship_stage="close", permissions=care_permissions(),
        daily_greeting="早安", period="late_night", daily_sent=99, daily_limit=8)
    assert result["candidates"][0]["vetoReason"] == "quiet_hours"
    assert result["candidates"][0]["checkedAt"] == NOW.isoformat()


# ===========================================================================
# 目标 8：服务重启、重复事件和跨端访问不会破坏幂等性
# ===========================================================================

def test_goal8_duplicate_message_id_is_idempotent(tmp_path: Path):
    repo = repo_at(tmp_path, "idem", Clock())
    say(repo, "m1", "我今天面试完了")
    before = len(repo.messages("u")["messages"])
    again = say(repo, "m1", "我今天面试完了", device="mobile")
    assert again["duplicate"] is True
    assert len(repo.messages("u")["messages"]) == before


def test_goal8_proactive_delivery_replay_returns_the_same_result(tmp_path: Path):
    clock = Clock()
    repo = repo_at(tmp_path, "replay", clock)
    repo.adjust_affinity({"userId": "u", "score": 700.0})
    first = repo.commit_proactive_decision({"userId": "u", "deliveryId": "D1"})
    clock.advance(hours=3)
    second = repo.commit_proactive_decision({"userId": "u", "deliveryId": "D1"})
    assert first == second, "同一个 deliveryId 重放拿到了不同结果"


def test_goal8_state_survives_a_restart(tmp_path: Path):
    clock = Clock()
    path = tmp_path / "restart.sqlite3"
    repo = MemoryRepository(path, clock=clock)
    repo.adjust_affinity({"userId": "u", "score": 700.0})
    sent = repo.commit_proactive_decision({"userId": "u", "deliveryId": "D1"})
    pressure = repo.proactive_decision("u")["pressure"]
    del repo

    revived = MemoryRepository(path, clock=clock)
    assert revived.proactive_decision("u")["pressure"] == pressure
    assert revived.commit_proactive_decision({"userId": "u", "deliveryId": "D1"}) == sent


def test_goal8_a_sent_candidate_is_not_repeated_after_restart(tmp_path: Path):
    clock = Clock()
    path = tmp_path / "norepeat.sqlite3"
    repo = MemoryRepository(path, clock=clock)
    repo.adjust_affinity({"userId": "u", "score": 700.0})
    first = repo.commit_proactive_decision({"userId": "u", "deliveryId": "D1"})
    signature = (first.get("selected") or {}).get("signature")
    del repo

    revived = MemoryRepository(path, clock=clock)
    clock.advance(hours=4)
    second = revived.commit_proactive_decision({"userId": "u", "deliveryId": "D2"})
    assert (second.get("selected") or {}).get("signature") != signature


def test_goal8_opening_claim_is_atomic_across_devices(tmp_path: Path):
    repo = repo_at(tmp_path, "claim", Clock())
    say(repo, "m1", "我今天写了很久代码")
    desktop = repo.claim_opening({"userId": "u", "deviceId": "desktop", "claimId": "c1"})
    mobile = repo.claim_opening({"userId": "u", "deviceId": "mobile", "claimId": "c1"})
    assert desktop == mobile


def test_goal8_read_only_polling_does_not_mutate_state(tmp_path: Path):
    repo = repo_at(tmp_path, "poll", Clock())
    repo.adjust_affinity({"userId": "u", "score": 700.0})
    before = repo.proactive_decision("u")["pressure"]
    for _ in range(5):
        repo.proactive_decision("u")
    assert repo.proactive_decision("u")["pressure"] == before


# ===========================================================================
# 目标 9：旧数据和现有 API 兼容
# ===========================================================================

_LEGACY_FRAME_KEYS = ("sharedScene", "relationship", "scene", "emotion", "openThreads",
                      "innerReaction", "turnDecision", "speechStyle", "safety", "dailyLife")


def test_goal9_legacy_frame_projections_remain_available():
    frame = frame_for("今天累")
    for key in _LEGACY_FRAME_KEYS:
        assert key in frame, f"旧调用方读的 {key} 不见了"


def test_goal9_legacy_projections_are_not_part_of_model_input():
    view = frame_for("今天累").model_view()
    for key in _LEGACY_FRAME_KEYS:
        assert key not in view


def test_goal9_memory_row_without_lifecycle_still_loads():
    """旧库里的记忆没有 lifecycle 字段，不能因此炸掉。"""
    legacy = observed("old-1", app="VS Code", now=NOW)
    legacy.pop("sourceType")
    rows = [r for r in frame_for("在忙", memories=[legacy]).model_view()["processedFacts"]
            if r["module"] == "memory"]
    assert len(rows) == 1
    assert rows[0]["lifecycle"] == "active"


def test_goal9_ensure_frame_accepts_the_legacy_signature():
    from whale_companion_service.companion_runtime.context_adapters import ensure_frame

    frame = ensure_frame([observed("m1", app="VS Code", now=NOW)],
                         [{"role": "user", "text": "在忙"}], {})
    assert frame["version"] == "companion-frame-v5"


def test_goal9_persisted_frame_re_enters_arbitration():
    from whale_companion_service.companion_runtime.context_adapters import ensure_frame

    original = frame_for("今天累", memories=[observed("m1", app="VS Code", now=NOW)])
    replayed = ensure_frame([], profile={"companionFrame": original.to_dict()})
    assert replayed["version"] == "companion-frame-v5"
    for row in replayed.model_view()["processedFacts"]:
        assert row["source_event_ids"]


def test_goal9_legacy_repository_schema_upgrades_in_place(tmp_path: Path):
    clock = Clock()
    path = tmp_path / "upgrade.sqlite3"
    repo = MemoryRepository(path, clock=clock)
    say(repo, "m1", "我今天面试完了")
    del repo
    revived = MemoryRepository(path, clock=clock)
    assert [r["messageId"] for r in revived.messages("u")["messages"]][0] == "m1"


# ===========================================================================
# 目标 10：LLM 只负责理解和表达，不负责修改内部状态
# ===========================================================================

def test_goal10_system_prompt_states_the_llm_owns_no_state():
    from whale_companion_service.companion_llm import SYSTEM_PROMPT

    assert "内部评分、状态变更和主动发送不由你决定" in SYSTEM_PROMPT
    assert "你只负责理解和表达" in SYSTEM_PROMPT


def test_goal10_responder_module_performs_no_persistence():
    import whale_companion_service.companion_llm as llm

    source = inspect.getsource(llm)
    for forbidden in ("sqlite3", "INSERT INTO", "UPDATE ", "DELETE FROM", "MemoryRepository"):
        assert forbidden not in source, f"模型层里出现了 {forbidden!r}"


def test_goal10_judge_failure_falls_back_deterministically():
    def exploding_judge(**_kwargs):
        raise RuntimeError("model down")

    candidates = [{"kind": "follow_up", "content": "问问他面试", "score": 90.0}]
    decision, source = proactive.judge_proactive_decision(candidates, {}, exploding_judge)
    assert source == "fallback"
    assert decision == {"shouldSpeak": True, "kind": "follow_up", "content": "问问他面试"}


def test_goal10_judge_cannot_invent_a_candidate_of_its_own():
    """模型可以在放行的候选里选，也可以说不想开口，但不能凭空造一个来源。"""
    def inventive_judge(**kwargs):
        return {"shouldSpeak": True, "kind": "invented", "content": "我偷偷查了你的日历"}, "model"

    decision, _source = proactive.judge_proactive_decision([], {}, inventive_judge)
    assert decision["shouldSpeak"] is False
    assert decision["reason"] == "nothing_grounded"


def test_goal10_judge_may_only_decline_or_rephrase_within_gated_candidates():
    seen = {}

    def recording_judge(**kwargs):
        seen.update(kwargs)
        return {"shouldSpeak": False}, "model"

    candidates = [{"kind": "follow_up", "content": "问问他面试", "score": 90.0}]
    decision, source = proactive.judge_proactive_decision(candidates, {"period": "daytime"}, recording_judge)
    assert decision == {"shouldSpeak": False, "reason": "llm_deferred"}
    assert [c["kind"] for c in seen["context"]["candidates"]] == ["follow_up"]


def test_goal10_no_judge_is_wired_into_the_production_path():
    """判断层是休眠的辅助接口：生产路径上模型只生成回复正文，不参与任何状态变更。

    这条不变量比提示词更硬——提示词说「不由你决定」，这里验证的是它**够不着**。
    要接入某个 judge_* 时，请连同 docs/COMPANION-POLICIES.md 一起改，
    并说明确定性安全网怎么继续兜住它。
    """
    import whale_companion_service.memory_server as server

    source = inspect.getsource(server)
    for judge_name in ("judge_companion_emotion", "judge_affinity_event", "judge_proactive_decision",
                       "judge_expression_candidate", "judge_refinement"):
        assert judge_name not in source, f"{judge_name} 被接进了生产路径，安全网需要重新审计"


def test_goal10_relationship_score_moves_only_through_the_deterministic_ledger(tmp_path: Path):
    clock = Clock(NOW)
    repo = repo_at(tmp_path, "ledger", clock)
    before = repo.affinity_state("u")["score"]
    say(repo, "m1", "谢谢你，今天多亏有你")
    after = repo.affinity_state("u")["score"]
    assert after != before
    # 单日正向增量受账本上限约束，模型给不出也拿不走这个数。
    assert after - before <= 12.0


def test_goal10_ungrounded_model_reply_is_rejected():
    from whale_companion_service.companion_llm import model_reply_is_grounded

    conversation = [{"role": "user", "text": "在忙"}]
    assert not model_reply_is_grounded("你今天一定很累吧", [], conversation)
    assert not model_reply_is_grounded("你刚刚开了三个小时会", [], conversation)
    assert model_reply_is_grounded("嗯，那就先忙。", [], conversation)


# ===========================================================================
# 端到端场景（12 个）
# ===========================================================================

def test_scenario_long_work_then_care(tmp_path: Path):
    """场景 1：长时间工作后的关心——关心必须落在观察到的事实上。"""
    clock = Clock(NOW)
    repo = repo_at(tmp_path, "s1", clock)
    repo.adjust_affinity({"userId": "u", "score": 700.0})
    long_session = observed("w1", app="Visual Studio Code", now=clock.now, hours=4.0,
                            project={"name": "小鲸"}, **RAW_NOISE)
    view = frame_for("在忙", memories=[long_session], now=clock.now).model_view()
    memory = next(r for r in view["processedFacts"] if r["module"] == "memory")
    assert memory["value"]["durationSeconds"] == 4 * 3600
    assert memory["source"] == "observed"
    assert "w1" in memory["source_event_ids"]
    assert "薪资谈判" not in json.dumps(view, ensure_ascii=False)


def test_scenario_do_not_disturb_during_meeting(tmp_path: Path):
    """场景 2：会议期间免打扰——一条也不许漏出去。"""
    meeting_ends = NOW + timedelta(minutes=45)
    result = evaluate_proactive_lifecycle(
        now=NOW, rest_until=meeting_ends.isoformat(), relationship_stage="close",
        permissions=care_permissions(), daily_greeting="早安", meal_care="记得吃饭",
        mood_checkin="还好吗", opening="你今天写了 2 小时代码",
        pending_follow_ups=[{"hint": "问问他面试", "weight": 0.9}])
    assert result["shouldSpeak"] is False
    assert {c["vetoReason"] for c in result["candidates"]} == {"user_resting"}
    # 会议结束后，同一批候选还在队列里等着
    after = evaluate_proactive_lifecycle(
        now=meeting_ends + timedelta(minutes=1), rest_until=meeting_ends.isoformat(),
        relationship_stage="close", permissions=care_permissions(),
        pending_follow_ups=[{"hint": "问问他面试", "weight": 0.9}])
    assert after["shouldSpeak"] is True


def test_scenario_late_night_activity(tmp_path: Path):
    """场景 3：深夜活动——默认闭嘴，只有用户亲口交代的约定例外。"""
    quiet = evaluate_proactive_lifecycle(
        now=NOW, period="late_night", relationship_stage="close", permissions=care_permissions(),
        opening="你还在写代码", mood_checkin="还好吗")
    assert quiet["shouldSpeak"] is False and quiet["vetoReason"] == "quiet_hours"

    promised = evaluate_proactive_lifecycle(
        now=NOW, period="late_night", relationship_stage="close", permissions=care_permissions(),
        pending_follow_ups=[{"hint": "半夜提醒我睡觉", "weight": 0.9}])
    assert promised["shouldSpeak"] is True
    assert promised["selected"]["kind"] == "follow_up"


def test_scenario_user_states_fatigue(tmp_path: Path):
    """场景 4：用户明确表达疲惫——情绪成为可断言事实，并开一条线程。"""
    clock = Clock(NOW)
    repo = repo_at(tmp_path, "s4", clock)
    say(repo, "m1", "我好累啊，这周太赶了")
    view = frame_for("我好累啊，这周太赶了", now=clock.now).model_view()
    emotion = next(r for r in view["processedFacts"] if r["key"] == "emotion")
    assert emotion["value"]["userEmotion"] != "unknown"
    assert emotion["value"]["mayStateAsFact"] is True
    assert emotion["source"] == "user_stated"


def test_scenario_next_day_natural_carry(tmp_path: Path):
    """场景 5：第二天自然承接——旧情绪只能当背景，不能当此刻的状态。"""
    clock = Clock(NOW)
    repo = repo_at(tmp_path, "s5", clock)
    say(repo, "m1", "我好累啊，这周太赶了")
    clock.advance(days=1)
    say(repo, "m2", "早")

    threads = repo.profile_memories("u")  # 触发一次读路径，确认不炸
    assert threads["userId"] == "u"
    with repo._connect() as db:
        open_rows = repo._open_threads_locked(db, "u")
    from whale_companion_service.companion_runtime.open_threads import build_open_threads

    state = build_open_threads(open_rows, now=clock.now)
    if state["threads"]:
        view = frame_for("早", now=clock.now, threads=open_rows,
                         conversation=[{"role": "user", "text": "早", "messageId": "m2"}]).model_view()
        carried = [r for r in view["processedFacts"] if r["module"] == "openThreads"]
        for row in carried:
            assert row["confidence"] <= 0.49, "隔夜的情绪被当成了高置信度的当前状态"
        emotion = next(r for r in view["processedFacts"] if r["key"] == "emotion")
        assert emotion["value"]["carriedEmotion"] == "", "旧情绪泄漏成了此刻的情绪"


def test_scenario_user_says_it_improved(tmp_path: Path):
    """场景 6：用户表示已经好转——线程闭合，记忆不再可说。"""
    from whale_companion_service.memory_lifecycle import lifecycle_transition_from_text

    status, reason = lifecycle_transition_from_text("已经好多了，昨天那事解决了")
    assert status == "resolved"
    assert reason == "user_said_it_improved_or_ended"

    resolved = observed("m1", app="VS Code", now=NOW)
    resolved["lifecycle"] = {"status": "resolved", "storedStatus": "resolved"}
    rows = [r for r in frame_for("在忙", memories=[resolved]).model_view()["processedFacts"]
            if r["module"] == "memory"]
    assert rows == []


def test_scenario_user_asks_never_to_mention_again(tmp_path: Path):
    """场景 7：用户要求以后别再提——边界写进库，话题从上下文和主动链路一起消失。"""
    clock = Clock(NOW)
    repo = repo_at(tmp_path, "s7", clock)
    say(repo, "m1", "以后别再提健身房了")
    boundaries = repo.profile_memories("u")["boundaries"]
    suppression = next(b for b in boundaries if b["kind"] == "topic_suppression")
    assert "健身房" in suppression["rule"]
    assert suppression["sourceMessageId"] == "m1"

    memory = observed("g1", app="健身房打卡", now=clock.now, context="life")
    view = frame_for("在忙", memories=[memory], boundaries=boundaries, now=clock.now).model_view()
    assert [r for r in view["processedFacts"] if r["module"] == "memory"] == []

    blocked = evaluate_proactive_lifecycle(
        now=clock.now, relationship_stage="close", permissions=care_permissions(),
        boundaries=boundaries, signals=[{"kind": "memory_echo", "content": "你上次去健身房",
                                         "signature": "echo:gym"}])
    assert blocked["candidates"][0]["vetoReason"] == "user_boundary"


def test_scenario_suppression_term_ignores_sentence_particles():
    """回归：「别再提健身房了」屏蔽的是「健身房」，不是「健身房了」。

    带着语气词的词条永远匹配不上真正的记忆内容，边界会静默失效——
    用户以为自己划了线，实际上什么都没挡住。
    """
    from whale_companion_service.profile_memory import extract_profile_memories

    for said, expected in (("以后别再提健身房了", "健身房"), ("别再提那件事了", "那件事"),
                           ("别再提健身房", "健身房"), ("别叫我宝宝了", "宝宝")):
        rules = [b["rule"] for b in extract_profile_memories(said)["boundaries"]]
        assert any(f"“{expected}”" in rule for rule in rules), f"{said!r} 抽出了 {rules}"


def test_scenario_repeated_no_response(tmp_path: Path):
    """场景 8：连续不回复——说到第三次没人理就停下。"""
    for streak in range(proactive.NO_RESPONSE_PAUSE_AFTER):
        allowed = evaluate_proactive_lifecycle(
            now=NOW, relationship_stage="close", permissions=care_permissions(),
            no_response_streak=streak, daily_greeting="早安")
        assert allowed["shouldSpeak"] is True, f"第 {streak + 1} 次就不该停"
    paused = evaluate_proactive_lifecycle(
        now=NOW, relationship_stage="close", permissions=care_permissions(),
        no_response_streak=proactive.NO_RESPONSE_PAUSE_AFTER, daily_greeting="早安")
    assert paused["shouldSpeak"] is False
    assert paused["vetoReason"] == "no_response_pause"


def test_scenario_no_response_streak_resets_after_a_reply(tmp_path: Path):
    clock = Clock(NOW)
    repo = repo_at(tmp_path, "s8b", clock)
    repo.adjust_affinity({"userId": "u", "score": 700.0})
    repo.commit_proactive_decision({"userId": "u", "deliveryId": "D1"})
    assert repo.proactive_decision("u")["pressure"]["noResponseStreak"] >= 1
    clock.advance(minutes=5)
    say(repo, "reply-1", "在呢")
    assert repo.proactive_decision("u")["pressure"]["noResponseStreak"] == 0


def test_scenario_user_corrects_a_fact(tmp_path: Path):
    """场景 9：用户纠正事实——旧事实转 corrected，纠正带着证据留痕。"""
    clock = Clock(NOW)
    repo = repo_at(tmp_path, "s9", clock)
    say(repo, "m1", "我叫小南")
    clock.advance(minutes=5)
    say(repo, "m2", "我叫阿哲")

    facts = repo.profile_memories("u")["userFacts"]
    names = {row["value"]: row["status"] for row in facts if row["predicate"] == "name"}
    assert names.get("阿哲") == "confirmed"
    assert names.get("小南") == "corrected", "被纠正的旧事实仍然是 confirmed"
    # 纠正本身留痕：旧事实上挂着这次矛盾的证据。
    old = next(r for r in facts if r["value"] == "小南")
    assert any(e["type"] == "contradiction" and e["messageId"] == "m2" for e in old["evidence"])

    confirmed = [r for r in facts if r["status"] == "confirmed" and r["predicate"] == "name"]
    view = frame_for("你还记得我叫什么吗", user_facts=confirmed, now=clock.now).model_view()
    name_fact = next(r for r in view["processedFacts"] if r["key"] == "user:name")
    assert name_fact["value"] == "阿哲"
    assert name_fact["source"] == "user_stated"


def test_scenario_multiple_candidates_conflict(tmp_path: Path):
    """场景 10：多个主动候选冲突——只发一条，其余留在队列里，全部可审计。"""
    result = evaluate_proactive_lifecycle(
        now=NOW, relationship_stage="close", permissions=care_permissions(),
        pending_follow_ups=[{"hint": "问问他面试", "weight": 0.9}],
        carried_emotion="面试焦虑", carried_weight=0.8,
        opening="你今天写了 2 小时代码", daily_greeting="早安",
        meal_care="记得吃饭", mood_checkin="还好吗")
    assert result["selected"]["kind"] == "follow_up", "到期约定没有压过其它候选"
    sent = [c for c in result["candidates"] if c["decision"] == "send"]
    assert len(sent) == 1
    deferred = [c for c in result["candidates"] if c["decision"] == "defer"]
    assert len(deferred) >= 4
    for candidate in result["candidates"]:
        assert candidate["checkedAt"] == NOW.isoformat()
        assert "score" in candidate


def test_scenario_multiple_candidates_are_selected_deterministically():
    """同一批候选在任何机器上都选出同一条。"""
    def run():
        return evaluate_proactive_lifecycle(
            now=NOW, relationship_stage="close", permissions=care_permissions(),
            opening="你今天写了 2 小时代码", daily_greeting="早安",
            meal_care="记得吃饭", mood_checkin="还好吗")["selected"]["signature"]

    assert len({run() for _ in range(5)}) == 1


def test_scenario_desktop_to_mobile_handoff(tmp_path: Path):
    """场景 11：桌面到手机切换——同一段生活，不重复，不丢。"""
    clock = Clock(NOW)
    repo = repo_at(tmp_path, "s11", clock)
    say(repo, "d1", "我今天面试完了", device="desktop")
    clock.advance(minutes=10)
    say(repo, "m1", "刚出地铁", device="mobile")

    ids = [r["messageId"] for r in repo.messages("u")["messages"]]
    assert "d1" in ids and "m1" in ids

    # 手机重放桌面那条：幂等，不会变成两条
    assert say(repo, "d1", "我今天面试完了", device="mobile")["duplicate"] is True
    assert [r["messageId"] for r in repo.messages("u")["messages"]] == ids

    # 两端读到的是同一份主动决策
    assert repo.proactive_decision("u", "idle") == repo.proactive_decision("u", "idle")


def test_scenario_service_restart_recovery(tmp_path: Path):
    """场景 12：服务重启恢复——压力计数、候选队列和已发送记录全都还在。"""
    clock = Clock(NOW)
    path = tmp_path / "s12.sqlite3"
    repo = MemoryRepository(path, clock=clock)
    repo.adjust_affinity({"userId": "u", "score": 700.0})
    say(repo, "m1", "我今天面试完了")
    committed = repo.commit_proactive_decision({"userId": "u", "deliveryId": "D1"})
    pressure = repo.proactive_decision("u")["pressure"]
    boundaries = repo.profile_memories("u")["boundaries"]
    del repo

    revived = MemoryRepository(path, clock=clock)
    assert revived.proactive_decision("u")["pressure"] == pressure
    assert revived.profile_memories("u")["boundaries"] == boundaries
    assert revived.commit_proactive_decision({"userId": "u", "deliveryId": "D1"}) == committed
    assert [r["messageId"] for r in revived.messages("u")["messages"]][0] == "m1"
