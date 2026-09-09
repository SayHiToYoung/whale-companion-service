# -*- coding: utf-8 -*-
"""大鲸「何时主动开口」的确定性决策器。

从 Miru 的 AttentionEngine 抄来的骨架，按本项目哲学简化：

Miru 是「LLM 提议 + 确定性闸门否决」两层——Attention LLM 根据全部上下文
（屏幕变化、活动状态、时段、情绪、对话节奏、DDL、长期画像）判断说/不说，
三条硬规则再一票否决。本项目不额外调用 LLM 做判定：候选全部来自已有的、
可验证的状态（到期约定、仍开着的情绪线程、可报信的事实），闸门同样是
确定性硬规则。

三层结构：

1. 候选——只有「有据可依」的东西才配成为主动开口的理由，优先级：
   到期约定 > 情绪线程 > 今日事实报信。绝不凭空找话说。
2. 节律——主动最小间隔、聊天静默下限、免打扰时段。
3. 闸门——任何一条硬规则触发就一票否决，返回 vetoReason 供审计。

本模块是纯函数：信号由调用方（桌宠心跳、聊天时间戳、共享记忆）喂进来，
它只负责「此刻该不该开口、开什么口」。返回结构可直接作为
GET /v1/companion/proactive 的响应体。
"""

from __future__ import annotations

from datetime import datetime, timezone
import re

ACTIVE_MIN_SILENCE_MINUTES = 2.0        # 用户仍在聊天（静默 < 2 分钟）时不插话
PROACTIVE_MIN_INTERVAL_MINUTES = 15.0   # 两次主动至少隔 15 分钟
FOLLOW_UP_WEIGHT_FLOOR = 0.25           # 约定权重低于此值视为已过期，不再主动
CARRIED_WEIGHT_FLOOR = 0.25             # 情绪线程权重低于此值不再主动提起

# 关系越近，主动开口越放得开：间隔更短、情绪线程更容易触发主动。
_STAGE_MIN_INTERVAL_MINUTES = {"early": 45.0, "warming": 30.0, "familiar": 15.0}
_STAGE_CARRIED_FLOOR = {"early": 0.6, "warming": 0.4, "familiar": 0.25}

# 免打扰时段：深夜默认不主动，除非有到期约定要兑现。
_QUIET_PERIODS = ("late_night",)


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _minutes_since(value: object, now: datetime) -> float | None:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds() / 60.0)


def _due_follow_up(follow_ups: list[dict]) -> dict | None:
    """第一个还新鲜、值得兑现的约定。"""
    for row in follow_ups:
        if float(row.get("weight") or 0.0) >= FOLLOW_UP_WEIGHT_FLOOR and str(row.get("hint") or row.get("topic") or "").strip():
            return row
    return None


def _veto(reason: str, checked_at: str) -> dict:
    return {"shouldSpeak": False, "vetoReason": reason, "checkedAt": checked_at}


def _speak(kind: str, reason: str, content: str, checked_at: str) -> dict:
    return {
        "shouldSpeak": True,
        "kind": kind,
        "reason": reason,
        "content": content,
        "checkedAt": checked_at,
    }


