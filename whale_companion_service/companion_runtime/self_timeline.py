# -*- coding: utf-8 -*-
"""大鲸的自我时间线：记录「她自己做过的事」，回答「你昨天/今天做了什么」。

从 astrbot_plugin_private_companion 的 self_timeline 提取，按本项目哲学精简：

1. 只收「她已确认做过的事」——日程完成、主动互动、明确情绪、日记、目标进度。
   不收用户的事（「我几点做过什么」不触发）、不收「正在做」的当前态、不收模拟/未确认。
2. 纯确定性：本模块不生成叙事，只做「该不该答」判定 + 「按时间/日期/关键词打分召回」。
   时间线是只读投影，不反向写回状态或记忆。
3. 与用户记忆、每日状态是三种东西：
   - 用户记忆 = 可治理事实（已有 user_facts / memory_lifecycle）。
   - 每日状态 = 当前快照（现在/今日计划，见 daily_life.py）。
   - 自我时间线 = 过去已发生、有据可依的行为历史。

存储由调用方持有（SQLite 表 self_timeline_events），本模块是纯函数：信号喂进来，
只负责「用户是不是在问这个」和「该召回哪几条」。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# 事件类型白名单：只有这五类能进时间线。
EVENT_TYPES = frozenset({"agenda_done", "interaction", "emotion", "diary", "goal_progress"})
# 只有 confirmed 才可召回；pending 待确认、obsolete 已作废。
RECALLABLE_STATUSES = frozenset({"confirmed"})
# 滚动保留上限：超出按时间淘汰最旧的。
MAX_TIMELINE_EVENTS = 50
# 召回条数上下限。
RECALL_MIN = 3
RECALL_MAX = 8

# 自查触发：自查指称（你/大鲸/小鲸/自己）+ 时间词 + 动作词。
_SELF_REF = r"(?:你|大鲸|小鲸|你(?:自己)?)"
_TIME_WORD = r"(?:昨天|前天|今天|今早|今晚|早上|上午|中午|下午|晚上|夜里|最近|这周|几点|什么时候)"
_ACTION_WORD = r"(?:做|干了|干嘛|干啥|忙|搞|在)"
_ASK_TIMELINE = re.compile(
    rf"{_SELF_REF}.{{0,6}}{_TIME_WORD}.{{0,6}}{_ACTION_WORD}"
    rf"|{_SELF_REF}.{{0,6}}{_ACTION_WORD}了(?:什么|啥|些啥|啥了)"
)

# 日期目标提取。
_DATE_TODAY = re.compile(r"今天|今早|今晚|现在")
_DATE_YESTERDAY = re.compile(r"昨天|昨儿")
_DATE_DAY_BEFORE = re.compile(r"前天")

# 关键词 boost：命中这些词说明用户在问具体哪类事。
_KEYWORD_BOOST = {
    "agenda_done": re.compile(r"日程|计划|安排|做了.*事|忙了"),
    "interaction": re.compile(r"聊|说话|互动|陪"),
    "emotion": re.compile(r"情绪|心情|生气|开心|难过"),
    "diary": re.compile(r"日记|记了|写"),
    "goal_progress": re.compile(r"目标|进度|学了|练"),
}


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def asks_about_self_timeline(user_text: str) -> bool:
    """用户是否在问「你（大鲸）自己做了什么」。"""
    text = str(user_text or "").strip()
    if not text:
        return False
    return bool(_ASK_TIMELINE.search(text))


def _date_bucket(ts: datetime, now: datetime) -> str:
    """把事件时间归到「今天/昨天/前天/更早」桶。"""
    days = (now.date() - ts.date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days == 2:
        return "day_before"
    return "older"


def _query_date_target(user_text: str) -> str | None:
    """用户问的是哪一天：today / yesterday / day_before / None(未指定)。"""
    text = str(user_text or "")
    if _DATE_DAY_BEFORE.search(text):
        return "day_before"
    if _DATE_YESTERDAY.search(text):
        return "yesterday"
    if _DATE_TODAY.search(text):
        return "today"
    return None


_DATE_MATCH = {"today": 1.0, "yesterday": 1.0, "day_before": 1.0}


def _recency_score(event_ts: datetime, now: datetime) -> float:
    """未指定日期时的时近性：越近越高，跨天按半衰期衰减。"""
    delta = (now - event_ts).total_seconds()
    if delta < 0:
        return 1.0
    hours = delta / 3600.0
    if hours < 24:
        return max(0.0, 1.0 - hours / 24.0)
    days = hours / 24.0
    return max(0.0, 0.6 * (0.5 ** (days / 3.0)))


def _keyword_score(event: dict, user_text: str) -> float:
    """事件类型/关键词/摘要命中查询关键词的 boost。"""
    text = str(user_text or "")
    event_type = str(event.get("type") or "")
    score = 0.0
    pattern = _KEYWORD_BOOST.get(event_type)
    if pattern and pattern.search(text):
        score += 0.4
    keywords = str(event.get("keywords") or "").split()
    summary = str(event.get("summary") or "")
    for kw in keywords:
        if kw and kw in text:
            score += 0.2
        elif kw and kw in summary:
            score += 0.05
    return min(1.0, score)


def score_self_timeline(events: list[dict], user_text: str, now: datetime | None = None) -> list[tuple[dict, float]]:
    """打分，返回 (event, score) 列表（未排序）。

    指定日期（今天/昨天/前天）时，日期命中占主导，非目标日压到底；
    未指定日期时，按时近性主导、关键词为辅。
    """
    moment = now or datetime.now(timezone.utc)
    target = _query_date_target(user_text)
    scored: list[tuple[dict, float]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if str(event.get("status") or "") not in RECALLABLE_STATUSES:
            continue
        ts = _parse_time(event.get("ts"))
        if ts is None:
            continue
        keyword = _keyword_score(event, user_text)
        if target is None:
            score = 0.7 * _recency_score(ts, moment) + 0.3 * keyword
        else:
            bucket = _date_bucket(ts, moment)
            if bucket == target:
                score = 0.7 + 0.3 * keyword
            else:
                # 非目标日：压到底，仅在目标日无货时兜底。
                score = 0.1 + 0.1 * keyword
        scored.append((event, score))
    return scored


def recall_self_timeline(events: list[dict], user_text: str, now: datetime | None = None, limit: int = RECALL_MAX) -> list[dict]:
    """召回 top-N 已确认事件，按得分降序。空结果表示「别编造」。"""
    scored = score_self_timeline(events, user_text, now)
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [event for event, _score in scored[: max(RECALL_MIN, min(RECALL_MAX, limit))]]


def self_timeline_context(recalled: list[dict]) -> dict:
    """给模型看的形态：只列事实，附带「别编造」约束。"""
    rows = []
    for event in recalled:
        rows.append({
            "when": str(event.get("when") or "")[:40],
            "type": str(event.get("type") or ""),
            "summary": str(event.get("summary") or "")[:160],
        })
    return {
        "usage": "这是你自己做过、有据可依的事。只回答这些内容，没有就别硬凑，直接说没记下。",
        "events": rows,
        "empty": not rows,
    }
