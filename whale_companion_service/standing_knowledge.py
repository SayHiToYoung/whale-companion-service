# -*- coding: utf-8 -*-
"""常驻认知：把散落的证据固化成大鲸"早就知道"的背景。

情景记忆走召回是抽卡——对用户的了解取决于本轮抽到什么。人不是这样记朋友的：
他不会去回忆"哪次谈话你说过家里的事"，他就是知道。这一层补的是这一步。

与召回层的两点不同：

- 它每轮直接进上下文，不参与检索、不衰减。
- 它是 constraint 不是 topic：大鲸据此理解用户，但不主动把它当话题拿出来说。

关键约束：本模块**不推导任何用户没有明说过的事**。每条常驻认知都由已经带原话
证据的行聚合而来（confirmed 的 user_facts、active 的边界、由用户亲口情绪开启的
线程），并保留 basedOn 指回来源，控制台可逐条复核。
"""
from __future__ import annotations

from .profile_memory import stable_id


PLATE_USER_PROFILE = "user_profile"
PLATE_INTERACTION = "interaction_preference"

PLATE_CAPACITY = {PLATE_USER_PROFILE: 12, PLATE_INTERACTION: 10}
PLATE_TITLES = {PLATE_USER_PROFILE: "TA的事", PLATE_INTERACTION: "我们之间"}

# 只有能写成一句稳定认知的 predicate 才上门牌；其余事实留在 user_facts 里
# 参与召回，不进常驻层。
_PROFILE_PHRASING: dict[str, str] = {
    "name": "用户叫{value}。",
    "life_stage": "用户已经在工作，不是学生。",
}

# 线程里的 need 由用户原话正则得出（"不需要建议""听我说""吐槽"），
# 因此可以作为互动偏好固化下来。unknown 不产生任何认知。
_NEED_PHRASING: dict[str, str] = {
    "listening": "用户说过这类事他要的是有人听着，不是建议。",
    "advice": "用户说过这类事他是真的在问办法，可以直接给。",
    "vent": "用户说过这类事他就是想吐槽，不用急着解决它。",
}


def _entry(plate: str, text: str, based_on: list[str], source_count: int) -> dict:
    return {
        "plate": plate,
        "entryId": stable_id("plate", plate, text),
        "text": text,
        "basedOn": list(based_on),
        "sourceCount": max(1, int(source_count)),
    }


def derive_standing_knowledge(
    facts: list[dict], boundaries: list[dict], threads: list[dict],
) -> list[dict]:
    """从已有证据聚合常驻认知；不引入任何新的推断。"""
    entries: list[dict] = []

    for row in facts:
        if row.get("status") != "confirmed":
            continue
        template = _PROFILE_PHRASING.get(str(row.get("predicate") or ""))
        if not template:
            continue
        value = str(row.get("value") or "").strip()
        evidence = [item for item in row.get("evidence", []) if item.get("type") == "support"]
        entries.append(_entry(
            PLATE_USER_PROFILE, template.format(value=value),
            [str(row.get("id") or "")], len(evidence) or 1,
        ))

    for row in boundaries:
        if row.get("status") != "active":
            continue
        rule = str(row.get("rule") or "").strip()
        if rule:
            entries.append(_entry(PLATE_INTERACTION, rule, [str(row.get("id") or "")], 1))

    # 同一种需求可能来自多条线程；合并成一条认知，证据累加。
    need_sources: dict[str, list[str]] = {}
    for row in threads:
        need = str(row.get("need") or "unknown")
        if need in _NEED_PHRASING:
            need_sources.setdefault(need, []).append(str(row.get("threadId") or ""))
    for need, sources in need_sources.items():
        entries.append(_entry(PLATE_INTERACTION, _NEED_PHRASING[need], sources, len(sources)))

    # 同一条认知可能被推出两次；按 entryId 去重并合并证据。
    merged: dict[str, dict] = {}
    for entry in entries:
        existing = merged.get(entry["entryId"])
        if existing is None:
            merged[entry["entryId"]] = entry
            continue
        existing["basedOn"] = list(dict.fromkeys(existing["basedOn"] + entry["basedOn"]))
        existing["sourceCount"] += entry["sourceCount"]

    result: list[dict] = []
    for plate in (PLATE_USER_PROFILE, PLATE_INTERACTION):
        rows = [entry for entry in merged.values() if entry["plate"] == plate]
        # 证据多的先留；容量是刻意的压力，边界判错的条目会被自然挤出。
        rows.sort(key=lambda entry: (entry["sourceCount"], entry["text"]), reverse=True)
        result.extend(rows[:PLATE_CAPACITY[plate]])
    return result


def standing_knowledge_context(entries: list[dict]) -> dict:
    """给模型看的形态：只有门牌名和正文，不带证据 id。"""
    plates: dict[str, list[str]] = {}
    for entry in entries:
        if entry.get("status", "active") != "active":
            continue
        plates.setdefault(str(entry.get("plate") or ""), []).append(str(entry.get("text") or ""))
    return {
        "usage": "你早已知道的背景。据此理解用户，但不要主动把它当话题提起。",
        "plates": [
            {"title": PLATE_TITLES.get(plate, plate), "entries": texts}
            for plate, texts in plates.items() if texts
        ],
    }
