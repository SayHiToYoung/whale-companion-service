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

ACTIVE_MIN_SILENCE_MINUTES = 2.0        # 用户仍在聊天（静默 < 2 分钟）时不插话
PROACTIVE_MIN_INTERVAL_MINUTES = 15.0   # 两次主动至少隔 15 分钟
FOLLOW_UP_WEIGHT_FLOOR = 0.25           # 约定权重低于此值视为已过期，不再主动
CARRIED_WEIGHT_FLOOR = 0.25             # 情绪线程权重低于此值不再主动提起

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
) -> dict:
    """判断大鲸此刻是否应该主动开口，以及开口的依据。

    信号全部可选：缺哪个就按「不知道就不硬拦」处理——但候选内容必须有据。
    """
    moment = now or datetime.now(timezone.utc)
    checked_at = moment.isoformat()
    follow_ups = [row for row in (pending_follow_ups or []) if isinstance(row, dict)]
    due = _due_follow_up(follow_ups)

    # 闸门：硬规则，一条触发即否决（顺序即优先级）。
    silence = _minutes_since(last_user_message_at, moment)
    if activity == "active" and silence is not None and silence < ACTIVE_MIN_SILENCE_MINUTES:
        return _veto("user_still_chatting", checked_at)
    since_spoke = _minutes_since(last_spoke_at, moment)
    if since_spoke is not None and since_spoke < PROACTIVE_MIN_INTERVAL_MINUTES:
        return _veto("too_soon", checked_at)
    if period in _QUIET_PERIODS and due is None:
        return _veto("quiet_hours", checked_at)

    # 候选：按优先级取第一个有据可依的开口理由。
    if due:
        return _speak("follow_up", "due_follow_up", str(due.get("hint") or due.get("topic") or ""), checked_at)
    if carried_emotion and carried_weight >= CARRIED_WEIGHT_FLOOR:
        return _speak("check_in", "open_emotion_thread", carried_emotion, checked_at)
    if opening:
        return _speak("observation", "grounded_opening", opening, checked_at)
    return _veto("nothing_grounded", checked_at)
