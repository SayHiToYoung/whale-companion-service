# -*- coding: utf-8 -*-
"""大鲸自己的情绪状态：有惯性、可跨会话演化，而不是用户情绪的镜子。

从 Miru（kiyotakali/Miru，Apache-2.0）抄来的核心设计，按本项目哲学改了三处：

1. 情绪不是离散标签，是「连续二维向量 + 自由文本」——valence 是正负向
   （-1 到 1），arousal 是唤醒度（0 到 1），mood 是一句描述当前心情的话。
2. 惯性靠「混合 + 惰性指数衰减」实现：新互动只贡献 0.6 的权重，旧状态保留
   0.4；每过一小时，偏离基线的部分乘 0.9 衰减。于是她不会上一秒生气下一秒
   没事——情绪是一天天淡掉的，不是被切掉的。
3. 跨会话连续：状态带真实时间戳，恢复时按墙钟时间惰性重算衰减，离线多久
   就淡了多少，不靠定时器去「扣」。

与 Miru 的差异（更克制、可审计）：

- 增量由确定性规则从「本轮用户明确表达的内容」推导，不额外调用 LLM 评估。
- 大鲸的情绪不是用户情绪的镜像：共情只是来源之一，被夸、用户熬夜都会生成
  她自己的反应；但不从未说出口的沉默、忙碌或语气词推导情绪。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

EMOTION_STATE_VERSION = "companion-emotion-v1"

BASELINE_VALENCE = 0.0
BASELINE_AROUSAL = 0.2
DECAY_PER_HOUR = 0.9
BLEND_NEW_WEIGHT = 0.6
CALM_VALENCE = 0.12
CALM_AROUSAL_DRIFT = 0.12


def empty_companion_emotion() -> dict:
    """平静的初始状态：没有 mood，向量落在基线。"""
    return {
        "version": EMOTION_STATE_VERSION,
        "mood": "",
        "valence": BASELINE_VALENCE,
        "arousal": BASELINE_AROUSAL,
        "lastEvent": "",
        "updatedAt": "",
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


def _hours_since(value: object, now: datetime) -> float:
    parsed = _parse_time(value)
    if parsed is None:
        return 0.0
    return max(0.0, (now - parsed).total_seconds() / 3600.0)


def _decay_toward(value: float, baseline: float, hours: float) -> float:
    """惰性指数衰减：偏离基线的部分每过一小时乘 DECAY_PER_HOUR。"""
    if hours <= 0.0:
        return value
    return baseline + (value - baseline) * (DECAY_PER_HOUR ** hours)


def _is_calm(valence: float, arousal: float) -> bool:
    return (
        abs(valence - BASELINE_VALENCE) < CALM_VALENCE
        and abs(arousal - BASELINE_AROUSAL) < CALM_AROUSAL_DRIFT
    )


_WARM = re.compile(r"(谢谢|辛苦|有你在|真好|喜欢你|想你了|抱抱|好棒|太会了|厉害|你懂我)")
_STAYING_UP = re.compile(r"(通宵|熬夜|不睡了|睡不着|还没睡|凌晨|半夜|一宿)")


def _event_target(user_text: str, user_emotion: str) -> tuple[float, float, str, str]:
    """从本轮用户明确表达的内容，推导大鲸本次互动「应该处于」的情绪目标。

    返回 (raw_valence, raw_arousal, mood, event)。raw 是绝对值而非增量：混合时
    用 new = 0.6 * raw + 0.4 * cur，新情绪才会和旧情绪融合，而不是覆盖。
    """
    text = str(user_text or "").strip()

    # 被暖到 / 被夸——人设里「被夸会开心转圈圈」的落地。
    if _WARM.search(text):
        return 0.60, 0.50, "被你这句话暖到了", "warmed"

    # 明确情绪 → 共情，但不镜像。mood 是「我在意你」，不是「我也难过」。
    if user_emotion in {"happy", "excited"}:
        return 0.55, 0.45, "被你带动得挺开心", "shared_joy"
    if user_emotion in {"sad", "wronged"}:
        return -0.25, 0.20, "有点在意你", "concern"
    if user_emotion in {"frustrated", "angry"}:
        return -0.10, 0.35, "想站你这边一起嫌弃", "solidarity"
    if user_emotion == "anxious":
        return -0.15, 0.30, "有点替你悬着", "worry"
    if user_emotion == "tired":
        return -0.05, -0.10, "想让你先歇会儿", "care"

    # 熬夜 → 关心里带一点小情绪，不是责备。
    if _STAYING_UP.search(text):
        return -0.20, -0.05, "又不好好睡觉", "staying_up"

    # 普通互动 → 无事件，情绪只靠衰减慢慢回基线。
    return 0.0, 0.0, "", ""


def update_companion_emotion(
    current: dict,
    user_text: str,
    user_emotion: str,
    *,
    now: datetime | None = None,
) -> dict:
    """按「惰性衰减 → 推导目标 → 混合」更新大鲸的情绪状态。

    纯函数：状态由调用方持有并持久化，这里只做演化，方便复用和测试。
    """
    moment = now or datetime.now(timezone.utc)
    state = empty_companion_emotion() if not isinstance(current, dict) else dict(current)
    valence = max(-1.0, min(1.0, float(state.get("valence") or 0.0)))
    arousal = max(0.0, min(1.0, float(state.get("arousal") or BASELINE_AROUSAL)))
    mood = str(state.get("mood") or "")

    # 1. 惰性衰减：先按墙钟时间把旧情绪淡掉。
    hours = _hours_since(state.get("updatedAt"), moment)
    valence = _decay_toward(valence, BASELINE_VALENCE, hours)
    arousal = _decay_toward(arousal, BASELINE_AROUSAL, hours)

    # 2. 本次互动的情绪目标。
    raw_v, raw_a, new_mood, event = _event_target(user_text, user_emotion)

    # 3. 混合（惯性）：新情绪只占 0.6，旧状态保留 0.4。
    if event:
        valence = BLEND_NEW_WEIGHT * raw_v + (1.0 - BLEND_NEW_WEIGHT) * valence
        arousal = BLEND_NEW_WEIGHT * raw_a + (1.0 - BLEND_NEW_WEIGHT) * arousal
        mood = new_mood
    # 无事件时 mood 保留，但情绪一旦衰减回平静就淡出，不留残余。
    if _is_calm(valence, arousal):
        mood = ""

    return {
        "version": EMOTION_STATE_VERSION,
        "mood": mood,
        "valence": round(max(-1.0, min(1.0, valence)), 4),
        "arousal": round(max(0.0, min(1.0, arousal)), 4),
        "lastEvent": event,
        "updatedAt": moment.isoformat(),
    }


def companion_emotion_context(state: dict) -> dict:
    """给模型看的形态：只描述心情，附带「别主动复读」的约束。"""
    current = state if isinstance(state, dict) else empty_companion_emotion()
    valence = max(-1.0, min(1.0, float(current.get("valence") or 0.0)))
    arousal = max(0.0, min(1.0, float(current.get("arousal") or BASELINE_AROUSAL)))
    calm = _is_calm(valence, arousal)
    return {
        "usage": "你现在的心情。它自然影响语气和用词，但不要主动说出来，更不要复读这句话。",
        "mood": "" if calm else str(current.get("mood") or ""),
        "valence": valence,
        "arousal": arousal,
        "calm": calm,
    }


def judge_companion_emotion(current: dict, user_text: str, user_emotion: str, judge=None, now=None):
    """LLM 判定她此刻的心情（心脏）；judge 为 None 或失败时回退到确定性规则。

    judge 是可调用 judge(task, context, fields, fallback) -> (result, source)。
    LLM 产出的是「她此刻真实的感觉」（带不确定性），确定性只保留「惰性混合 +
    衰减」这一层身体惯性——她不会瞬间切换情绪，但情绪本身由 LLM 感觉，不靠 regex。
    返回 (state, source)，source ∈ {"model", "fallback"}。
    """
    moment = now or datetime.now(timezone.utc)
    fallback = update_companion_emotion(current, user_text, user_emotion, now=moment)
    if judge is None:
        return fallback, "fallback"
    try:
        result, source = judge(
            task="判断陪伴者此刻的真实心情",
            context={
                "userText": str(user_text or "")[:400],
                "userEmotion": user_emotion,
                "currentMood": str((current or {}).get("mood") or ""),
            },
            fields={
                "mood": "一句话描述她此刻的心情（可以没有感觉，输出空字符串）",
                "valence": "-1 到 1 的正负向，-1 很不好，+1 很好",
                "arousal": "0 到 1 的唤醒度，0 平静，1 兴奋/紧绷",
            },
            fallback={"mood": fallback["mood"], "valence": fallback["valence"], "arousal": fallback["arousal"]},
        )
    except Exception:
        return fallback, "fallback"
    raw_v = max(-1.0, min(1.0, float(result.get("valence", fallback["valence"]))))
    raw_a = max(0.0, min(1.0, float(result.get("arousal", fallback["arousal"]))))
    mood = str(result.get("mood") or "")
    # 身体惯性：LLM 是「感觉」，旧状态 + 衰减是「身体」，她不能瞬间换心情。
    state = empty_companion_emotion() if not isinstance(current, dict) else dict(current)
    valence = max(-1.0, min(1.0, float(state.get("valence") or 0.0)))
    arousal = max(0.0, min(1.0, float(state.get("arousal") or BASELINE_AROUSAL)))
    hours = _hours_since(state.get("updatedAt"), moment)
    valence = _decay_toward(valence, BASELINE_VALENCE, hours)
    arousal = _decay_toward(arousal, BASELINE_AROUSAL, hours)
    valence = BLEND_NEW_WEIGHT * raw_v + (1.0 - BLEND_NEW_WEIGHT) * valence
    arousal = BLEND_NEW_WEIGHT * raw_a + (1.0 - BLEND_NEW_WEIGHT) * arousal
    if _is_calm(valence, arousal):
        mood = ""
    return {
        "version": EMOTION_STATE_VERSION,
        "mood": mood,
        "valence": round(max(-1.0, min(1.0, valence)), 4),
        "arousal": round(max(0.0, min(1.0, arousal)), 4),
        "lastEvent": "llm_judged",
        "updatedAt": moment.isoformat(),
    }, source
