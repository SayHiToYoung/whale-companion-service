# -*- coding: utf-8 -*-
"""最小故事线：两个人的共同故事，和她自己的生活故事。

故事在这里不是内容库，是**状态机**。它不生成情节，只记住"你们走到哪儿了"，
并且只在现实里真的发生了什么的时候往前走一格：

- 共同故事由你们之间实际发生的事推进——她知道了你的名字、那件事被反复聊起、关系到了某一档。
- 她自己的生活故事由时间和她自己的日常推进——种下去的东西过了多少天、她写了几篇日记。

四条底线，比情节重要得多：

1. **进度只由真实事件驱动。** 没有对应的事实、记忆、关系阶段或用户的明确选择，
   故事就停在原地。它绝不会因为"该有进展了"而有进展。
2. **同时最多一条主线加一条轻支线。** 主线是你们的，支线是她的。再多就成了连续剧。
3. **只输出当前节点。** 走过的节拍留在状态里，不往上下文里塞。
4. **故事只能造主动候选。** 发不发由 proactive 决定，故事没有绕过它的路。

模板刻意保持很小：两个共同故事、两个生活故事，各三到四拍。这是骨架，不是内容系统。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .affinity import stage_index_of
from .context_contract import ContextFact, ContextFragment, Priority

ARC_TYPES = ("shared", "companion_life")
ARC_WEIGHTS = ("main", "light")
ARC_STATUSES = ("eligible", "active", "paused", "completed", "abandoned")

# 一格走完之后要缓一缓。故事不该每轮都往前推——那不是故事，是进度条。
BEAT_COOLDOWN_HOURS = 6.0
# 用户明确说不想聊这个之后，隔这么久才重新变得可提。
REFUSAL_COOLDOWN_HOURS = 72.0


@dataclass(frozen=True)
class StoryArc:
    """一条故事线此刻的全部状态。走过的路留在这里，不往上下文里带。"""

    id: str
    type: str
    premise: str
    status: str = "eligible"
    current_beat: str = ""
    prerequisites: tuple[dict, ...] = ()
    related_memory_ids: tuple[str, ...] = ()
    next_eligible_at: str = ""
    completed_beats: tuple[str, ...] = ()
    available_branches: tuple[dict, ...] = ()
    weight: str = "main"
    # 起线时间是独立的：`updated_at` 每走一格都会变，用它算"种下去多少天"会一直归零。
    started_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        """JSON 友好的形态：元组转成列表，落库和读回是同一种东西。"""
        row = asdict(self)
        for key in ("prerequisites", "related_memory_ids", "completed_beats", "available_branches"):
            row[key] = list(row[key])
        return row

    @classmethod
    def from_dict(cls, row: dict) -> "StoryArc":
        known = {f for f in cls.__dataclass_fields__}
        data = {key: value for key, value in dict(row or {}).items() if key in known}
        for key in ("prerequisites", "related_memory_ids", "completed_beats", "available_branches"):
            if key in data:
                data[key] = tuple(data[key] or ())
        return cls(**data)


# ---------------------------------------------------------------------------
# 模板：两个共同故事 + 两个她自己的生活故事
# ---------------------------------------------------------------------------

_TEMPLATES: tuple[dict, ...] = (
    {
        "id": "shared:getting_to_know",
        "type": "shared",
        "weight": "main",
        "premise": "你们还在互相认识，她正一点点弄清楚你是个什么样的人。",
        "prerequisites": ({"signal": "relationship_stage_at_least", "value": "acquaintance"},),
        "beats": (
            {"id": "no_name", "summary": "她还叫不出你的名字。",
             "prompt": "你们才刚开始，别装熟。",
             "advance_when": ({"signal": "known_fact_count_at_least", "value": 1},)},
            {"id": "first_facts", "summary": "她开始记得住关于你的几件事了。",
             "prompt": "可以自然地用上你已经知道的事，但别拿出来表功。",
             "advance_when": ({"signal": "relationship_stage_at_least", "value": "familiar"},)},
            {"id": "settled", "summary": "你们已经熟到不用每次都从头解释。",
             "prompt": "说话可以省掉客套，直接接上上次的语气。",
             "advance_when": ({"signal": "relationship_stage_at_least", "value": "close"},)},
            {"id": "known", "summary": "她认得你，也认得你的沉默。", "prompt": "不用再证明什么。",
             "advance_when": ()},   # 空条件 = 终点
        ),
    },
    {
        "id": "shared:the_unfinished_thing",
        "type": "shared",
        "weight": "main",
        "premise": "有件事你们聊过好几次，一直没聊完。",
        "prerequisites": ({"signal": "thread_mentions_at_least", "value": 2},),
        "beats": (
            {"id": "reopened", "summary": "那件事又被提起来了。",
             "prompt": "记得它还没过去，但不要替用户宣布它到了哪一步。",
             "advance_when": ({"signal": "thread_mentions_at_least", "value": 4},),
             "branches": (
                 {"id": "ask", "label": "问问它现在怎么样了", "next_beat": "checking_in"},
                 {"id": "wait", "label": "先不问，等他自己说", "next_beat": "holding"},
             )},
            # 两条分支各走各的，但收在同一处——分叉之后靠 next 指路，不能再按顺序往下数。
            {"id": "checking_in", "summary": "她主动问起了那件事。", "prompt": "问得轻一点。",
             "advance_when": ({"signal": "thread_resolved"},), "next": "let_go"},
            {"id": "holding", "summary": "她记着，但没有开口问。",
             "prompt": "不要提，除非用户先说。",
             "advance_when": ({"signal": "thread_resolved"},), "next": "let_go"},
            {"id": "let_go", "summary": "那件事过去了。", "prompt": "可以轻轻带过，不必庆祝。",
             "advance_when": ()},
        ),
    },
    {
        "id": "life:the_window_plant",
        "type": "companion_life",
        "weight": "light",
        "premise": "她窗台上养了点东西，长得很慢。",
        "prerequisites": (),
        "beats": (
            {"id": "planted", "summary": "刚种下去，什么也看不出来。",
             "prompt": "如果聊到，就说它还没动静。",
             "advance_when": ({"signal": "days_since_start_at_least", "value": 3},)},
            {"id": "sprout", "summary": "冒了一点绿的出来。", "prompt": "可以顺口提一句，不用展开。",
             "advance_when": ({"signal": "days_since_start_at_least", "value": 14},)},
            {"id": "growing", "summary": "长起来了，她有点得意。", "prompt": "得意可以露出来一点。",
             "advance_when": ()},
        ),
    },
    {
        "id": "life:the_half_written_notebook",
        "type": "companion_life",
        "weight": "light",
        "premise": "她有个写了一半的本子，断断续续记着东西。",
        "prerequisites": ({"signal": "timeline_events_at_least", "value": 1},),
        "beats": (
            {"id": "blank_pages", "summary": "本子空着的页比写过的多。",
             "prompt": "提到时带着点心虚。",
             "advance_when": ({"signal": "timeline_events_at_least", "value": 3},)},
            {"id": "filling_up", "summary": "最近写得勤了些。", "prompt": "可以说说写了什么，别编细节。",
             "advance_when": ({"signal": "timeline_events_at_least", "value": 8},)},
            {"id": "half_full", "summary": "本子过半了，她自己也没想到。", "prompt": "轻描淡写地提。",
             "advance_when": ()},
        ),
    },
)

_TEMPLATE_BY_ID = {row["id"]: row for row in _TEMPLATES}


def story_templates() -> tuple[dict, ...]:
    return _TEMPLATES


def _beats(arc_id: str) -> tuple[dict, ...]:
    return tuple(_TEMPLATE_BY_ID.get(arc_id, {}).get("beats", ()))


def _beat(arc_id: str, beat_id: str) -> dict:
    return next((row for row in _beats(arc_id) if row["id"] == beat_id), {})


# ---------------------------------------------------------------------------
# 条件求值：声明式，可审计，绝不接受"该有进展了"这种理由
# ---------------------------------------------------------------------------

def _signal_met(condition: dict, signals: dict) -> bool:
    name = str(condition.get("signal") or "")
    wanted = condition.get("value")
    if name == "relationship_stage_at_least":
        return stage_index_of(str(signals.get("relationshipStage") or "acquaintance")) >= stage_index_of(str(wanted))
    if name == "known_fact_count_at_least":
        return int(signals.get("knownFactCount") or 0) >= int(wanted or 0)
    if name == "thread_mentions_at_least":
        return int(signals.get("threadMentions") or 0) >= int(wanted or 0)
    if name == "thread_resolved":
        return bool(signals.get("threadResolved"))
    if name == "days_since_start_at_least":
        return float(signals.get("daysSinceStart") or 0.0) >= float(wanted or 0)
    if name == "timeline_events_at_least":
        return int(signals.get("timelineEvents") or 0) >= int(wanted or 0)
    if name == "user_choice":
        return str(signals.get("userChoice") or "") == str(wanted or "")
    return False   # 不认识的条件永远不满足：未知不等于放行


def conditions_met(conditions, signals: dict) -> bool:
    return all(_signal_met(row, signals) for row in conditions or ())


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _branches_of(arc_id: str, beat_id: str) -> tuple[dict, ...]:
    return tuple(_beat(arc_id, beat_id).get("branches", ()))


# ---------------------------------------------------------------------------
# 起线与推进
# ---------------------------------------------------------------------------

def start_arc(template_id: str, *, now: datetime, memory_ids=()) -> StoryArc:
    template = _TEMPLATE_BY_ID[template_id]
    first = template["beats"][0]
    return StoryArc(
        id=template["id"], type=template["type"], premise=template["premise"],
        status="active", current_beat=first["id"],
        prerequisites=tuple(template.get("prerequisites", ())),
        related_memory_ids=tuple(str(value) for value in memory_ids),
        next_eligible_at=(now + timedelta(hours=BEAT_COOLDOWN_HOURS)).isoformat(),
        completed_beats=(), available_branches=_branches_of(template["id"], first["id"]),
        weight=template.get("weight", "main"),
        started_at=now.isoformat(), updated_at=now.isoformat(),
    )


def eligible_templates(arcs: list[StoryArc], signals: dict) -> list[dict]:
    """还没起过、且前置条件已经成立的模板。"""
    seen = {arc.id for arc in arcs}
    return [row for row in _TEMPLATES
            if row["id"] not in seen and conditions_met(row.get("prerequisites"), signals)]


def advance_arc(arc: StoryArc, signals: dict, *, now: datetime) -> StoryArc:
    """够条件就走一格，不够就原地不动。

    有分支的节拍必须等用户选——她不替用户决定故事往哪边走。
    """
    if arc.status != "active":
        return arc
    ready_at = _parse_time(arc.next_eligible_at)
    if ready_at is not None and now < ready_at:
        return arc
    beats = _beats(arc.id)
    index = next((i for i, row in enumerate(beats) if row["id"] == arc.current_beat), -1)
    if index < 0:
        return arc
    current = beats[index]

    branches = _branches_of(arc.id, arc.current_beat)
    if branches:
        chosen = next((row for row in branches
                       if str(row["id"]) == str(signals.get("userChoice") or "")), None)
        if chosen is None:
            return arc     # 等用户选，不自己往前走
        target = str(chosen["next_beat"])
    else:
        if not current.get("advance_when"):
            return _complete(arc, now)
        if not conditions_met(current["advance_when"], signals):
            return arc
        target = str(current.get("next") or "")
        if not target:
            target = beats[index + 1]["id"] if index + 1 < len(beats) else ""

    if not target:
        return _complete(arc, now)
    return replace(
        arc, current_beat=target,
        completed_beats=arc.completed_beats + (arc.current_beat,),
        available_branches=_branches_of(arc.id, target),
        next_eligible_at=(now + timedelta(hours=BEAT_COOLDOWN_HOURS)).isoformat(),
        updated_at=now.isoformat(),
    )


def _complete(arc: StoryArc, now: datetime) -> StoryArc:
    return replace(arc, status="completed",
                   completed_beats=arc.completed_beats + (arc.current_beat,),
                   available_branches=(), next_eligible_at="", updated_at=now.isoformat())


def choose_branch(arc: StoryArc, branch_id: str, *, now: datetime) -> StoryArc:
    """用户选了一条路。这是唯一能让有分支的节拍动起来的方式。"""
    ready = replace(arc, next_eligible_at="")
    return advance_arc(ready, {"userChoice": branch_id}, now=now)


# ---------------------------------------------------------------------------
# 暂停与恢复
# ---------------------------------------------------------------------------

def pause_arc(arc: StoryArc, reason: str, *, now: datetime, cooldown_hours: float = 0.0) -> StoryArc:
    if arc.status in {"completed", "abandoned"}:
        return arc
    resume_at = (now + timedelta(hours=cooldown_hours)).isoformat() if cooldown_hours else arc.next_eligible_at
    return replace(arc, status="paused", next_eligible_at=resume_at, updated_at=now.isoformat())


def refuse_arc(arc: StoryArc, *, now: datetime) -> StoryArc:
    """用户明确说不想聊这条线。暂停，而不是抹掉——他可能只是今天不想聊。"""
    return pause_arc(arc, "user_refused", now=now, cooldown_hours=REFUSAL_COOLDOWN_HOURS)


def apply_story_gates(arcs: list[StoryArc], *, permissions: dict, boundary_state: str = "open",
                      period: str = "daytime", now: datetime) -> list[StoryArc]:
    """关系权限、用户边界和免打扰决定故事能不能继续讲。

    这三样任何一样不满足，故事就停下——不是讲慢一点，是停下。恢复要等条件重新成立，
    并且被拒绝过的那条还要额外等满冷却，不能第二天又端上来。
    """
    permissions = permissions or {}
    quiet = period == "late_night"
    result = []
    for arc in arcs:
        # 共同故事讲的是你们之间的经历，要有"可以引用共同记忆"这个许可才配讲。
        # 她自己的生活故事不需要这个许可——那是她的日子，不是你们的账。
        blocked = (
            boundary_state != "open"
            or quiet
            or (arc.type == "shared" and not permissions.get("allowSharedMemory"))
        )
        if blocked:
            result.append(pause_arc(arc, "gated", now=now))
            continue
        if arc.status == "paused":
            ready_at = _parse_time(arc.next_eligible_at)
            if ready_at is not None and now < ready_at:
                result.append(arc)          # 冷却没到，继续停着
                continue
            result.append(replace(arc, status="active", updated_at=now.isoformat()))
            continue
        result.append(arc)
    return result


def select_current_arcs(arcs: list[StoryArc]) -> list[StoryArc]:
    """同时最多一条主线 + 一条轻支线。再多就成了连续剧。"""
    active = [arc for arc in arcs if arc.status == "active"]
    picked = []
    for weight in ARC_WEIGHTS:
        candidate = next((arc for arc in active if arc.weight == weight), None)
        if candidate is not None:
            picked.append(candidate)
    return picked


# ---------------------------------------------------------------------------
# 对外投影：只给当前节点
# ---------------------------------------------------------------------------

class StoryProvider(Protocol):
    def context_fragments(self, *, now) -> list[ContextFragment]: ...


def arc_fragment(arc: StoryArc, *, now: datetime) -> ContextFragment:
    """只有当前这一格进上下文。走过的节拍、完整节拍表、前置条件都留在状态里。"""
    beat = _beat(arc.id, arc.current_beat)
    value = {
        "arcId": arc.id,
        "type": arc.type,
        "premise": arc.premise,
        "currentBeat": {"id": arc.current_beat,
                        "summary": str(beat.get("summary") or ""),
                        "usage": str(beat.get("prompt") or "")},
        "availableBranches": [{"id": row["id"], "label": row["label"]}
                              for row in arc.available_branches],
    }
    sources = tuple(arc.related_memory_ids) or ("story:" + arc.id,)
    return ContextFragment(module="story", priority=Priority.STORY,
        facts=(ContextFact("story:" + arc.id, value, source="derived", lifecycle="active"),),
        instructions=("故事只是你们之间的背景，不要复述进度，也不要编造没发生过的节点。",),
        source_event_ids=sources, created_at=arc.updated_at or now.isoformat(),
        token_budget=4000)


def story_fragments(provider: StoryProvider | None, *, now) -> list[ContextFragment]:
    if provider is not None:
        return provider.context_fragments(now=now)
    return [ContextFragment(module="story", priority=Priority.STORY,
        facts=(ContextFact("story", {"status": "empty", "currentNode": None}, source="default", lifecycle="unknown"),),
        instructions=("没有已确认的故事节点，不得编造故事进展。",),
        source_event_ids=("story:empty",), confidence=0.0, created_at=now.isoformat())]


class ArcStoryProvider:
    """把一组故事线接到既有的 story_fragments 读接口上。"""

    def __init__(self, arcs: list[StoryArc]):
        self.arcs = list(arcs)

    def context_fragments(self, *, now) -> list[ContextFragment]:
        current = select_current_arcs(self.arcs)
        if not current:
            return story_fragments(None, now=now)
        return [arc_fragment(arc, now=now) for arc in current]


# ---------------------------------------------------------------------------
# 主动候选：故事只能提议，不能发送
# ---------------------------------------------------------------------------

def story_candidate_signals(arcs: list[StoryArc], *, now: datetime) -> list[dict]:
    """当前节点能提供的主动候选信号。

    **故事没有发送这条路。** 它产出的是候选，和日程产出的候选走同一条闸门链；
    是否开口、什么时候开口，仍然由 proactive 决定。
    """
    signals = []
    for arc in select_current_arcs(arcs):
        ready_at = _parse_time(arc.next_eligible_at)
        if ready_at is not None and now < ready_at:
            continue
        beat = _beat(arc.id, arc.current_beat)
        summary = str(beat.get("summary") or "")
        if not summary:
            continue
        signals.append({
            "kind": "story_beat",
            "reason": "story_beat",
            "content": summary,
            "topic": arc.premise,
            "motive": "想跟你说说这条线走到哪儿了",
            "signature": f"story_beat:{arc.id}:{arc.current_beat}",
            "windowStart": now.isoformat(),
            "bestUntil": (now + timedelta(hours=6)).isoformat(),
            "expiresAt": (now + timedelta(hours=24)).isoformat(),
        })
    return signals


def arc_age_days(arc: StoryArc, now: datetime) -> float:
    """这条线起了多少天。用起线时间算，不用最后一次改动的时间。"""
    started = _parse_time(arc.started_at) or _parse_time(arc.updated_at)
    return 0.0 if started is None else max(0.0, (now - started).total_seconds() / 86_400.0)
