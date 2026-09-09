# -*- coding: utf-8 -*-
"""按记忆类型分策略的连续激活度。

固定过期把"还记不记得"压成一个瞬间的悬崖：48 小时内满血，48 小时零一秒彻底消失。
真实的记忆不这样退场，而且不同类型的记忆本来就该以不同速度退场——桌面上"刚才在写代码"
两天后理应淡掉；用户亲口说的难过不该同样淡掉；两个人一起revisit过的事、以及用户是谁，
更不该按同一把尺子被时间抹掉。

本模块把"还记不记得"改写成一个连续量 activation ∈ [0, 1]：

    activation = retention(按类型的时间保持率) × (STRENGTH_FLOOR + (1-STRENGTH_FLOOR) × strength)

两个因子分工不重叠：retention 只回答"过去多久了"，strength 回答"它凭什么值得被想起来"。
任何一个塌下去都会让记忆退到背景里，但都不会一刀切。

本模块是纯函数、无依赖：不读数据库、不调模型、不写状态。调用方负责提供提及次数、
话题相关度这类外部信号；它们会被合并进同一个可审计的读数。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone


TRANSIENT_CONTEXT = "transient_context"
USER_EMOTION = "user_emotion"
SHARED_EXPERIENCE = "shared_experience"
RELATIONSHIP = "relationship"
USER_FACT = "user_fact"
BOUNDARY = "boundary"

MEMORY_CLASSES = (
    TRANSIENT_CONTEXT, USER_EMOTION, SHARED_EXPERIENCE, RELATIONSHIP, USER_FACT, BOUNDARY,
)

# 半衰期而不是有效期：越靠后的类型越接近"不因为时间而忘记"。
# 用户事实与边界是 None——它们只会被用户改写或撤回，不会被时间冲淡。
HALF_LIFE_HOURS: dict[str, float | None] = {
    TRANSIENT_CONTEXT: 24.0,
    USER_EMOTION: 60.0,
    SHARED_EXPERIENCE: 720.0,
    RELATIONSHIP: 2160.0,
    USER_FACT: None,
    BOUNDARY: None,
}

# 低于这个激活度就不再由大鲸主动带进上下文；它取代了原来的固定新鲜期。
SPEAKABLE_ACTIVATION = 0.30

# strength 的各项权重，合计 1.0。改动这里等于改动"什么样的记忆值得被记住"。
STRENGTH_WEIGHTS = {
    "importance": 0.30,
    "emotionalIntensity": 0.20,
    "sourceConfidence": 0.20,
    "topicRelevance": 0.15,
    "reinforcement": 0.10,
    "unresolved": 0.05,
}
# strength 只在 [FLOOR, 1] 区间内调节 retention，不能单独把一条陈年记忆拉回台前。
STRENGTH_FLOOR = 0.35

# 每被再次提及一次，半衰期延长 60%，最多算 5 次——反复聊过的事忘得慢，但不会永不遗忘。
REINFORCEMENT_HALF_LIFE_BONUS = 0.6
MAX_COUNTED_MENTIONS = 5

# 只有用户亲口说出的情绪标签才有强度；本模块不推断任何用户没说过的情绪。
EMOTION_INTENSITY = {
    "angry": 0.9,
    "sad": 0.85,
    "anxious": 0.85,
    "wronged": 0.85,
    "frustrated": 0.7,
    "excited": 0.7,
    "happy": 0.6,
    "tired": 0.6,
}

_TIME_FIELDS = ("occurredAt", "endedAt", "startedAt", "createdAt", "receivedAt")
_LAYER_CLASSES = {"L1": TRANSIENT_CONTEXT, "L2": TRANSIENT_CONTEXT, "L3": USER_EMOTION}
_KIND_CLASSES = {
    "stated_emotion": USER_EMOTION,
    "shared_experience": SHARED_EXPERIENCE,
    "episode": SHARED_EXPERIENCE,
    "milestone": SHARED_EXPERIENCE,
    "promise": SHARED_EXPERIENCE,
    "commitment": SHARED_EXPERIENCE,
    "confirmed_insight": RELATIONSHIP,
    "interaction_preference": RELATIONSHIP,
    "relationship": RELATIONSHIP,
    "user_fact": USER_FACT,
    "profile_fact": USER_FACT,
    "boundary": BOUNDARY,
}
_CLOSED_STATUSES = {"resolved", "suppressed", "corrected"}


def parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _clamp(value: object, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if not math.isfinite(number) else max(0.0, min(1.0, number))


def classify_memory(memory: dict, lifecycle: dict | None = None) -> str:
    """记忆类型决定它以多快的速度退场。

    优先顺序是"谁最清楚这条记忆的性质"：持有关系数据的服务端（lifecycle 覆盖层）>
    生产这条记忆的客户端（payload 自述）> 从 kind / layer 推出的默认值。
    """
    for declared in (
        str((lifecycle or {}).get("memoryClass") or ""),
        str(memory.get("memoryClass") or ""),
    ):
        if declared in MEMORY_CLASSES:
            return declared
    kind = str(memory.get("kind") or "")
    if kind in _KIND_CLASSES:
        return _KIND_CLASSES[kind]
    if memory.get("rule"):
        return BOUNDARY
    if memory.get("predicate"):
        return USER_FACT
    return _LAYER_CLASSES.get(str(memory.get("layer") or ""), TRANSIENT_CONTEXT)


def memory_importance(memory: dict) -> float:
    explicit = memory.get("importance")
    if explicit is not None:
        try:
            return max(0.0, min(1.0, float(explicit)))
        except (TypeError, ValueError):
            pass
    if memory.get("layer") == "L3":
        return 0.9
    if memory.get("layer") == "L2":
        return 0.7
    seconds = max(0.0, float(memory.get("durationSeconds") or 0.0))
    return min(0.8, 0.35 + seconds / 28_800.0)


def memory_source_confidence(memory: dict) -> float:
    """来源置信度：L2 是推导出来的线索，天生比观察和用户原话弱。"""
    explicit = memory.get("confidence")
    if explicit is not None:
        return _clamp(explicit, 0.5)
    return 0.5 if memory.get("layer") == "L2" else 1.0


def emotional_intensity(memory: dict) -> float:
    explicit = memory.get("emotionalIntensity")
    if explicit is not None:
        return _clamp(explicit, 0.0)
    if memory.get("layer") != "L3":
        return 0.0
    return EMOTION_INTENSITY.get(str(memory.get("label") or ""), 0.5)


def memory_reference_time(memory: dict, lifecycle: dict | None = None) -> datetime | None:
    """"上一次被碰到"是什么时候：发生时间、状态变更、被再次提及，取最晚的一个。"""
    stored = lifecycle or {}
    candidates = [parse_time(memory.get(key)) for key in _TIME_FIELDS]
    candidates.append(parse_time(stored.get("updatedAt")))
    candidates.append(parse_time(stored.get("lastMentionedAt")))
    known = [value for value in candidates if value is not None]
    return max(known) if known else None


def _mention_count(lifecycle: dict, override: int | None) -> int:
    value = override if override is not None else lifecycle.get("mentionCount")
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _unresolved(memory: dict, status: str) -> float:
    if status in _CLOSED_STATUSES:
        return 0.0
    if memory.get("unresolved") is True or str(memory.get("followUpHint") or ""):
        return 1.0
    if status == "pending_confirmation":
        return 1.0
    return 1.0 if memory.get("layer") == "L3" else 0.0


def memory_activation(
    memory: dict,
    lifecycle: dict | None = None,
    *,
    now: datetime | None = None,
    relevance: float = 0.0,
    mention_count: int | None = None,
    status: str = "",
) -> dict:
    """返回一条记忆此刻的激活读数，附带可复核的分项。

    读数是纯函数：同样的输入永远得到同样的输出，控制台可以逐项解释"为什么这条还在/不在"。
    """
    stored = dict(lifecycle or {})
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    memory_class = classify_memory(memory, stored)
    effective_status = str(status or stored.get("status") or "")
    mentions = _mention_count(stored, mention_count)

    base_half_life = HALF_LIFE_HOURS.get(memory_class)
    reference = memory_reference_time(memory, stored)
    age_hours = max(0.0, (current - reference).total_seconds() / 3600.0) if reference else 0.0
    if base_half_life is None:
        half_life = None
        retention = 1.0
    else:
        half_life = base_half_life * (
            1.0 + REINFORCEMENT_HALF_LIFE_BONUS * min(mentions, MAX_COUNTED_MENTIONS)
        )
        retention = 0.5 ** (age_hours / half_life)

    components = {
        "importance": memory_importance(memory),
        "emotionalIntensity": emotional_intensity(memory),
        "sourceConfidence": memory_source_confidence(memory),
        "topicRelevance": _clamp(relevance, 0.0),
        "reinforcement": min(mentions, MAX_COUNTED_MENTIONS) / MAX_COUNTED_MENTIONS,
        "unresolved": _unresolved(memory, effective_status),
    }
    strength = sum(STRENGTH_WEIGHTS[name] * value for name, value in components.items())
    activation = retention * (STRENGTH_FLOOR + (1.0 - STRENGTH_FLOOR) * strength)
    return {
        "memoryClass": memory_class,
        "activation": round(activation, 4),
        "retention": round(retention, 4),
        "strength": round(strength, 4),
        "ageHours": round(age_hours, 3),
        "halfLifeHours": None if half_life is None else round(half_life, 3),
        "mentionCount": mentions,
        "components": {name: round(value, 4) for name, value in components.items()},
        "threshold": SPEAKABLE_ACTIVATION,
        "speakable": activation >= SPEAKABLE_ACTIVATION,
    }
