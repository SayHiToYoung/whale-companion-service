# -*- coding: utf-8 -*-
"""大鲸的好感度状态机：长期好感分 → 8 档阶段 → 短期互动档 → 表达/主动权限。

从 astrbot_plugin_private_companion 的 relationship_* 提取，按本项目哲学精简：

0. 亲密度不是一个数字。内部状态有七个维度：长期好感分、阶段、短期互动档，
   再加上三个各有各的时间尺度的连续量（熟悉度、信任、暖意），以及一个不由时间决定、
   只由用户明说决定的边界态。它们不能互相替代——很熟不等于被信任，暖意上头不等于
   可以越过用户划下的线。
1. 长期好感是「分数 + 账本」而非简单计数：事件去重、单次/每日上限、阶段迟滞、
   只向 0 回落的自然降温，让亲近是慢慢长出来的，而不是被反复事件刷上去。
2. 越界按档位差分级，踩底线（贬低角色/珍视之物/在意的人）单独最重；修复有据可查。
3. 阶段不仅决定「有多熟」，还决定允许做什么：主动关怀额度、可否调侃/续话/提记忆/日常关怀。
4. 纯确定性：本模块不调用 LLM，所有增量可复现、可审计。

存储由调用方持有（relationship_state / relationship_ledger 表），本模块是纯函数。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

SCORE_MIN = -1200.0
SCORE_MAX = 1200.0

# 8 个普通阶段（key 顺序即亲密度递增），阈值来自 relationship_policy.py。
STAGES: tuple[dict, ...] = (
    {"key": "deeply_distant", "label": "极度疏离", "min": -1200, "max": -801, "proactive_care_limit": 0, "allow_playful": False, "allow_followup": False, "allow_memory": False, "allow_daily_care": False},
    {"key": "strongly_distant", "label": "强烈疏离", "min": -800, "max": -401, "proactive_care_limit": 0, "allow_playful": False, "allow_followup": False, "allow_memory": False, "allow_daily_care": False},
    {"key": "distant", "label": "疏离", "min": -400, "max": -1, "proactive_care_limit": 0, "allow_playful": False, "allow_followup": False, "allow_memory": False, "allow_daily_care": False},
    {"key": "acquaintance", "label": "初识", "min": 0, "max": 199, "proactive_care_limit": 0, "allow_playful": False, "allow_followup": True, "allow_memory": False, "allow_daily_care": False},
    {"key": "familiar", "label": "熟悉", "min": 200, "max": 599, "proactive_care_limit": 1, "allow_playful": True, "allow_followup": True, "allow_memory": True, "allow_daily_care": True},
    {"key": "close", "label": "亲近", "min": 600, "max": 899, "proactive_care_limit": 2, "allow_playful": True, "allow_followup": True, "allow_memory": True, "allow_daily_care": True},
    {"key": "intimate", "label": "亲密", "min": 900, "max": 1199, "proactive_care_limit": 3, "allow_playful": True, "allow_followup": True, "allow_memory": True, "allow_daily_care": True},
    {"key": "deeply_bonded", "label": "深度联结", "min": 1200, "max": 1200, "proactive_care_limit": 4, "allow_playful": True, "allow_followup": True, "allow_memory": True, "allow_daily_care": True},
)

STAGE_KEYS = tuple(s["key"] for s in STAGES)
_STAGE_BY_KEY = {s["key"]: s for s in STAGES}

# 互动状态 7 档（按亲密度递增）。close/affectionate 仅主要用户开放。
INTERACTION_STATES = ("avoidant", "hurt", "relaxed", "lively", "warm", "close", "affectionate")

# 边界态：不由时间决定，只由用户明说决定。它压过其余六个维度。
BOUNDARY_STATES = ("open", "guarded", "restricted")

# 三个连续维度各有各的时间尺度，这正是它们不能合成一个数字的原因：
# 暖意两天就凉，信任要一个月才淡，熟悉半年也还记得。
WARMTH_HALF_LIFE_DAYS = 2.0
TRUST_HALF_LIFE_DAYS = 30.0
FAMILIARITY_HALF_LIFE_DAYS = 180.0

WARMTH_PER_POSITIVE = 0.12
TRUST_PER_POSITIVE = 0.02
FAMILIARITY_PER_EVENT = 0.01      # 熟悉是攒出来的，攒得慢
TRUST_LOSS_PER_VIOLATION = 0.15   # 信任掉得比涨得快得多
WARMTH_LOSS_PER_VIOLATION = 0.4

# 账本机制。
DEDUP_WINDOW_MINUTES = 30.0          # 同 eventKey 30 分钟内去重
SINGLE_EVENT_CAP = 4.0               # 普通事件单次增量上限
VIOLATION_EVENT_CAP = 12.0           # 越界/底线单次增量上限
DAILY_POSITIVE_CAP = 12.0            # 每日正向增量上限
# 被主动消息"钓"出来的那句回应，每天最多只算这么多分。
# 否则多打扰几次、多收几句客气话，关系就能被刷上去——那不是亲近，是骚扰的副产品。
SOLICITED_POSITIVE_CAP = 2.0
HYSTERESIS = 20.0                    # 阶段迟滞：跨档需越过阈值 ±20
LEDGER_MAX_ENTRIES = 200             # 账本限长
DECAY_GRACE_DAYS = 3.0               # 自然降温宽限：停止互动 3 天后才开始向 0 回落
DECAY_PER_DAY = 0.03                 # 每日向 0 回落比例（3%/天，温和）

# 越界扣分（按档位差 gap，或踩底线）。
BOUNDARY_DEDUCT = {"light": 4, "medium": 7, "heavy": 12, "bottom_line": 14}


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _clamp_score(value: float) -> float:
    return max(SCORE_MIN, min(SCORE_MAX, value))


def stage_of(score: float) -> dict:
    """分数 → 阶段（查表）。"""
    value = _clamp_score(score)
    for stage in STAGES:
        if stage["min"] <= value <= stage["max"]:
            return dict(stage)
    return dict(STAGES[3])  # 理论不可达，回落到初识


def stage_key_of(score: float) -> str:
    return str(stage_of(score)["key"])


def stage_index_of(key: str) -> int:
    return STAGE_KEYS.index(key) if key in STAGE_KEYS else 3


def empty_affinity_state() -> dict:
    """初始状态：初识（0 分），互动 relaxed，三个连续维度归零，边界未设限。"""
    return {
        "score": 0.0,
        "stage": "acquaintance",
        "interaction": "relaxed",
        "familiarity": 0.0,
        "trust": 0.0,
        "warmth": 0.0,
        "boundaryState": "open",
        "updatedAt": "",
    }


def _unit(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if number != number else max(0.0, min(1.0, number))


def normalize_affinity_state(state: object) -> dict:
    """把任意来源的状态补齐成完整的七维内部状态。

    升级前落库的行只有 score/stage/interaction；这里给缺的维度补零值，
    老数据因此可以直接读进新逻辑，不需要迁移脚本。
    """
    base = empty_affinity_state()
    if not isinstance(state, dict):
        return base
    stage = str(state.get("stage") or base["stage"])
    boundary = str(state.get("boundaryState") or base["boundaryState"])
    return {
        "score": round(_clamp_score(float(state.get("score") or 0.0)), 2),
        "stage": stage if stage in STAGE_KEYS else base["stage"],
        "interaction": str(state.get("interaction") or base["interaction"]),
        "familiarity": round(_unit(state.get("familiarity")), 4),
        "trust": round(_unit(state.get("trust")), 4),
        "warmth": round(_unit(state.get("warmth")), 4),
        "boundaryState": boundary if boundary in BOUNDARY_STATES else base["boundaryState"],
        "updatedAt": str(state.get("updatedAt") or ""),
    }


def _decayed(value: float, last: datetime | None, now: datetime, half_life_days: float) -> float:
    """只向中性（0）回归，永远不会把一个维度推得离中性更远。"""
    if last is None or half_life_days <= 0:
        return value
    days = (now - last).total_seconds() / 86_400.0
    if days <= 0:
        return value
    return value * (0.5 ** (days / half_life_days))


def boundary_violation_delta(gap: int, malicious: bool = False) -> int:
    """越界扣分：恶意踩底线最重，否则按档位差分轻/中/重。"""
    if malicious:
        return BOUNDARY_DEDUCT["bottom_line"]
    if gap >= 3:
        return BOUNDARY_DEDUCT["heavy"]
    if gap == 2:
        return BOUNDARY_DEDUCT["medium"]
    if gap == 1:
        return BOUNDARY_DEDUCT["light"]
    return 0


def apology_repair_delta(last_violation_delta: float) -> int:
    """道歉修复：返还上次越界扣分的 60%。"""
    return int(round(abs(float(last_violation_delta)) * 0.6))


def interaction_state_of(stage_key: str, recent_violation: bool = False, warmth: float = 0.0) -> str:
    """短期互动档：由长期阶段 + 近期越界/暖意派生。"""
    if recent_violation:
        return "hurt" if stage_index_of(stage_key) >= 4 else "avoidant"
    if stage_key in {"intimate", "deeply_bonded"}:
        return "affectionate"
    if stage_key == "close":
        return "close" if warmth > 0 else "warm"
    if stage_key == "familiar":
        return "warm"
    if stage_key == "acquaintance":
        return "lively" if warmth > 0 else "relaxed"
    return "avoidant"


def proactive_quota(global_limit: float, user_limit: float, stage_key: str, interaction: str) -> int:
    """主动额度 = min(全局, 用户, 阶段, 互动状态)。"""
    stage_limit = float(stage_of_by_key(stage_key).get("proactive_care_limit") or 0)
    if interaction in {"avoidant", "hurt"}:
        interaction_limit = 0.0
    else:
        interaction_limit = float("inf")
    values = [global_limit, user_limit, stage_limit, interaction_limit]
    return int(max(0.0, min(values)))


def stage_of_by_key(key: str) -> dict:
    return dict(_STAGE_BY_KEY.get(key, STAGES[3]))


# ---------------------------------------------------------------------------
# 边界态与关系权限
# ---------------------------------------------------------------------------

# 用户明说"别主动找我"这一类，直接把关系收到最紧。
_NO_CONTACT = re.compile(r"(?:(?:不要|别)(?:再)?(?:主动)?(?:找|联系|打扰|烦)我|免打扰|别理我|不想说话)")
# 划下具体的线（别提某事、别那样叫我、别主动联系），收紧但不切断。
_GUARDING_KINDS = frozenset({"topic_suppression", "addressing", "contact"})
# 纠正事实不是划线："我不是学生"改的是她对你的认知，不该顺带把玩笑话也一起关掉。
_NON_GUARDING_KINDS = frozenset({"identity_correction"})


def derive_boundary_state(boundaries: list[dict] | None = None, *, refusals: int = 0) -> str:
    """边界态只由用户明说决定，不由分数、时间或她自己的感觉决定。

    这是"用户拒绝或设置边界后立即收敛"的落点：本轮刚说出口的边界也算数，
    不必等它落库、更不必等好感分慢慢降下来——收敛必须是同一轮就发生的事。
    """
    active = [row for row in (boundaries or [])
              if isinstance(row, dict) and str(row.get("status", "active")) == "active"]
    for row in active:
        # kind 是结构化信号，优先于对规则原文再做一次解析。
        if str(row.get("kind") or "") == "contact" or row.get("blockProactive"):
            return "restricted"
        if _NO_CONTACT.search(str(row.get("rule") or "")):
            return "restricted"
    if refusals >= 2:
        return "restricted"
    if refusals >= 1 or any(
        str(row.get("kind") or "") in _GUARDING_KINDS
        or (str(row.get("kind") or "") not in _NON_GUARDING_KINDS and row.get("rule"))
        for row in active
    ):
        return "guarded"
    return "open"


def _has_addressing_boundary(boundaries: list[dict] | None) -> bool:
    return any(isinstance(row, dict) and str(row.get("status", "active")) == "active"
               and str(row.get("kind") or "") == "addressing"
               for row in (boundaries or []))


# 更亲密的表达要的不只是"熟"，还要被信任过。分数到了、信任没到，一样不开。
NICKNAME_MIN_TRUST = 0.25
INTIMATE_MIN_TRUST = 0.5


def relationship_permissions(
    state: dict,
    *,
    boundaries: list[dict] | None = None,
    global_quota: float = float("inf"),
    user_quota: float = float("inf"),
) -> dict:
    """给模型看的关系权限：只说能做什么，不带任何内部评分。

    权限不是分数的同义词。它由四样东西共同决定，任何一样都能单独把门关上：
    阶段（有多熟）、互动档（此刻的气氛）、信任（够不够格更进一步）、边界态（用户划的线）。
    """
    internal = normalize_affinity_state(state)
    stage = internal["stage"]
    flags = stage_of_by_key(stage)
    interaction = internal["interaction"]
    boundary_state = internal["boundaryState"]
    if boundary_state == "open" and boundaries:
        boundary_state = derive_boundary_state(boundaries)
    withdrawn = interaction in {"hurt", "avoidant"}
    restricted = boundary_state == "restricted"
    guarded = boundary_state in {"guarded", "restricted"}

    allow_playful = bool(flags["allow_playful"]) and not withdrawn and not guarded
    allow_shared_memory = bool(flags["allow_memory"]) and not restricted
    allow_nickname = (
        stage_index_of(stage) >= stage_index_of("familiar")
        and internal["trust"] >= NICKNAME_MIN_TRUST
        and not guarded
        and not _has_addressing_boundary(boundaries)
    )
    allow_intimate = (
        stage_index_of(stage) >= stage_index_of("close")
        and internal["trust"] >= INTIMATE_MIN_TRUST
        and not withdrawn
        and not guarded
    )
    allow_proactive_care = bool(flags["allow_daily_care"]) and not withdrawn and not restricted
    quota = 0 if restricted else proactive_quota(global_quota, user_quota, stage, interaction)
    return {
        "allowProactiveCare": allow_proactive_care,
        "allowSharedMemory": allow_shared_memory,
        "allowNickname": allow_nickname,
        "allowPlayful": allow_playful,
        "allowIntimateTone": allow_intimate,
        "proactiveQuota": int(quota),
    }


# 权限关掉时，哪些模块就不该继续往上下文里塞东西。
# 只有"用户划了线"或"关系已经疏离"才真的动结构；其余情况权限仍是给模型的分寸提示，
# 不去悄悄改变既有的上下文组成。
_PERMISSION_BLOCKS = {
    "allowPlayful": ("learnedExpressions",),
    "allowSharedMemory": ("openThreads",),
    "allowProactiveCare": ("dailyLife",),
}


def relationship_constraints(state: dict, permissions: dict, *, boundaries: list[dict] | None = None) -> dict:
    """权限拒绝时交给组装器执行的结构约束。

    返回的是控制数据，永远不进模型：组装器据此把对应模块挡在上下文之外。
    """
    internal = normalize_affinity_state(state)
    boundary_state = internal["boundaryState"]
    if boundary_state == "open" and boundaries:
        boundary_state = derive_boundary_state(boundaries)
    distant = stage_index_of(internal["stage"]) < stage_index_of("acquaintance")
    if boundary_state == "open" and not distant:
        return {"boundaryState": boundary_state, "blocked_modules": [], "deny_sensitivities": []}
    blocked: list[str] = []
    for name, modules in _PERMISSION_BLOCKS.items():
        if not permissions.get(name):
            blocked.extend(modules)
    if boundary_state == "restricted":
        blocked.append("story")
    return {
        "boundaryState": boundary_state,
        "blocked_modules": sorted(set(blocked)),
        "deny_sensitivities": ["intimate"] if not permissions.get("allowIntimateTone") else [],
    }


# 从用户消息推导好感事件（保守：只识别明确的暖意与明确的越界）。
_WARM = re.compile(r"(谢谢|辛苦|有你在|真好|喜欢你|想你了|抱抱|好棒|太会了|厉害|你懂我)")
_VIOLATION = re.compile(r"(你(?:真|太|好)?(?:笨|蠢|废物|垃圾|没用|不行|讨厌|恶心|滚))")


def derive_affinity_event(user_text: str, event_key: str = "") -> dict | None:
    """从一条用户消息推导好感事件：明确暖意 +2，明确越界（贬低角色）-7，其余 None。

    event_key 用于账本去重：建议传 `warm:{message_id}` / `boundary:{message_id}`。
    """
    text = str(user_text or "").strip()
    if not text:
        return None
    if _VIOLATION.search(text):
        return {"eventKey": event_key or _event_key("boundary", text), "kind": "boundary", "delta": -7.0}
    if _WARM.search(text):
        return {"eventKey": event_key or _event_key("warm", text), "kind": "warm", "delta": 2.0}
    return None


def _event_key(kind: str, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"{kind}:{digest}"


def _is_same_day(a: datetime, b: datetime) -> bool:
    return a.date() == b.date()


def _decay_toward_zero(score: float, last_activity: datetime, now: datetime) -> float:
    """只向 0 回落：停止互动超过宽限期后，每天温和衰减。"""
    days = (now - last_activity).total_seconds() / 86_400.0
    if days <= DECAY_GRACE_DAYS:
        return score
    decay_days = days - DECAY_GRACE_DAYS
    factor = (1.0 - DECAY_PER_DAY) ** decay_days
    return score * factor


def apply_event(state: dict, ledger: list[dict], event: dict, now: datetime | None = None) -> tuple[dict, list[dict]]:
    """应用一个好感事件，返回 (新状态, 新账本)。

    event: {eventKey, kind, delta, at}
      kind ∈ {"warm", "self_disclosure", "boundary", "repair", "care", ...}
      delta 为正表示亲近增、为负表示越界减。
    规则：去重 → 单次上限 → 每日正向上限 → 迟滞 → 只向 0 降温 → 账本限长。
    """
    moment = now or datetime.now(timezone.utc)
    state = normalize_affinity_state(state)
    ledger = [row for row in ledger if isinstance(row, dict)]
    event_key = str(event.get("eventKey") or "").strip()
    delta = float(event.get("delta") or 0.0)
    at = _parse_time(event.get("at")) or moment
    # 没有 eventKey 就是没有证据。关系不会因为"感觉上应该变了"而变。
    if not event_key or delta == 0.0:
        return state, ledger

    score = _clamp_score(float(state.get("score") or 0.0))

    # 1. 去重：同 eventKey 30 分钟内忽略。
    for row in ledger:
        if row.get("eventKey") != event_key:
            continue
        row_at = _parse_time(row.get("at"))
        if row_at is not None and (moment - row_at).total_seconds() <= DEDUP_WINDOW_MINUTES * 60:
            return state, ledger

    # 2. 单次增量上限（越界/底线可到 12，普通 4）。
    cap = VIOLATION_EVENT_CAP if event.get("kind") in {"boundary", "bottom_line"} else SINGLE_EVENT_CAP
    delta = max(-VIOLATION_EVENT_CAP, min(cap, delta))

    # 3. 每日正向增量上限。被主动消息钓出来的回应另算一份更小的额度——
    #    关系不能靠多打扰几次刷上去。
    solicited = bool(event.get("solicited"))
    if delta > 0:
        same_day = [row for row in ledger
                    if _is_same_day(_parse_time(row.get("at")) or moment, moment)]
        today_positive = sum(max(0.0, float(row.get("delta") or 0.0)) for row in same_day)
        room = DAILY_POSITIVE_CAP - today_positive
        if solicited:
            today_solicited = sum(max(0.0, float(row.get("delta") or 0.0))
                                  for row in same_day if row.get("solicited"))
            room = min(room, SOLICITED_POSITIVE_CAP - today_solicited)
        if room <= 0:
            return state, ledger
        delta = min(delta, room)

    # 4. 迟滞：分数自由移动，但阶段只在越过阈值 ±20 后才切换（防抖动）。
    old_stage = stage_key_of(score)
    new_score = _clamp_score(score + delta)
    naive_stage = stage_key_of(new_score)
    if naive_stage != old_stage:
        old_idx = stage_index_of(old_stage)
        new_idx = stage_index_of(naive_stage)
        if new_idx > old_idx:
            # 升级：越过目标档下界 + 迟滞才算。
            threshold = STAGES[new_idx]["min"]
            new_stage = naive_stage if new_score >= threshold + HYSTERESIS else old_stage
        else:
            # 降级：跌破当前档下界 - 迟滞才算。
            threshold = STAGES[old_idx]["min"]
            new_stage = naive_stage if new_score <= threshold - HYSTERESIS else old_stage
    else:
        new_stage = naive_stage

    # 5. 三个连续维度：先按各自的时间尺度回落到此刻，再让这次事件推一把。
    #    它们不是分数的换算，各走各的——所以"很久没联系但还很熟"和"刚吵完架但很信任"
    #    都能被表达出来，而一个数字做不到这件事。
    last = _parse_time(state.get("updatedAt"))
    violation = event.get("kind") in {"boundary", "bottom_line"}
    familiarity = _decayed(state["familiarity"], last, moment, FAMILIARITY_HALF_LIFE_DAYS)
    trust = _decayed(state["trust"], last, moment, TRUST_HALF_LIFE_DAYS)
    warmth = _decayed(state["warmth"], last, moment, WARMTH_HALF_LIFE_DAYS)
    # 每一次真实互动都让人更熟，哪怕这次不愉快——认识就是认识了。
    familiarity += FAMILIARITY_PER_EVENT
    if violation:
        trust -= TRUST_LOSS_PER_VIOLATION
        warmth -= WARMTH_LOSS_PER_VIOLATION
    elif delta > 0:
        trust += TRUST_PER_POSITIVE
        warmth += WARMTH_PER_POSITIVE
    familiarity, trust, warmth = _unit(familiarity), _unit(trust), _unit(warmth)

    updated_state = {
        "score": round(_clamp_score(new_score), 2),
        "stage": new_stage,
        "interaction": interaction_state_of(new_stage, recent_violation=violation, warmth=warmth),
        "familiarity": round(familiarity, 4),
        "trust": round(trust, 4),
        "warmth": round(warmth, 4),
        # 边界态只由用户明说决定，事件账本改不了它。
        "boundaryState": state["boundaryState"],
        "updatedAt": moment.isoformat(),
    }

    entry = {
        "eventKey": event_key,
        "kind": str(event.get("kind") or "interaction"),
        "delta": round(delta, 2),
        "solicited": solicited,
        "at": moment.isoformat(),
    }
    ledger.append(entry)
    if len(ledger) > LEDGER_MAX_ENTRIES:
        ledger = ledger[-LEDGER_MAX_ENTRIES:]

    return updated_state, ledger


def decay_state(state: dict, now: datetime | None = None) -> dict:
    """惰性降温：每个维度按自己的尺度向中性回归，谁都不会被推得离中性更远。

    负分不自动回弹：裂痕不会自己长好，只能靠修复事件补回来。降温从不越过中性，
    也从不朝远离中性的方向走。边界态不参与降温——用户划下的线不会因为时间到了就消失。
    """
    moment = now or datetime.now(timezone.utc)
    state = normalize_affinity_state(state)
    last = _parse_time(state.get("updatedAt"))
    score = _clamp_score(float(state.get("score") or 0.0))
    if last is not None and score > 0:
        score = _decay_toward_zero(score, last, moment)
    stage = stage_key_of(score)
    warmth = _unit(_decayed(state["warmth"], last, moment, WARMTH_HALF_LIFE_DAYS))
    return {
        "score": round(_clamp_score(score), 2),
        "stage": stage,
        "interaction": interaction_state_of(stage, warmth=warmth),
        "familiarity": round(_unit(_decayed(state["familiarity"], last, moment, FAMILIARITY_HALF_LIFE_DAYS)), 4),
        "trust": round(_unit(_decayed(state["trust"], last, moment, TRUST_HALF_LIFE_DAYS)), 4),
        "warmth": round(warmth, 4),
        "boundaryState": state["boundaryState"],
        "updatedAt": moment.isoformat(),
    }


def judge_affinity_event(user_text: str, stage: str, judge=None):
    """LLM 判定这轮互动让她感觉亲近还是疏远（心脏）；judge 为 None/失败回退到 regex。

    返回 (event_or_None, source)。event 形如 {eventKey, kind, delta}，delta 落在
    [-12, 12]；确定性账本（去重/上限/迟滞）仍然作为安全网在后面套。
    """
    fallback = derive_affinity_event(user_text)
    if judge is None:
        return fallback, "fallback"
    try:
        result, source = judge(
            task="判断这轮互动让陪伴者感觉亲近了还是疏远了",
            context={"userText": str(user_text or "")[:400], "stage": stage},
            fields={
                "delta": "-12 到 12 的整数：正=让她亲近，负=让她疏远，0=没感觉",
                "reason": "一句话说明你为什么会这么感觉",
            },
            fallback={"delta": float(fallback["delta"]) if fallback else 0.0, "reason": ""},
        )
    except Exception:
        return fallback, "fallback"
    try:
        delta = float(result.get("delta", 0.0))
    except (TypeError, ValueError):
        delta = 0.0
    delta = max(-12.0, min(12.0, delta))
    if delta == 0.0:
        return None, source
    kind = "warm" if delta > 0 else "boundary"
    return {
        "eventKey": f"llm:{kind}:{hashlib.sha256(str(user_text).encode('utf-8')).hexdigest()[:10]}",
        "kind": kind,
        "delta": delta,
    }, source
