# -*- coding: utf-8 -*-
"""大鲸的生活连续性：时型、每日状态、日程与临近细化。

从 astrbot_plugin_private_companion 的 daily_state / chronotype / agenda 提取，
按本项目哲学精简：

1. 每日状态不是固定字段，而是由「状态条件 condition」动态合成：
   energy = 75 + Σ(condition.energy_delta)，mood_bias 取非平稳且强度最高的 mood。
2. 时型（chronotype）只决定「现在是活跃窗还是睡眠窗」，并据此切时段；
   不预测用户作息，默认锚点 7:30 醒 / 22:30 睡。
3. 日程分三层：固定节律（只由 chronotype 锚点决定）→ 每日日程（当日种子给出这一天的
   具体安排）→ 临时生活事件（会出现也会过去的小事，只在自己的时间窗里存在）。
4. 日程只产出**结构化信号**：给模型的是处理过的 ContextFragment，给主动链路的是带时间窗的
   候选信号。**本模块永远不发送消息**——它连发送所需的依赖都没有。发不发由 proactive 决定。
5. 纯确定性：本模块不额外调用 LLM。所有信号可由日期种子复现，便于测试与审计。

存储由调用方持有（daily_state / chronotype_profile / daily_plan 表），本模块是纯函数。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

# 默认作息锚点（分钟自午夜）。
DEFAULT_WAKE_MINUTE = 7 * 60 + 30     # 07:30
DEFAULT_SLEEP_MINUTE = 22 * 60 + 30   # 22:30

# 精力基值与边界。
ENERGY_BASE = 75.0
ENERGY_MIN = 10.0
ENERGY_MAX = 100.0

# 时段按 chronotype 切分（相对唤醒/入睡锚点）。
_MORNING_SPAN_MINUTES = 3 * 60 + 30   # 醒后 3.5h 内算 morning
_EVENING_SPAN_MINUTES = 4 * 60        # 睡前 4h 内算 evening


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _minutes_of_day(now: datetime) -> int:
    return now.hour * 60 + now.minute


# ---------------------------------------------------------------------------
# 时型 chronotype
# ---------------------------------------------------------------------------

def empty_chronotype() -> dict:
    """默认时型：未学习，7:30 醒 / 22:30 睡，零置信。"""
    return {
        "wakeMinute": DEFAULT_WAKE_MINUTE,
        "sleepMinute": DEFAULT_SLEEP_MINUTE,
        "source": "default",
        "confidence": 0.0,
        "shiftMinutes": 0,
        "hourHistogram": [0] * 24,
    }


def _wake_minute(chronotype: dict) -> int:
    return max(0, min(24 * 60 - 1, int(chronotype.get("wakeMinute") or DEFAULT_WAKE_MINUTE)))


def _sleep_minute(chronotype: dict) -> int:
    return max(0, min(24 * 60 - 1, int(chronotype.get("sleepMinute") or DEFAULT_SLEEP_MINUTE)))


def period_of(now: datetime | None = None, chronotype: dict | None = None) -> str:
    """按 chronotype 切时段：late_night / morning / daytime / evening。

    睡眠窗（sleep ~ 次日 wake，跨午夜）判为 late_night。
    """
    moment = now or datetime.now(timezone.utc)
    profile = chronotype if isinstance(chronotype, dict) else empty_chronotype()
    wake = _wake_minute(profile)
    sleep = _sleep_minute(profile)
    minute = _minutes_of_day(moment)
    if sleep <= minute or minute < wake:
        return "late_night"
    if minute < wake + _MORNING_SPAN_MINUTES:
        return "morning"
    if minute >= sleep - _EVENING_SPAN_MINUTES:
        return "evening"
    return "daytime"


def is_active_window(now: datetime | None = None, chronotype: dict | None = None) -> bool:
    """是否在活跃窗（非睡眠窗）。"""
    return period_of(now, chronotype) != "late_night"


# ---------------------------------------------------------------------------
# 每日状态条件
# ---------------------------------------------------------------------------

# 睡眠条件池：(label, mood, energy_delta, intensity)。
_SLEEP_POOL = (
    ("睡得挺沉", "", 12.0, 0.8),
    ("睡得一般", "", 4.0, 0.5),
    ("没睡够", "有点困", -8.0, 0.6),
)
# 梦境条件池（只有小概率出现，不编造具体内容）。
_DREAM_POOL = (
    (None, None, None, None),   # 多数时候没有梦
    ("做了个记不清的梦", "有点恍惚", -2.0, 0.3),
    ("睡了个安稳觉没做梦", "", 2.0, 0.3),
)


def _date_seed(now: datetime) -> int:
    """同一天稳定、跨天变化的种子。"""
    return int(now.strftime("%Y%m%d"))


def generate_daily_conditions(now: datetime | None = None, chronotype: dict | None = None) -> list[dict]:
    """当日首次生成的基线条件：睡眠 + 可能出现的梦境。

    确定性：同一天生成同样的结果，便于审计与测试。
    """
    moment = now or datetime.now(timezone.utc)
    rng = random.Random(_date_seed(moment))
    sleep_label, sleep_mood, sleep_delta, sleep_intensity = rng.choice(_SLEEP_POOL)
    conditions = [{
        "kind": "sleep",
        "title": "昨夜睡眠",
        "label": sleep_label,
        "mood": sleep_mood,
        "energyDelta": sleep_delta,
        "intensity": sleep_intensity,
        "cause": "昨夜睡眠",
        "phase": "active",
    }]
    dream_label, dream_mood, dream_delta, dream_intensity = rng.choice(_DREAM_POOL)
    if dream_label is not None:
        conditions.append({
            "kind": "dream",
            "title": "昨夜梦境",
            "label": dream_label,
            "mood": dream_mood,
            "energyDelta": dream_delta,
            "intensity": dream_intensity,
            "cause": "昨夜梦境",
            "phase": "active",
        })
    return conditions


def meal_hunger_condition(now: datetime | None = None, chronotype: dict | None = None) -> dict | None:
    """到饭点窗口时注入的饥饿条件；未到饭点返回 None。"""
    moment = now or datetime.now(timezone.utc)
    minute = _minutes_of_day(moment)
    profile = chronotype if isinstance(chronotype, dict) else empty_chronotype()
    wake = _wake_minute(profile)
    # 简化饭点：醒后 1h 早餐、醒后 5h 午餐、睡后 1h 晚餐，各 60 分钟窗。
    breakfast = wake + 60
    lunch = wake + 5 * 60
    dinner = _sleep_minute(profile) + 60
    for anchor in (breakfast, lunch, dinner):
        if anchor <= minute < anchor + 60:
            name = {breakfast: "早饭", lunch: "午饭", dinner: "晚饭"}[anchor]
            return {
                "kind": "hunger",
                "title": f"想吃{name}",
                "label": "有点饿",
                "mood": "",
                "energyDelta": -6.0,
                "intensity": 0.6,
                "cause": "到饭点了",
                "phase": "active",
            }
    return None


# ---------------------------------------------------------------------------
# 每日状态合成
# ---------------------------------------------------------------------------

def compose_daily_state(conditions: list[dict], now: datetime | None = None, chronotype: dict | None = None) -> dict:
    """由条件合成当日状态：energy 累加、mood_bias 取最强非平稳情绪。"""
    moment = now or datetime.now(timezone.utc)
    energy = ENERGY_BASE
    mood_bias = ""
    mood_intensity = 0.0
    for condition in conditions:
        if not isinstance(condition, dict):
            continue
        energy += float(condition.get("energyDelta") or 0.0)
        mood = str(condition.get("mood") or "").strip()
        intensity = float(condition.get("intensity") or 0.0)
        if mood and intensity > mood_intensity:
            mood_bias = mood
            mood_intensity = intensity
    energy = max(ENERGY_MIN, min(ENERGY_MAX, energy))
    return {
        "date": moment.strftime("%Y-%m-%d"),
        "energy": round(energy, 1),
        "moodBias": mood_bias,
        "period": period_of(moment, chronotype),
        "activeWindow": is_active_window(moment, chronotype),
        "conditions": [dict(c) for c in conditions if isinstance(c, dict)],
    }


def daily_life_context(daily_state: dict, chronotype: dict | None = None, now: datetime | None = None) -> dict:
    """给模型看的形态：只描述「她现在的状态」，附「别拿来编故事」的约束。"""
    state = daily_state if isinstance(daily_state, dict) else {}
    return {
        "usage": "你此刻的身体状态和生活节律，只能影响语气，不要主动复述，更不要用它编造你没经历过的事。",
        "energy": float(state.get("energy") or ENERGY_BASE),
        "moodBias": str(state.get("moodBias") or ""),
        "period": str(state.get("period") or period_of(now, chronotype)),
        "activeWindow": is_active_window(now, chronotype),
    }


# ---------------------------------------------------------------------------
# 日程与临近细化
# ---------------------------------------------------------------------------

def default_daily_plan(chronotype: dict | None = None) -> list[dict]:
    """默认生活节奏模板：按 chronotype 锚点排一天的段。"""
    profile = chronotype if isinstance(chronotype, dict) else empty_chronotype()
    wake = _wake_minute(profile)
    sleep = _sleep_minute(profile)

    def _hhmm(minute: int) -> str:
        minute = minute % (24 * 60)
        return f"{minute // 60:02d}:{minute % 60:02d}"

    return [
        {"start": _hhmm(wake), "end": _hhmm(wake + 60), "activity": "起床收拾", "mood": "有点懒", "messageSeed": "刚醒，还不太想说话"},
        {"start": _hhmm(wake + 60), "end": _hhmm(wake + 5 * 60), "activity": "上午的日常", "mood": "平稳", "messageSeed": "上午有一搭没一搭地过着"},
        {"start": _hhmm(wake + 5 * 60), "end": _hhmm(wake + 6 * 60), "activity": "吃午饭", "mood": "满足", "messageSeed": "吃饱了想眯一会儿"},
        {"start": _hhmm(wake + 6 * 60), "end": _hhmm(sleep - 4 * 60), "activity": "下午的日常", "mood": "平稳", "messageSeed": "下午慢慢悠悠的"},
        {"start": _hhmm(sleep - 4 * 60), "end": _hhmm(sleep), "activity": "傍晚放松", "mood": "放松", "messageSeed": "天快黑了，人也懒下来"},
    ]


def current_segment(plan: list[dict], now: datetime | None = None, chronotype: dict | None = None) -> dict | None:
    """当前时段命中的日程段。"""
    moment = now or datetime.now(timezone.utc)
    minute = _minutes_of_day(moment)
    for segment in plan:
        if not isinstance(segment, dict):
            continue
        start = _parse_hhmm(segment.get("start"))
        end = _parse_hhmm(segment.get("end"))
        if start is None or end is None:
            continue
        if end > start:
            hit = start <= minute < end
        else:  # 跨午夜
            hit = minute >= start or minute < end
        if hit:
            return segment
    return None


def _parse_hhmm(value: object) -> int | None:
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return None


def refine_segment(segment: dict | None, daily_state: dict | None = None) -> dict:
    """临近细化的确定性版：从日程段 + 状态推导结构化信号，不生成散文。

    返回 {summary, location, stateVariables, todayEvents, proactiveEvents}，
    其中 todayEvents / proactiveEvents 是「可分享碎片 / 主动契机」的结构化描述，
    交给主 LLM 决定要不要说、怎么说（或保持静默）。
    """
    if not isinstance(segment, dict):
        return {"summary": "", "location": "", "stateVariables": [], "todayEvents": [], "proactiveEvents": []}
    state = daily_state if isinstance(daily_state, dict) else {}
    activity = str(segment.get("activity") or "")
    message_seed = str(segment.get("messageSeed") or "")
    mood = str(segment.get("mood") or "")
    energy = float(state.get("energy") or ENERGY_BASE)
    mood_bias = str(state.get("moodBias") or "")
    state_variables = [{"name": "精力", "value": f"{energy:.0f}", "note": "由睡眠/饥饿等条件合成"}]
    if mood_bias:
        state_variables.append({"name": "心情底色", "value": mood_bias, "note": "来自今日状态条件"})
    return {
        "summary": f"{activity}（{mood}）",
        "location": "",  # 无观测时不编造地点
        "stateVariables": state_variables,
        "todayEvents": [
            {
                "window": f"{segment.get('start')}-{segment.get('end')}",
                "event": message_seed,
                "mood": mood,
                "basis": "日程模板",
                "confidence": "low",
            }
        ],
        "proactiveEvents": [
            {
                "window": f"{segment.get('start')}-{segment.get('end')}",
                "reason": "生活契机",
                "action": "share",
                "why": f"到了「{activity}」的时段",
                "topic": message_seed,
                "motive": "想和你分享这会儿的状态",
                "scene": "casual",
                "tone": "松弛",
            }
        ],
    }


def judge_refinement(segment: dict | None, daily_state: dict | None = None, judge=None):
    """LLM 生成当天生活的「细化」（心脏）；judge 为 None/失败回退到模板。

    返回 (refined, source)。refined 结构同 refine_segment。
    """
    fallback = refine_segment(segment, daily_state)
    if judge is None or segment is None:
        return fallback, "fallback"
    state = daily_state if isinstance(daily_state, dict) else {}
    try:
        result, source = judge(
            task="生成陪伴者当前这一天的生活细节：她此刻在做什么、有什么想分享的",
            context={
                "segment": segment,
                "energy": state.get("energy"),
                "moodBias": state.get("moodBias"),
            },
            fields={
                "summary": "一句话：她此刻的生活状态",
                "location": "她此刻在哪（没依据就空字符串，别编造）",
                "todayEvent": "一件今天发生、可以分享的具体小事（没依据就空字符串）",
                "proactiveImpulse": "一句她可能主动想说的话（没冲动就空字符串）",
            },
            fallback={"summary": fallback["summary"], "location": fallback["location"], "todayEvent": "", "proactiveImpulse": ""},
        )
    except Exception:
        return fallback, "fallback"
    refined = dict(fallback)
    refined["summary"] = str(result.get("summary") or fallback["summary"])
    refined["location"] = str(result.get("location") or "")[:60]
    window = f"{segment.get('start', '')}-{segment.get('end', '')}"
    if result.get("todayEvent"):
        refined["todayEvents"] = [{"window": window, "event": str(result["todayEvent"]), "mood": "", "basis": "llm", "confidence": "medium"}]
    if result.get("proactiveImpulse"):
        refined["proactiveEvents"] = [{"window": window, "reason": "生活契机", "action": "share", "why": "", "topic": str(result["proactiveImpulse"]), "motive": "", "scene": "casual", "tone": ""}]
    return refined, source


# ---------------------------------------------------------------------------
# 每日日程：固定节律 + 当日安排
# ---------------------------------------------------------------------------

# 一天里她大致待在哪。这是她自己的虚拟生活，不是对用户位置的任何断言。
_NIGHT_LOCATION = "床上"
# 下午那一段每天做的事不同——这是"每日日程"与"固定节律"的区别所在。
_AFTERNOON_FOCUS = (
    ("看会儿书", "安静"),
    ("整理旧照片", "有点怀旧"),
    ("追一集剧", "放松"),
    ("写点乱七八糟的东西", "专注"),
    ("什么正事也没干", "有点虚度"),
)
# 一天里最多向模型暴露几件可分享的小事，避免日程把上下文挤满。
MAX_SHAREABLE_EVENTS = 2


def _hhmm(minute: int) -> str:
    minute = minute % (24 * 60)
    return f"{minute // 60:02d}:{minute % 60:02d}"


def build_daily_agenda(now: datetime | None = None, chronotype: dict | None = None) -> list[dict]:
    """今天的日程：固定节律排出骨架，当日种子决定下午那一段具体做什么。

    每段带 `location`——她此刻在自己家的哪儿。段与段之间位置会变，
    `advance_agenda` 负责把这个变化说清楚，而不是让她凭空出现在另一个房间。
    """
    moment = now or datetime.now(timezone.utc)
    profile = chronotype if isinstance(chronotype, dict) else empty_chronotype()
    wake = _wake_minute(profile)
    sleep = _sleep_minute(profile)
    focus, focus_mood = random.Random(_date_seed(moment) * 17 + 3).choice(_AFTERNOON_FOCUS)
    evening_start = max(wake + 6 * 60 + 1, sleep - 4 * 60)
    return [
        {"segmentId": "wake", "start": _hhmm(wake), "end": _hhmm(wake + 60),
         "activity": "起床收拾", "mood": "有点懒", "messageSeed": "刚醒，还不太想说话", "location": "卧室"},
        {"segmentId": "forenoon", "start": _hhmm(wake + 60), "end": _hhmm(wake + 5 * 60),
         "activity": "上午的日常", "mood": "平稳", "messageSeed": "上午有一搭没一搭地过着", "location": "书桌前"},
        {"segmentId": "lunch", "start": _hhmm(wake + 5 * 60), "end": _hhmm(wake + 6 * 60),
         "activity": "吃午饭", "mood": "满足", "messageSeed": "吃饱了想眯一会儿", "location": "厨房"},
        {"segmentId": "afternoon", "start": _hhmm(wake + 6 * 60), "end": _hhmm(evening_start),
         "activity": f"下午{focus}", "mood": focus_mood, "messageSeed": f"下午在{focus}", "location": "客厅"},
        {"segmentId": "evening", "start": _hhmm(evening_start), "end": _hhmm(sleep),
         "activity": "傍晚放松", "mood": "放松", "messageSeed": "天快黑了，人也懒下来", "location": "窗边"},
        {"segmentId": "night", "start": _hhmm(sleep), "end": _hhmm(wake),
         "activity": "睡觉", "mood": "困", "messageSeed": "已经躺下了", "location": _NIGHT_LOCATION},
    ]


def previous_segment(agenda: list[dict], segment: dict | None) -> dict | None:
    """日程里排在当前段前面的那一段，用来判断位置是否刚刚发生过切换。"""
    if not isinstance(segment, dict):
        return None
    start = _parse_hhmm(segment.get("start"))
    if start is None:
        return None
    for row in agenda:
        if isinstance(row, dict) and _parse_hhmm(row.get("end")) == start and row is not segment:
            return row
    return None


# ---------------------------------------------------------------------------
# 临时生活事件
# ---------------------------------------------------------------------------

# 会出现、也会过去的小事。它们不是日程的一部分，而是落在日程上的插曲。
_TRANSIENT_EVENT_POOL = (
    {"kind": "weather", "title": "外面下起雨来", "mood": "有点安静",
     "energyDelta": -2.0, "intensity": 0.4, "durationMinutes": 120, "shareable": True},
    {"kind": "chore", "title": "把屋子从头到尾收拾了一遍", "mood": "踏实",
     "energyDelta": -5.0, "intensity": 0.5, "durationMinutes": 90, "shareable": True},
    {"kind": "small_joy", "title": "翻到一首很久没听的歌", "mood": "挺开心",
     "energyDelta": 4.0, "intensity": 0.6, "durationMinutes": 60, "shareable": True},
    {"kind": "small_trouble", "title": "煮糊了一锅粥", "mood": "有点丧",
     "energyDelta": -4.0, "intensity": 0.5, "durationMinutes": 60, "shareable": True},
    {"kind": "quiet", "title": "在窗边发了会儿呆", "mood": "松弛",
     "energyDelta": 2.0, "intensity": 0.3, "durationMinutes": 45, "shareable": False},
)
_TRANSIENT_COUNT_POOL = (0, 1, 1, 2)


def transient_life_events(now: datetime | None = None, chronotype: dict | None = None) -> list[dict]:
    """今天会发生的临时生活事件，含各自的时间窗。

    同一天永远得到同一组事件，跨天才变——测试可以把时钟往前拨，看它们依次开始、结束。
    """
    moment = now or datetime.now(timezone.utc)
    profile = chronotype if isinstance(chronotype, dict) else empty_chronotype()
    rng = random.Random(_date_seed(moment) * 31 + 7)
    wake = _wake_minute(profile)
    sleep = _sleep_minute(profile)
    pool = list(_TRANSIENT_EVENT_POOL)
    rng.shuffle(pool)
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    events = []
    for index in range(rng.choice(_TRANSIENT_COUNT_POOL)):
        template = pool[index]
        duration = int(template["durationMinutes"])
        earliest = wake + 60
        latest = max(earliest, sleep - duration - 30)
        start_minute = rng.randint(earliest, latest) if latest > earliest else earliest
        starts_at = midnight + timedelta(minutes=start_minute)
        events.append({
            "eventId": f"life:{moment.strftime('%Y%m%d')}:{template['kind']}",
            "kind": template["kind"],
            "title": template["title"],
            "mood": template["mood"],
            "energyDelta": template["energyDelta"],
            "intensity": template["intensity"],
            "shareable": bool(template["shareable"]),
            "startsAt": starts_at.isoformat(),
            "endsAt": (starts_at + timedelta(minutes=duration)).isoformat(),
        })
    return sorted(events, key=lambda row: row["startsAt"])


def active_transient_events(events: list[dict], now: datetime | None = None) -> list[dict]:
    """此刻正在发生的临时事件。窗口过了就不在了——它过去了，不是被删掉了。"""
    moment = now or datetime.now(timezone.utc)
    live = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        starts = _parse_time(event.get("startsAt"))
        ends = _parse_time(event.get("endsAt"))
        if starts is not None and ends is not None and starts <= moment < ends:
            live.append(event)
    return live


def transient_conditions(events: list[dict]) -> list[dict]:
    """把正在发生的临时事件转成状态条件，让 energy 和心情底色跟着一天起伏。"""
    return [{
        "kind": "transient_life_event",
        "title": str(event.get("title") or ""),
        "label": str(event.get("title") or ""),
        "mood": str(event.get("mood") or ""),
        "energyDelta": float(event.get("energyDelta") or 0.0),
        "intensity": float(event.get("intensity") or 0.0),
        "cause": str(event.get("kind") or "transient"),
        "phase": "active",
    } for event in events or [] if isinstance(event, dict)]


# ---------------------------------------------------------------------------
# 日程推进
# ---------------------------------------------------------------------------

def advance_agenda(
    *,
    now: datetime | None = None,
    chronotype: dict | None = None,
    daily_state: dict | None = None,
    conditions: list[dict] | None = None,
    agenda: list[dict] | None = None,
    transient: list[dict] | None = None,
) -> dict:
    """把时钟推到 `now`，给出此刻的生活状态。

    这是日程模块唯一的对外状态出口：当前活动、精力、心情底色、所在位置和可分享的小事。
    完整日程、条件流水和事件池都留在这一层里面，不往外走。

    给了 `daily_state` 就用它的 energy / moodBias，不再自己合成一份。持久化的当日状态
    只能有一个权威来源，否则同一时刻会算出两个不一样的精力值。
    """
    moment = now or datetime.now(timezone.utc)
    profile = chronotype if isinstance(chronotype, dict) else empty_chronotype()
    plan = agenda if agenda is not None else build_daily_agenda(moment, profile)
    pool = transient if transient is not None else transient_life_events(moment, profile)
    live = active_transient_events(pool, moment)
    if isinstance(daily_state, dict) and daily_state:
        state = {
            "date": str(daily_state.get("date") or moment.strftime("%Y-%m-%d")),
            "energy": float(daily_state.get("energy") or ENERGY_BASE),
            "moodBias": str(daily_state.get("moodBias") or ""),
            "period": str(daily_state.get("period") or period_of(moment, profile)),
            "activeWindow": bool(daily_state.get("activeWindow", is_active_window(moment, profile))),
        }
    else:
        state = compose_daily_state(list(conditions or []) + transient_conditions(live), moment, profile)

    segment = current_segment(plan, moment, profile) or {}
    previous = previous_segment(plan, segment) or {}
    location = str(segment.get("location") or "")
    came_from = str(previous.get("location") or "")
    return {
        "date": state["date"],
        "period": state["period"],
        "activeWindow": state["activeWindow"],
        "energy": state["energy"],
        "moodBias": state["moodBias"],
        "currentActivity": str(segment.get("activity") or ""),
        "segmentId": str(segment.get("segmentId") or ""),
        "segmentStart": str(segment.get("start") or ""),
        "segmentEnd": str(segment.get("end") or ""),
        "location": location,
        # 位置切换必须交代来处，否则她会凭空出现在另一个房间。
        "movedFrom": came_from if came_from and came_from != location else "",
        "shareableEvents": [{
            "eventId": str(event.get("eventId") or ""),
            "title": str(event.get("title") or ""),
            "mood": str(event.get("mood") or ""),
            "window": f"{str(event.get('startsAt'))[11:16]}-{str(event.get('endsAt'))[11:16]}",
        } for event in live if event.get("shareable")][:MAX_SHAREABLE_EVENTS],
        "stateKind": "virtual",
    }


def daily_life_fragments(state: dict, *, now: datetime, source: str = "") -> list:
    """日程进模型的唯一形态：处理过的 ContextFragment，不是完整日程。

    进去的只有"她此刻是什么状态"；日程表、状态条件流水、临时事件池、主动候选一律不进。
    """
    from .context_contract import ContextFact, ContextFragment, Priority

    view = state if isinstance(state, dict) else {}
    at = now.isoformat()
    origin = source or ("daily_state:" + str(view.get("date") or "default"))
    facts = [ContextFact("dailyLife", {
        "stateKind": "virtual",
        "period": str(view.get("period") or ""),
        "activeWindow": bool(view.get("activeWindow", True)),
        "energy": float(view.get("energy") or ENERGY_BASE),
        "moodBias": str(view.get("moodBias") or ""),
        "currentActivity": str(view.get("currentActivity") or ""),
        "location": str(view.get("location") or ""),
        "movedFrom": str(view.get("movedFrom") or ""),
    })]
    for event in (view.get("shareableEvents") or [])[:MAX_SHAREABLE_EVENTS]:
        facts.append(ContextFact("dailyLife:event:" + str(event.get("eventId") or ""), {
            "title": str(event.get("title") or ""),
            "mood": str(event.get("mood") or ""),
            "window": str(event.get("window") or ""),
            "stateKind": "virtual",
        }))
    return [ContextFragment(module="dailyLife", priority=Priority.DAILY_LIFE, facts=tuple(facts),
        instructions=(
            "这是你自己的虚拟生活状态，只能影响语气。不要主动复述，也不要拿它编造你没经历过的事。",
            "位置和活动都是你自己的，不是对用户处境的判断。",
        ),
        source_event_ids=(origin,), confidence=1.0 if view else 0.0,
        created_at=at, token_budget=12000)]


# ---------------------------------------------------------------------------
# 主动候选信号（只造候选，不发消息）
# ---------------------------------------------------------------------------

# 每类信号的有效期：过了这个窗口，这句话就不该再说了。
GREETING_WINDOW_MINUTES = 90.0
MEAL_WINDOW_MINUTES = 60.0
AGENDA_SHARE_WINDOW_MINUTES = 120.0


def agenda_candidate_signals(
    state: dict, *, now: datetime | None = None, transient: list[dict] | None = None,
) -> list[dict]:
    """日程能提供的主动候选信号，每个都带自己的有效时间窗。

    **只造候选。** 本模块不知道怎么发消息，也拿不到发消息需要的任何东西；
    是否发送、什么时候发送，全部由 `proactive.evaluate_proactive_lifecycle` 决定。
    """
    moment = now or datetime.now(timezone.utc)
    view = state if isinstance(state, dict) else {}
    signals: list[dict] = []

    def window(minutes: float, *, start: datetime | None = None) -> dict:
        opens = start or moment
        return {"windowStart": opens.isoformat(),
                "bestUntil": (opens + timedelta(minutes=minutes * 0.5)).isoformat(),
                "expiresAt": (opens + timedelta(minutes=minutes)).isoformat()}

    # 签名都带当天日期：早安每天都该说一次，但同一天只说一次。
    # 签名只按正文算的话，第二天那句一模一样的"早安"会被当成"已经说过"而永远不再出现。
    date = str(view.get("date") or moment.strftime("%Y-%m-%d"))
    period = str(view.get("period") or "")
    greeting = {"morning": "早安，新的一天啦", "evening": "天快黑了，今天过得怎么样"}.get(period, "")
    if greeting:
        signals.append({"kind": "daily_greeting", "reason": "daily_anchor", "content": greeting,
                        "topic": greeting, "motive": "按生活节律打个招呼",
                        "signature": f"daily_greeting:{date}:{period}", **window(GREETING_WINDOW_MINUTES)})
    meal = next((str(row.get("title") or "吃饭") for row in (view.get("conditions") or [])
                 if isinstance(row, dict) and str(row.get("kind") or "") == "hunger"), "")
    if meal:
        signals.append({"kind": "meal_care", "reason": "meal_care", "content": "到饭点了，记得吃点东西",
                        "topic": meal, "motive": "到饭点关心一下",
                        "signature": f"meal_care:{date}:{meal}", **window(MEAL_WINDOW_MINUTES)})
    for event in (view.get("shareableEvents") or [])[:MAX_SHAREABLE_EVENTS]:
        title = str(event.get("title") or "")
        if not title:
            continue
        signals.append({"kind": "agenda_share", "reason": "life_opportunity", "content": title,
                        "topic": title, "motive": "想跟你说说这会儿发生的小事",
                        "signature": "agenda_share:" + str(event.get("eventId") or f"{date}:{title}"),
                        **window(AGENDA_SHARE_WINDOW_MINUTES)})
    activity = str(view.get("currentActivity") or "")
    if activity and view.get("activeWindow"):
        signals.append({"kind": "agenda_share", "reason": "life_opportunity", "content": activity,
                        "topic": activity, "motive": "分享此刻在做的事",
                        "signature": f"agenda_share:{date}:{view.get('segmentId')}",
                        **window(AGENDA_SHARE_WINDOW_MINUTES)})
    return signals