def decide_proactive_speak(
    *,
    now: datetime | None = None,
    last_spoke_at: object = None,
    last_user_message_at: object = None,
    period: str = "daytime",
    activity: str = "idle",
    pending_follow_ups: list[dict] | None = None,
    carried_emotion: str = "",
    carried_weight: float = 0.0,
    opening: str = "",
    relationship_stage: str = "familiar",
) -> dict:
    """判断大鲸此刻是否应该主动开口，以及开口的依据。

    信号全部可选：缺哪个就按「不知道就不硬拦」处理——但候选内容必须有据。
    """
    moment = now or datetime.now(timezone.utc)
    checked_at = moment.isoformat()
    follow_ups = [row for row in (pending_follow_ups or []) if isinstance(row, dict)]
    due = _due_follow_up(follow_ups)
    min_interval = _STAGE_MIN_INTERVAL_MINUTES.get(relationship_stage, PROACTIVE_MIN_INTERVAL_MINUTES)
    carried_floor = _STAGE_CARRIED_FLOOR.get(relationship_stage, CARRIED_WEIGHT_FLOOR)

    # 闸门：硬规则，一条触发即否决（顺序即优先级）。
    silence = _minutes_since(last_user_message_at, moment)
    if activity == "active" and silence is not None and silence < ACTIVE_MIN_SILENCE_MINUTES:
        return _veto("user_still_chatting", checked_at)
    since_spoke = _minutes_since(last_spoke_at, moment)
    if since_spoke is not None and since_spoke < min_interval:
        return _veto("too_soon", checked_at)
    if period in _QUIET_PERIODS and due is None:
        return _veto("quiet_hours", checked_at)

    # 候选：按优先级取第一个有据可依的开口理由。
    if due:
        return _speak("follow_up", "due_follow_up", str(due.get("hint") or due.get("topic") or ""), checked_at)
    if carried_emotion and carried_weight >= carried_floor:
        return _speak("check_in", "open_emotion_thread", carried_emotion, checked_at)
    if opening:
        return _speak("observation", "grounded_opening", opening, checked_at)
    return _veto("nothing_grounded", checked_at)


# ============================================================================
# 主动候选生命周期（升级版）
#
# 把上面的「3 候选源 + 3 闸门」扩展为「多候选源 → 评分 → 完整闸门链 → 审计」。
# 候选是结构化对象：来源、类型、理由、正文、话题、动机、价值分量、去重签名。
# 闸门覆盖：关系边界、互动收敛、用户仍在聊天、最小间隔、免打扰、每日上限、
# 未回应降速、用户休息、Token 硬限额。命中任一硬闸门即否决并留 vetoReason。
# ============================================================================

# 候选来源基础分：有据可依、越紧急越高。
CANDIDATE_BASE_SCORE = {
    "follow_up": 90.0,        # 到期约定：用户亲口交代，最该兑现
    "open_emotion": 75.0,     # 未闭合情绪线程
    "grounded_opening": 70.0, # 今日有据事实报信
    "agenda_share": 55.0,     # 生活契机（日程段）
    "story_beat": 52.0,       # 故事走到了新的一格
    "memory_echo": 50.0,      # 记忆回响
    "mood_checkin": 45.0,     # 心情问候
    "daily_greeting": 40.0,   # 早晚安锚点
    "meal_care": 35.0,        # 饭点关心
}

# 8 档好感阶段 → 主动策略：allow 决定是否允许主动，interval 是最小间隔（分钟）。
_STAGE_PROACTIVE = {
    "deeply_distant": {"allow": False, "interval": None},
    "strongly_distant": {"allow": False, "interval": None},
    "distant": {"allow": False, "interval": None},
    "acquaintance": {"allow": True, "interval": 45.0},
    "familiar": {"allow": True, "interval": 30.0},
    "close": {"allow": True, "interval": 20.0},
    "intimate": {"allow": True, "interval": 15.0},
    "deeply_bonded": {"allow": True, "interval": 15.0},
}

# 未回应降速：连续 N 次主动无回应后暂停主动。
NO_RESPONSE_PAUSE_AFTER = 3

# 哪些候选归"主动关心"额度管，哪些要"允许引用共同记忆"才能提。
# 分开是必要的：她分享自己今天做了什么，和她主动来关心你，不是同一件事，
# 也不该被同一个权限一起关掉。
_CARE_KINDS = frozenset({"daily_greeting", "meal_care", "mood_checkin"})
# 共同故事讲的就是你们之间的经历，所以它和记忆回响受同一条许可管。
_SHARED_MEMORY_KINDS = frozenset({"memory_echo", "open_emotion", "story_beat"})

# 候选价值下限：低于这个分数的念头不值得打断用户，直接弃掉而不是排队等。
MIN_CANDIDATE_SCORE = 30.0

