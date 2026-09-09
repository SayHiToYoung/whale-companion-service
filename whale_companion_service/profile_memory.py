# -*- coding: utf-8 -*-
"""Evidence-first long-term profile and boundary memory helpers."""
from __future__ import annotations

import hashlib
import re


PROFILE_MEMORY_VERSION = "echo-profile-memory-v1"


def stable_id(prefix: str, *parts: str) -> str:
    raw = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()[:24]}"


# 句末语气词。用户说“别再提健身房了”，要屏蔽的话题是“健身房”，不是“健身房了”——
# 带着语气词的词条永远匹配不上真正的记忆内容，边界会静默失效。
_TRAILING_PARTICLES = re.compile(r"(?:[了吧啊呢吗呀嘛哦噢喔嗯])+$")


def _boundary_term(raw: str) -> str:
    """把用户随口说的一段话收敛成可用于匹配的词条。"""
    term = " ".join(str(raw or "").split()).strip("“”「」\"' ")
    stripped = _TRAILING_PARTICLES.sub("", term).strip()
    # 全是语气词时保留原词，宁可宽一点也不要产出空边界。
    return stripped or term


def extract_profile_memories(text: str) -> dict[str, list[dict]]:
    """Extract only explicit, high-precision statements from a user message."""
    source = " ".join(str(text or "").split())
    facts: list[dict] = []
    boundaries: list[dict] = []

    name_match = re.search(r"(?:我叫|我的名字是)\s*([^，。！？!?\s]{1,20})", source)
    if name_match:
        facts.append({"predicate": "name", "value": name_match.group(1), "confidence": 1.0})

    if re.search(r"(?:我已经工作了|我在上班|我是上班族|我不是学生)", source):
        facts.append({"predicate": "life_stage", "value": "working", "confidence": 1.0})
    if re.search(r"(?:我)?不是学生", source):
        boundaries.append({
            "kind": "identity_correction",
            "rule": "不得把用户当作学生，也不要询问早课、考试、作业、学校或寒暑假安排。",
            "priority": 100,
        })

    for match in re.finditer(r"(?:不要|别)(?:再)?(?:叫我)\s*([^，。！？!?]{1,20})", source):
        term = _boundary_term(match.group(1))
        if term:
            boundaries.append({
                "kind": "addressing",
                "rule": f"不要称呼用户为“{term}”。",
                "priority": 100,
            })
    for match in re.finditer(r"(?:不要|别)再提\s*([^，。！？!?]{1,40})", source):
        term = _boundary_term(match.group(1))
        if term:
            boundaries.append({
                "kind": "topic_suppression",
                "rule": f"不要主动提起“{term}”。",
                "priority": 100,
            })
    # "别主动找我"是对行为本身的限制，不是对某个话题的限制，必须单独识别——
    # 否则它只会被当成一句普通的抱怨，关系照旧、主动照旧。
    if re.search(r"(?:(?:不要|别)(?:再)?(?:主动)?(?:找|联系|打扰|烦)我|免打扰|别来烦我|别理我)", source):
        boundaries.append({
            "kind": "contact",
            "rule": "不要主动联系用户；除非用户先开口，否则保持安静。",
            "priority": 100,
            "blockProactive": True,
        })
    return {"facts": facts, "boundaries": boundaries}


def profile_context(facts: list[dict], boundaries: list[dict]) -> dict:
    return {
        "version": PROFILE_MEMORY_VERSION,
        "userFacts": [
            {
                "id": row.get("id", ""),
                "predicate": row.get("predicate", ""),
                "value": row.get("value", ""),
                "status": row.get("status", ""),
                "confidence": row.get("confidence", 0),
                "evidence": row.get("evidence", []),
            }
            for row in facts
            if row.get("status") in {"confirmed", "candidate"}
        ],
        "boundaries": [
            {"id": row.get("id", ""), "kind": row.get("kind", ""), "rule": row.get("rule", "")}
            for row in boundaries
            if row.get("status") == "active"
        ],
    }
