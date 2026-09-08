# -*- coding: utf-8 -*-
"""Budgeted, auditable companion-memory retrieval.

Inspired by Aura's relevant/important/recent prompt buckets, while preserving
Echo's evidence-first memory objects instead of flattening them into summaries.
"""
from __future__ import annotations

import json
import re

from .memory_protocol import memory_time


MEMORY_CONTEXT_BUDGET = 12_000
MAX_SELECTED_MEMORIES = 40

# 三个信号的相对分量。相关性主导，重要性次之，时近性只做微调——
# 桌面事实天然按时间涌入，让时近性主导会把当天的琐事顶到最前面。
RELEVANCE_WEIGHT = 0.5
IMPORTANCE_WEIGHT = 0.3
RECENCY_WEIGHT = 0.2


def _search_terms(text: str) -> tuple[set[str], set[str]]:
    """把查询拆成完整词和字符二元组两级。

    二元组让中文在没有分词和向量的情况下也能召回，但它命中得太容易
    （"今天"能撞上任何一条提到今天的记忆）。两级分开返回，好让打分时
    给完整词更高的分量。
    """
    normalized = re.sub(r"\s+", "", str(text or "").lower())
    words = {term for term in re.findall(r"[a-z0-9_.+-]{2,}|[\u4e00-\u9fff]{2,}", normalized) if term}
    bigrams = {normalized[index:index + 2] for index in range(max(0, len(normalized) - 1))}
    return words, {term for term in bigrams if term}


# 一个完整词的命中远比一个二元组巧合可信。
BIGRAM_WEIGHT = 0.3


def _relevance(haystack: str, words: set[str], bigrams: set[str]) -> tuple[int, float]:
    """返回原始命中数（用于分桶）和按查询覆盖度归一的相关度（用于排序）。"""
    word_hits = sum(1 for term in words if term in haystack)
    bigram_hits = sum(1 for term in bigrams if term in haystack)
    coverage = (
        (word_hits / len(words) if words else 0.0)
        + BIGRAM_WEIGHT * (bigram_hits / len(bigrams) if bigrams else 0.0)
    )
    return word_hits + bigram_hits, min(1.0, coverage)


def _searchable_text(memory: dict) -> str:
    project = memory.get("project") if isinstance(memory.get("project"), dict) else {}
    fields = (
        memory.get("app"), memory.get("title"), memory.get("context"),
        memory.get("statement"), memory.get("label"), memory.get("quote"),
        project.get("name"), project.get("summary"),
    )
    return " ".join(str(value or "") for value in fields)


def _importance(memory: dict) -> float:
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


def _encoded_size(memory: dict) -> int:
    return len(json.dumps(memory, ensure_ascii=False, separators=(",", ":")))


def select_companion_memories(memories: list[dict], query: str) -> tuple[list[dict], dict]:
    """Select evidence through relevant, important and recent buckets.

    The buckets are merged by stable memory id and then constrained by a hard
    character budget. The trace is safe to expose in the local debug console.
    """
    rows = [row for row in memories if isinstance(row, dict) and row.get("id")]
    words, bigrams = _search_terms(query)

    scored: list[tuple[int, float, str, dict]] = []
    coverage: dict[str, float] = {}
    for row in rows:
        haystack = _searchable_text(row).lower()
        hits, matched = _relevance(haystack, words, bigrams)
        coverage[str(row["id"])] = matched
        scored.append((hits, _importance(row), memory_time(row), row))

    relevant = [item[3] for item in sorted(scored, key=lambda item: (item[0], item[1], item[2]), reverse=True) if item[0] > 0][:16]
    important = [item[3] for item in sorted(scored, key=lambda item: (item[1], item[2]), reverse=True)[:12]]
    recent = [item[3] for item in sorted(scored, key=lambda item: item[2], reverse=True)[:16]]

    # 桶只决定候选和轨迹，不再决定顺序。此前是严格的桶优先级：一条因二元组
    # 巧合而 relevance=1 的记忆会压过真正重要且新鲜的记忆。改为三个信号加权，
    # 相关度按查询覆盖度归一，巧合命中因此拿不到满分。
    times = sorted({item[2] for item in scored})
    recency_rank = {value: index / (len(times) - 1) for index, value in enumerate(times)} if len(times) > 1 else {}
    blended = {
        str(item[3]["id"]): (
            RELEVANCE_WEIGHT * coverage.get(str(item[3]["id"]), 0.0)
            + IMPORTANCE_WEIGHT * item[1]
            + RECENCY_WEIGHT * recency_rank.get(item[2], 1.0)
        )
        for item in scored
    }

    merged: list[dict] = []
    seen: set[str] = set()
    bucket_ids: dict[str, list[str]] = {"relevant": [], "important": [], "recent": []}
    for bucket_name, bucket in (("relevant", relevant), ("important", important), ("recent", recent)):
        for row in bucket:
            memory_id = str(row["id"])
            bucket_ids[bucket_name].append(memory_id)
            if memory_id not in seen:
                seen.add(memory_id)
                merged.append(row)
    merged.sort(key=lambda row: blended.get(str(row["id"]), 0.0), reverse=True)

    selected: list[dict] = []
    used = 0
    for row in merged:
        size = _encoded_size(row)
        if selected and (used + size > MEMORY_CONTEXT_BUDGET or len(selected) >= MAX_SELECTED_MEMORIES):
            continue
        selected.append(row)
        used += size

    trace = {
        "strategy": "relevant_important_recent_v2_blended",
        "weights": {
            "relevance": RELEVANCE_WEIGHT,
            "importance": IMPORTANCE_WEIGHT,
            "recency": RECENCY_WEIGHT,
        },
        "blendedScores": {str(row["id"]): round(blended.get(str(row["id"]), 0.0), 4) for row in selected},
        "query": str(query or "")[:400],
        "candidateCount": len(rows),
        "bucketMemoryIds": bucket_ids,
        "selectedMemoryIds": [str(row["id"]) for row in selected],
        "contextChars": used,
        "contextBudgetChars": MEMORY_CONTEXT_BUDGET,
        "omittedCount": max(0, len(rows) - len(selected)),
    }
    return selected, trace