# 候选状态机。sent 是终点：说过的话不再第二次冒出来。
CANDIDATE_STATUSES = ("queued", "deferred", "sent", "dropped", "expired")

# 时间窗过了就作废的否决理由，与"暂时不合适"分开——前者不该再等。
_DROP_REASONS = frozenset({
    "user_boundary", "relationship_boundary", "nothing_grounded", "window_expired", "low_value",
    "care_not_permitted", "shared_memory_not_permitted",
})


def _window_state(candidate: dict, now: datetime) -> str:
    """候选的有效时间窗：还没到 / 正当时 / 已经过去。"""
    starts = _parse_time(candidate.get("windowStart"))
    expires = _parse_time(candidate.get("expiresAt"))
    if expires is not None and now >= expires:
        return "expired"
    if starts is not None and now < starts:
        return "not_open"
    return "open"


def build_proactive_candidates(
    *,
    pending_follow_ups: list[dict] | None = None,
    carried_emotion: str = "",
    carried_weight: float = 0.0,
    opening: str = "",
    daily_greeting: str = "",
    meal_care: str = "",
    agenda_shares: list[dict] | None = None,
    memory_echo: str = "",
    mood_checkin: str = "",
    signals: list[dict] | None = None,
) -> list[dict]:
    """把所有有据可依的信号归一化成候选列表。

    每个候选带 source/kind/reason/content/topic/motive/signature/salience/warmth/urgency。
    """
    candidates: list[dict] = []

    def add(kind: str, *, reason: str, content: str, topic: str, motive: str, salience: float = 0.5,
            warmth: float = 0.3, urgency: float = 0.3, signature: str = "", window: dict | None = None) -> None:
        if not str(content or "").strip():
            return
        bounds = window or {}
        candidates.append({
            "source": kind,
            "kind": kind,
            "reason": reason,
            "content": str(content)[:200],
            "topic": str(topic)[:160],
            "motive": motive,
            "signature": signature or f"{kind}:{str(content)[:80]}",
            "salience": salience,
            "warmth": warmth,
            "urgency": urgency,
            # 时间窗是可选的：约定和情绪线程没有"过点就作废"这回事，
            # 早安和饭点关心有——晚了三个小时再说就不是关心了。
            "windowStart": str(bounds.get("windowStart") or ""),
            "preferredAt": str(bounds.get("preferredAt") or ""),
            "bestUntil": str(bounds.get("bestUntil") or ""),
            "expiresAt": str(bounds.get("expiresAt") or ""),
        })

    # 优先级靠前的来源先入（同分时稳定排序）。
    for row in (pending_follow_ups or []):
        if float(row.get("weight") or 0.0) >= FOLLOW_UP_WEIGHT_FLOOR:
            hint = str(row.get("hint") or row.get("topic") or "").strip()
            add("follow_up", reason="due_follow_up", content=hint, topic=hint,
                motive="兑现用户亲口交代的约定", salience=1.0, urgency=0.9, warmth=0.6)
    if carried_emotion and carried_weight >= CARRIED_WEIGHT_FLOOR:
        add("open_emotion", reason="open_emotion_thread", content=carried_emotion, topic=carried_emotion,
            motive="上次那件事还没说完", salience=0.8, warmth=0.7, urgency=0.5)
    if opening:
        add("grounded_opening", reason="grounded_opening", content=opening, topic=opening,
            motive="分享有据可依的今日观察", salience=0.7, warmth=0.4, urgency=0.4)
    for share in (agenda_shares or []):
        if isinstance(share, dict):
            add("agenda_share", reason="life_opportunity", content=str(share.get("topic") or ""),
                topic=str(share.get("topic") or ""), motive=str(share.get("motive") or "分享此刻的状态"),
                salience=0.4, warmth=0.4, urgency=0.2)
    if memory_echo:
        add("memory_echo", reason="memory_echo", content=memory_echo, topic=memory_echo,
            motive="自然想起一件共同的事", salience=0.4, warmth=0.5, urgency=0.2)
    if mood_checkin:
        add("mood_checkin", reason="mood_checkin", content=mood_checkin, topic=mood_checkin,
            motive="关心一下心情", salience=0.3, warmth=0.6, urgency=0.2)
    if daily_greeting:
        add("daily_greeting", reason="daily_anchor", content=daily_greeting, topic=daily_greeting,
            motive="按生活节律打个招呼", salience=0.2, warmth=0.4, urgency=0.2)
    if meal_care:
        add("meal_care", reason="meal_care", content=meal_care, topic=meal_care,
            motive="到饭点关心一下", salience=0.2, warmth=0.5, urgency=0.1)
    # 带时间窗的成形信号（日程模块产出的就是这种）。它们只是候选，发不发在下面决定。
    for signal in (signals or []):
        if not isinstance(signal, dict):
            continue
        kind = str(signal.get("kind") or "")
        if kind not in CANDIDATE_BASE_SCORE:
            continue
        content = str(signal.get("content") or signal.get("topic") or "")
        add(kind, reason=str(signal.get("reason") or kind), content=content,
            topic=str(signal.get("topic") or content), motive=str(signal.get("motive") or ""),
            salience=float(signal.get("salience") or 0.4), warmth=float(signal.get("warmth") or 0.4),
            urgency=float(signal.get("urgency") or 0.2), signature=str(signal.get("signature") or ""),
            window=signal)
    return candidates


