# -*- coding: utf-8 -*-
"""可插拔感知输入：来源状态与新鲜度模型。

桌宠、日历、天气、设备在场……这些都是**可选**的外部观察。本模块回答的问题只有一个：
一条外部观察在什么条件下、以什么形态，才有资格出现在模型上下文里。

它是纯函数模块：不读库、不调模型、不发消息、不写状态。存储与接线在 `memory_server`，
组装仍然只有 `ContextAssembler` 一个入口。

## 一条观察至少要说清楚七件事

| 字段 | 含义 |
| --- | --- |
| `sourceId` | 哪个来源实例报的（同一台桌宠、同一个日历账号） |
| `sourceType` | 它是哪一类来源，决定字段白名单和默认新鲜期 |
| `observedAt` | 这件事**被看到**的时刻 |
| `receivedAt` | 服务**收到**它的时刻 |
| `expiresAt` / freshness policy | 什么时候它不再代表此刻 |
| `confidence` | 来源自己有多确定 |
| `payload` / `evidenceRef` | 原始证据，**只进存储** |

`observedAt` 和 `receivedAt` 必须分开：一条离线攒了两小时才补传的观察，收到得很新，
看到得很旧。新鲜度只能按 `observedAt` 算，否则一次补传就能让两小时前的画面冒充此刻。

## 五条底线

1. **感知端完全可选。** 一条来源都没有时，`perception_fragments([])` 返回空，聊天照旧。
2. **原始 payload 只进存储。** 进上下文的只有按 `sourceType` 白名单投影出来的最小事实。
3. **过期即不进。** 过了新鲜期的观察不投影，也不靠"来源还在线"续命。
4. **观察永远是观察。** 投影出来的事实 `source` 恒为 `observed`，置信度受本类型上限压制，
   绝不会因为来源自称什么就升格成用户亲口说过的事。
5. **来源在线 ≠ 用户在场。** 本模块只描述来源，不描述用户，也不产出任何主动开口信号。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from .context_contract import ContextFact, ContextFragment, Priority

# 观察进上下文的档位：压在她自己的生活之下、故事之上。
# 外部观察可以给语气一点背景，但不该盖过用户此刻说的话、未完成的事和有据记忆。
PERCEPTION_PRIORITY = int(Priority.STORY) + 50

# 来源状态。`live` 只表示这个来源刚刚还在报数，与用户在不在无关。
SOURCE_LIVE_SECONDS = 120.0
SOURCE_OFFLINE_SECONDS = 900.0

# 时钟允许的前偏差。超过它就说明来源的钟不对，宁可丢掉也不让"未来"的观察进来。
MAX_CLOCK_SKEW_SECONDS = 120.0

_TEXT_LIMIT = 240
_MAX_PAYLOAD_BYTES = 32 * 1024


class PerceptionError(ValueError):
    """一条观察在协议层就不合法。调用方转成 4xx，不要落库。"""


# 每类来源能投影的字段、必须拿得出的证据、默认新鲜期，以及置信度上限。
# 上限的意义：观察永远只是观察。哪怕来源自称 1.0，它也不该在仲裁里压过
# 用户亲口说过的事——那条线由 source="observed" 和这个上限共同守住。
SOURCE_TYPE_RULES: dict[str, dict] = {
    "desktop_activity": {
        "fields": ("app", "context", "durationSeconds"),
        "required": ("app",),
        "freshnessSeconds": 300.0,
        "maxConfidence": 0.6,
    },
    "calendar": {
        "fields": ("title", "startsAt", "endsAt", "location"),
        "required": ("title", "startsAt"),
        "freshnessSeconds": 86_400.0,
        "maxConfidence": 0.8,
    },
    "weather": {
        "fields": ("condition", "temperatureC", "location"),
        "required": ("condition",),
        "freshnessSeconds": 3_600.0,
        "maxConfidence": 0.7,
    },
    "device_presence": {
        "fields": ("device", "state"),
        "required": ("state",),
        "freshnessSeconds": 600.0,
        "maxConfidence": 0.6,
    },
}

SOURCE_TYPES = tuple(sorted(SOURCE_TYPE_RULES))

# 认不出的来源类型：**收下，但不投影**。收下是为了让新来源不必等服务端先发版；
# 不投影是因为没有白名单就没法净化——未知不等于放行。
UNKNOWN_TYPE_FRESHNESS_SECONDS = 300.0

# 这句永远都在，与来源在不在线无关：模型看到的是带时刻的外部观察，
# 不是一路实时画面。少了它，一条五分钟前的桌面观察就会被说成"我正看着你在写代码"。
PERCEPTION_USAGE = (
    "这些是外部来源在各自 observedAt 时刻的观察，不是你此刻正在看到的画面，"
    "也不是用户亲口告诉你的事。可以据此调整语气，不要声称你在实时观看，"
    "更不要把它说成用户说过的话。"
)
OFFLINE_USAGE = "这个来源已经不在报数了，上面那条是它离线前留下的观察，不要当作现在的情况。"


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _token(value: object, field: str, limit: int = 128) -> str:
    text = " ".join(str(value or "").split())
    if not text or len(text) > limit or any(ord(char) < 32 for char in text):
        raise PerceptionError(f"invalid perception {field}")
    return text


def freshness_seconds_of(source_type: str) -> float:
    rules = SOURCE_TYPE_RULES.get(str(source_type or ""))
    return float(rules["freshnessSeconds"]) if rules else UNKNOWN_TYPE_FRESHNESS_SECONDS


def normalize_observation(row: object, *, received_at: datetime) -> dict:
    """把客户端报上来的一条观察归一成可落库的形态。

    这里只做协议层校验——字段在不在、时间读不读得出来、payload 大不大。
    **不在这里做投影**：原始 payload 原样落库，净化是读取时 `perception_fragments` 的事。
    """
    if not isinstance(row, dict):
        raise PerceptionError("observation must be an object")
    observation_id = _token(row.get("observationId"), "observationId")
    source_id = _token(row.get("sourceId"), "sourceId")
    source_type = _token(row.get("sourceType"), "sourceType", 64)

    observed_at = _parse_time(row.get("observedAt")) or received_at
    if observed_at > received_at + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
        # 来源的钟比服务快太多。放进来的话，"未来"的观察会永远显得最新鲜。
        raise PerceptionError("observedAt is too far in the future")

    freshness = row.get("freshnessSeconds")
    if freshness is None:
        freshness = freshness_seconds_of(source_type)
    try:
        freshness = float(freshness)
    except (TypeError, ValueError):
        raise PerceptionError("invalid freshnessSeconds") from None
    if not math.isfinite(freshness) or freshness <= 0 or freshness > 30 * 86_400.0:
        raise PerceptionError("invalid freshnessSeconds")

    explicit_expiry = _parse_time(row.get("expiresAt"))
    expires_at = explicit_expiry or (observed_at + timedelta(seconds=freshness))

    confidence = row.get("confidence")
    confidence = 0.5 if confidence is None else confidence
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        raise PerceptionError("invalid confidence") from None
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise PerceptionError("invalid confidence")

    payload = row.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise PerceptionError("payload must be an object")
    if len(str(payload)) > _MAX_PAYLOAD_BYTES:
        raise PerceptionError("payload too large")

    return {
        "observationId": observation_id,
        "sourceId": source_id,
        "sourceType": source_type,
        "observedAt": observed_at.isoformat(),
        "receivedAt": received_at.isoformat(),
        "expiresAt": expires_at.isoformat(),
        "freshnessSeconds": freshness,
        "confidence": confidence,
        "payload": payload,
        "evidenceRef": _token(row.get("evidenceRef"), "evidenceRef", 512) if row.get("evidenceRef") else "",
    }


def freshness_of(observation: dict, now: datetime) -> dict:
    """一条观察此刻还算不算数。过期只看 observedAt 和新鲜期，与来源在不在线无关。"""
    observed = _parse_time(observation.get("observedAt"))
    expires = _parse_time(observation.get("expiresAt"))
    age = None if observed is None else max(0.0, (now - observed).total_seconds())
    if expires is None or observed is None:
        return {"state": "expired", "ageSeconds": age, "expiresAt": None}
    return {
        "state": "expired" if expires <= now else "fresh",
        "ageSeconds": age,
        "expiresAt": expires.isoformat(),
    }


def source_states(observations: list[dict], *, now: datetime) -> list[dict]:
    """每个来源此刻的状态。**这描述的是来源，不是用户。**

    `live` 只意味着这个来源刚刚还在报数。它不表示用户在电脑前、有空、或者醒着——
    把来源在线当成用户在场，是这类系统最容易犯、也最伤人的一个错。
    """
    grouped: dict[str, dict] = {}
    for row in observations:
        source_id = str(row.get("sourceId") or "")
        if not source_id:
            continue
        entry = grouped.setdefault(source_id, {
            "sourceId": source_id,
            "sourceType": str(row.get("sourceType") or ""),
            "observationCount": 0,
            "lastObservedAt": "",
            "lastReceivedAt": "",
            "freshObservations": 0,
        })
        entry["observationCount"] += 1
        for key, field in (("lastObservedAt", "observedAt"), ("lastReceivedAt", "receivedAt")):
            value = str(row.get(field) or "")
            if value > entry[key]:
                entry[key] = value
        if freshness_of(row, now)["state"] == "fresh":
            entry["freshObservations"] += 1

    result = []
    for entry in grouped.values():
        received = _parse_time(entry["lastReceivedAt"])
        silence = None if received is None else max(0.0, (now - received).total_seconds())
        if silence is None or silence > SOURCE_OFFLINE_SECONDS or not entry["freshObservations"]:
            state = "offline"
        elif silence <= SOURCE_LIVE_SECONDS:
            state = "live"
        else:
            state = "idle"
        result.append({**entry, "state": state,
                       "silenceSeconds": None if silence is None else round(silence, 1)})
    return sorted(result, key=lambda row: row["sourceId"])


def _projected_value(observation: dict) -> tuple[dict | None, str]:
    """按来源类型的白名单投影出最小事实。白名单之外的一切都留在存储里。"""
    source_type = str(observation.get("sourceType") or "")
    rules = SOURCE_TYPE_RULES.get(source_type)
    if rules is None:
        return None, "unsupported_source_type"
    payload = observation.get("payload")
    if not isinstance(payload, dict):
        return None, "payload_not_projectable"
    value: dict = {}
    for key in rules["fields"]:
        raw = payload.get(key)
        if raw is None:
            continue
        if isinstance(raw, bool):
            return None, "field_not_projectable"
        if isinstance(raw, (int, float)):
            number = float(raw)
            if not math.isfinite(number):
                return None, "field_not_projectable"
            if key == "durationSeconds":
                if number < 0:
                    return None, "field_not_projectable"
                value[key] = int(number)
            else:
                value[key] = round(number, 2)
            continue
        if not isinstance(raw, str):
            return None, "field_not_projectable"
        text = " ".join(raw.split())[:_TEXT_LIMIT]
        if text:
            value[key] = text
    if any(key not in value for key in rules["required"]):
        return None, "missing_required_evidence"
    value["sourceType"] = source_type
    value["observedAt"] = str(observation.get("observedAt") or "")
    return value, ""


def perception_fragments(observations: list[dict] | None, *, now: datetime,
                         sources: list[dict] | None = None) -> tuple[list[ContextFragment], list[dict]]:
    """观察进上下文的唯一通道：过期筛除 → 白名单投影 → 每来源取最新 → ContextFragment。

    返回 `(fragments, trace)`。`trace` 逐条记下每个 `observationId` 是进去了还是被丢了、
    以及丢它的理由——没有这条轨迹，"她为什么没提这件事"就只能靠猜。
    **trace 是调试数据，不进模型**：调用方把它挂在帧的兼容键上，`model_view()` 看不见。

    没有任何来源时返回 `([], [])`，聊天链路一切照旧。
    """
    rows = [row for row in (observations or []) if isinstance(row, dict)]
    if not rows:
        return [], []
    state_by_source = {row["sourceId"]: row for row in
                       (sources if sources is not None else source_states(rows, now=now))}

    trace: list[dict] = []
    newest: dict[str, tuple[dict, dict]] = {}
    for row in sorted(rows, key=lambda item: (str(item.get("observedAt") or ""),
                                              str(item.get("observationId") or ""))):
        entry = {
            "observationId": str(row.get("observationId") or ""),
            "sourceId": str(row.get("sourceId") or ""),
            "sourceType": str(row.get("sourceType") or ""),
            "observedAt": str(row.get("observedAt") or ""),
        }
        reading = freshness_of(row, now)
        if reading["state"] != "fresh":
            trace.append({**entry, "decision": "dropped", "reason": "expired"})
            continue
        value, reason = _projected_value(row)
        if value is None:
            trace.append({**entry, "decision": "dropped", "reason": reason})
            continue
        previous = newest.get(entry["sourceId"])
        if previous is not None:
            trace.append({**previous[1], "decision": "dropped", "reason": "superseded"})
        newest[entry["sourceId"]] = (row, {**entry, "value": value})

    fragments = []
    for source_id, (row, entry) in sorted(newest.items()):
        source_state = str(state_by_source.get(source_id, {}).get("state") or "offline")
        rules = SOURCE_TYPE_RULES[str(row["sourceType"])]
        # 置信度受本类型上限压制：观察不会因为来源自称笃定就变成事实。
        confidence = min(float(row.get("confidence") or 0.0), float(rules["maxConfidence"]))
        instructions = [PERCEPTION_USAGE]
        if source_state != "live":
            instructions.append(OFFLINE_USAGE)
        fragments.append(ContextFragment(
            module="perception", priority=PERCEPTION_PRIORITY,
            facts=(ContextFact(
                # 同 key 意味着互斥取值：同一个来源对"此刻"只能有一种说法。
                key="perception:" + str(row["sourceType"]) + ":" + source_id,
                value={**entry["value"], "sourceState": source_state},
                # 恒为 observed。来源自称什么都不改变这一点——观察不会升格成用户亲口的事实。
                source="observed", lifecycle="active",
                expires_at=str(row.get("expiresAt") or "")),),
            instructions=tuple(instructions),
            source_event_ids=("perception:" + source_id + ":" + str(row["observationId"]),),
            confidence=confidence, created_at=str(row["observedAt"]),
            expires_at=str(row.get("expiresAt") or ""),
            sensitivity="personal", token_budget=4000))
        trace.append({key: value for key, value in entry.items() if key != "value"} |
                     {"decision": "projected", "reason": "", "sourceState": source_state})

    trace.sort(key=lambda row: (row["sourceId"], row["observedAt"], row["observationId"]))
    return fragments, trace


def perception_debug_view(observations: list[dict] | None, *, now: datetime) -> dict:
    """控制台视图：来源状态 + 本轮投影/丢弃轨迹。原始 payload 不出现在这里。"""
    rows = [row for row in (observations or []) if isinstance(row, dict)]
    states = source_states(rows, now=now)
    fragments, trace = perception_fragments(rows, now=now, sources=states)
    return {
        "sources": states,
        "projected": [row for row in trace if row["decision"] == "projected"],
        "dropped": [row for row in trace if row["decision"] == "dropped"],
        "fragmentCount": len(fragments),
        "checkedAt": now.isoformat(),
    }