def score_candidate(candidate: dict, age_minutes: float = 0.0) -> float:
    """候选价值分 = 基础分 + 价值分量加权 - 年龄衰减。"""
    base = CANDIDATE_BASE_SCORE.get(str(candidate.get("kind") or ""), 40.0)
    salience = float(candidate.get("salience") or 0.0)
    warmth = float(candidate.get("warmth") or 0.0)
    urgency = float(candidate.get("urgency") or 0.0)
    age_penalty = max(0.0, age_minutes) * 0.01
    return round(base + urgency * 1.0 + warmth * 0.5 + salience * 0.5 - age_penalty, 2)


def apply_proactive_gates(
    candidates: list[dict],
    *,
    activity: str = "idle",
    silence_minutes: float | None = None,
    since_spoke_minutes: float | None = None,
    period: str = "daytime",
    relationship_stage: str = "familiar",
    interaction_state: str = "relaxed",
    daily_sent: int = 0,
    daily_limit: int = 8,
    no_response_streak: int = 0,
    rest_until: str = "",
    token_remaining: float = float("inf"),
    min_interval_minutes: float = PROACTIVE_MIN_INTERVAL_MINUTES,
    min_score: float = MIN_CANDIDATE_SCORE,
    permissions: dict | None = None,
    care_sent: int = 0,
    now: datetime | None = None,
) -> list[dict]:
    """按固定顺序过闸门，命中即给候选附 vetoReason；返回全部候选（未否决的排在前面）。

    顺序就是需求里的那条链：用户情境 → 免打扰 → 每日上限 → 最小间隔 → 未回应次数 →
    关系权限 → 候选价值 → 有效时间窗。它是唯一的发送授权入口，任何模块都不能绕过它。
    """
    moment = now or datetime.now(timezone.utc)
    stage_policy = _STAGE_PROACTIVE.get(relationship_stage, _STAGE_PROACTIVE["familiar"])
    interval = max(stage_policy["interval"] or 0.0, min_interval_minutes)
    checked_at = moment.isoformat()

    has_due_followup = any(c["kind"] == "follow_up" for c in candidates)

    def veto(candidate: dict, reason: str) -> dict:
        candidate["vetoReason"] = reason
        candidate["checkedAt"] = checked_at
        return candidate

    rest = _parse_time(rest_until)
    gated: list[dict] = []
    for candidate in candidates:
        # 闸门顺序即优先级，每一条都是硬规则，命中即否决并留下 vetoReason。
        # 1. 用户情境：她在跟人说话，或用户明说了在休息。
        if activity == "active" and silence_minutes is not None and silence_minutes < ACTIVE_MIN_SILENCE_MINUTES:
            gated.append(veto(candidate, "user_still_chatting"))
            continue
        if rest is not None and moment < rest:
            gated.append(veto(candidate, "user_resting"))
            continue
        # 2. 免打扰（到期约定例外：那是用户亲口交代要提醒的）。
        if period in _QUIET_PERIODS and not has_due_followup:
            gated.append(veto(candidate, "quiet_hours"))
            continue
        # 3. 每日上限。
        if daily_sent >= daily_limit:
            gated.append(veto(candidate, "daily_cap"))
            continue
        # 4. 最小间隔。
        if since_spoke_minutes is not None and since_spoke_minutes < interval:
            gated.append(veto(candidate, "too_soon"))
            continue
        # 5. 用户未回应次数：连着说了几次没人理，就先停下。
        if no_response_streak >= NO_RESPONSE_PAUSE_AFTER:
            gated.append(veto(candidate, "no_response_pause"))
            continue
        # 6. 关系权限：疏离阶段不允许主动；受伤/回避时她自己也不想开口。
        if not stage_policy["allow"]:
            gated.append(veto(candidate, "relationship_boundary"))
            continue
        if interaction_state in {"hurt", "avoidant"}:
            gated.append(veto(candidate, "interaction_converged"))
            continue
        # 6b. 逐类权限：关心和提共同记忆各自需要各自的许可，额度也各自计。
        kind = str(candidate.get("kind") or "")
        if permissions is not None:
            if kind in _CARE_KINDS:
                if not permissions.get("allowProactiveCare"):
                    gated.append(veto(candidate, "care_not_permitted"))
                    continue
                if care_sent >= int(permissions.get("proactiveQuota") or 0):
                    gated.append(veto(candidate, "care_quota_used"))
                    continue
            if kind in _SHARED_MEMORY_KINDS and not permissions.get("allowSharedMemory"):
                gated.append(veto(candidate, "shared_memory_not_permitted"))
                continue
        # 7. 候选价值：不值得打断用户的念头就别排队了。
        if float(candidate.get("score") or 0.0) < min_score:
            gated.append(veto(candidate, "low_value"))
            continue
        # 8. 有效时间窗：还没到就等，过去了就作废。
        window = _window_state(candidate, moment)
        if window == "not_open":
            gated.append(veto(candidate, "window_not_open"))
            continue
        if window == "expired":
            gated.append(veto(candidate, "window_expired"))
            continue
        # 9. Token 硬限额（系统预算，与用户无关，放在最后）。
        if token_remaining <= 0:
            gated.append(veto(candidate, "token_hard_limit"))
            continue
        candidate["vetoReason"] = ""
        candidate["checkedAt"] = checked_at
        gated.append(candidate)
    gated.sort(key=lambda c: bool(c.get("vetoReason")))
    return gated


def reconcile_candidates(
    fresh: list[dict], stored: list[dict] | None = None, *, now: datetime | None = None,
) -> list[dict]:
    """把这一轮新算出来的候选和上一轮留下来的合成一个队列。

    合并按 signature，这是防重复的那道锁：
    - 已经说出口的（`sent`）永远不再回到队列。同一个念头不会因为轮询而说第二遍。
    - 还在队列里的保留它**第一次出现的时间**。延后了多久、还剩多少价值、窗口过没过，
      都得从那一刻算起，否则每轮重算等于把年龄清零，一条候选可以永远不老。
    - 上一轮留下、这一轮没有依据的：有时间窗的留到窗口过期，没时间窗的直接消失——
      依据没了就是没了，不能靠惯性继续排队。
    """
    moment = now or datetime.now(timezone.utc)
    at = moment.isoformat()
    stored_by_signature = {
        str(row.get("signature") or ""): row
        for row in (stored or []) if isinstance(row, dict) and row.get("signature")
    }
    queue: list[dict] = []
    seen: set[str] = set()
    for candidate in fresh:
        signature = str(candidate.get("signature") or "")
        previous = stored_by_signature.get(signature)
        if previous is not None and str(previous.get("status") or "") == "sent":
            continue
        if signature in seen:
            continue
        seen.add(signature)
        row = dict(candidate)
        row["createdAt"] = str((previous or {}).get("createdAt") or candidate.get("createdAt") or at)
        row["status"] = str((previous or {}).get("status") or "queued")
        row["deliverCount"] = int((previous or {}).get("deliverCount") or 0)
        queue.append(row)
    for signature, previous in stored_by_signature.items():
        if signature in seen or str(previous.get("status") or "") == "sent":
            continue
        row = dict(previous)
        if _window_state(row, moment) == "expired":
            row["status"] = "expired"
            queue.append(row)
            continue
        if not str(row.get("expiresAt") or ""):
            continue
        queue.append(row)
    for row in queue:
        created = _parse_time(row.get("createdAt")) or moment
        row["ageMinutes"] = round(max(0.0, (moment - created).total_seconds() / 60.0), 2)
        row["score"] = score_candidate(row, row["ageMinutes"])
    return queue


def evaluate_proactive_lifecycle(
    *,
    activity: str = "idle",
    period: str = "daytime",
    relationship_stage: str = "familiar",
    interaction_state: str = "relaxed",
    daily_sent: int = 0,
    daily_limit: int = 8,
    no_response_streak: int = 0,
    rest_until: str = "",
    token_remaining: float = float("inf"),
    min_interval_minutes: float = PROACTIVE_MIN_INTERVAL_MINUTES,
    min_score: float = MIN_CANDIDATE_SCORE,
    permissions: dict | None = None,
    care_sent: int = 0,
    last_spoke_at: object = None,
    last_user_message_at: object = None,
    now: datetime | None = None,
    boundaries: list[dict] | None = None,
    stored_candidates: list[dict] | None = None,
    **candidate_signals: object,
) -> dict:
    """完整主动生命周期：构建候选 → 评分 → 排序 → 过闸门 → 选最优。

    返回 {shouldSpeak, selected, candidates, audit}，candidates 带 score/vetoReason 供审计。
    """
    moment = now or datetime.now(timezone.utc)
    checked_at = moment.isoformat()

    candidates = reconcile_candidates(
        build_proactive_candidates(**candidate_signals), stored_candidates, now=moment)
    silence = _minutes_since(last_user_message_at, moment)
    since_spoke = _minutes_since(last_spoke_at, moment)

    gated = apply_proactive_gates(
        candidates,
        activity=activity,
        silence_minutes=silence,
        since_spoke_minutes=since_spoke,
        period=period,
        relationship_stage=relationship_stage,
        interaction_state=interaction_state,
        daily_sent=daily_sent,
        daily_limit=daily_limit,
        no_response_streak=no_response_streak,
        rest_until=rest_until,
        token_remaining=token_remaining,
        min_interval_minutes=min_interval_minutes,
        min_score=min_score,
        permissions=permissions,
        care_sent=care_sent,
        now=moment,
    )

    # User boundaries outrank every candidate source and relationship permission.
    for candidate in gated:
        for boundary in boundaries or []:
            if boundary.get("status", "active") != "active":
                continue
            rule = str(boundary.get("rule") or "")
            terms = list(boundary.get("blockedTerms") or [])
            if boundary.get("kind") in {"topic_suppression", "addressing"}:
                terms.extend(re.findall(r"[“「](.*?)[”」]", rule))
            if (boundary.get("blockProactive") or re.search(r"(?:不要|别).*主动|免打扰", rule)
                    or any(term and term in candidate.get("content", "") for term in terms)):
                candidate["vetoReason"] = "user_boundary"
                break
    viable = [c for c in gated if not c.get("vetoReason")]
    # 同分时按 signature 排序，让同一批候选在任何机器上选出同一条。
    viable.sort(key=lambda c: (-float(c.get("score") or 0.0), str(c.get("signature") or "")))
    selected = viable[0] if viable else None
    for candidate in gated:
        reason = str(candidate.get("vetoReason") or "")
        candidate["decision"] = (
            "send" if candidate is selected else
            "drop" if reason in _DROP_REASONS else "defer"
        )

    if selected is None:
        veto_reason = gated[0].get("vetoReason", "nothing_grounded") if gated else "nothing_grounded"
        return {
            "shouldSpeak": False,
            "decision": "drop" if not gated or all(c["decision"] == "drop" for c in gated) else "defer",
            "selected": None,
            "vetoReason": veto_reason,
            "checkedAt": checked_at,
            "candidates": gated,
        }
    return {
        "shouldSpeak": True,
        "decision": "send",
        "selected": selected,
        "vetoReason": "",
        "checkedAt": checked_at,
        "candidates": gated,
    }


def proactive_policy_fragment(relationship: dict, *, now):
    """Read projection only; candidate scoring and send authority stay in this module."""
    from .context_contract import ContextFact, ContextFragment, Priority
    stage = str(relationship.get("stage") or "acquaintance")
    policy = _STAGE_PROACTIVE.get(stage, _STAGE_PROACTIVE["acquaintance"])
    permissions = relationship.get("permissions") or {}
    return ContextFragment(module="proactivePolicy", priority=Priority.RELATIONSHIP,
        facts=(ContextFact("proactivePolicy", {
            "decisionOwner": "proactive", "allowedActions": ["send", "defer", "drop"],
            "modulesMaySend": False, "relationshipAllowsProactive": bool(policy["allow"]),
            "careAllowed": bool(permissions.get("allowProactiveCare")),
            "careQuota": int(permissions.get("proactiveQuota") or 0),
        }),),
        instructions=("主动信号只能形成候选；是否发送由确定性 proactive 决策器决定。",),
        source_event_ids=("policy:proactive:" + stage,), created_at=now.isoformat())


def judge_proactive_decision(viable_candidates: list[dict], state_context: dict, judge=None):
    """LLM 人格判定：确定性闸门放行后，由她「自己决定」要不要开口、开什么口。

    judge 为 None/失败时回退到确定性选优（取最高分候选的正文）。
    返回 (decision, source)；decision = {shouldSpeak, kind, content} 或
    {shouldSpeak: False, reason: "llm_deferred"}。
    """
    if not viable_candidates:
        return {"shouldSpeak": False, "reason": "nothing_grounded"}, "fallback"
    best = max(viable_candidates, key=lambda c: float(c.get("score") or 0.0))
    fallback = {"shouldSpeak": True, "kind": best.get("kind"), "content": best.get("content")}
    if judge is None:
        return fallback, "fallback"
    candidate_view = [
        {"kind": c.get("kind"), "reason": c.get("reason"), "content": c.get("content"), "score": c.get("score")}
        for c in viable_candidates[:5]
    ]
    try:
        result, source = judge(
            task="作为陪伴者，判断自己此刻是否真的想主动开口，以及想说什么",
            context={"state": state_context, "candidates": candidate_view},
            fields={
                "shouldSpeak": "布尔：是否真的想主动开口",
                "kind": "如果开口，选哪个候选的 kind（照抄 candidates 里的 kind）",
                "content": "如果开口，她想说的话（一句，符合她的口气）",
            },
            fallback={"shouldSpeak": True, "kind": fallback["kind"], "content": fallback["content"]},
        )
    except Exception:
        return fallback, "fallback"
    if result.get("shouldSpeak") is False:
        return {"shouldSpeak": False, "reason": "llm_deferred"}, source
    content = str(result.get("content") or "").strip()
    kind = str(result.get("kind") or best.get("kind"))
    if not content:
        return fallback, source
    return {"shouldSpeak": True, "kind": kind, "content": content[:200]}, source
