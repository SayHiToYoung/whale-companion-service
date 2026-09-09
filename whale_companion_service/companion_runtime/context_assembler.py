"""Deterministic arbitration. No state writes, prompts, model calls or message sends."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import math
import re

from .context_contract import (
    CompanionFrame, ContextBudgetError, ContextFragment, Priority, canonical, timestamp, token_cost,
)

_PRIVATE_KEYS = frozenset({
    "raw", "raw_events", "rawEvents", "activityLogs", "activity_logs", "audit", "ledger",
    "conditions", "detail", "evidence", "score", "carryWeight", "carriedWeight", "weight",
    "mentionCount", "userTurnCount", "knownFactCount", "usageCount", "hourHistogram",
    "companionEmotionNext", "metadata", "internalMetadata",
})
_SOURCE_RANK = {"user_stated": 4, "confirmed": 3, "observed": 2, "derived": 1, "default": 0}
# 只有这两个模块有资格约束别的模块：用户边界，以及由用户边界和关系状态算出的关系权限。
# 它们自己不受别人的屏蔽——否则一个被约束的模块可以反过来把约束它的那个挡掉。
_CONTROL_MODULES = frozenset({"boundaries", "relationshipPolicy"})


def safe_value(value):
    """Defense in depth; adapters must still explicitly project domain fields."""
    if isinstance(value, dict):
        return {str(k): safe_value(v) for k, v in value.items() if k not in _PRIVATE_KEYS}
    if isinstance(value, (list, tuple)):
        return [safe_value(v) for v in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("Context values must be finite JSON data")


class ContextAssembler:
    def __init__(self, *, total_token_budget: int = 48000, module_budgets: dict[str, int] | None = None):
        self.total_token_budget = total_token_budget
        self.module_budgets = dict(module_budgets or {})

    def assemble(self, fragments: list[ContextFragment], *, scene: str = "", query: str = "",
                 now: datetime | None = None, recent_conversation: list[dict] | None = None,
                 compatibility: dict | None = None) -> CompanionFrame:
        moment = now or datetime.now(timezone.utc)
        moment = timestamp(moment)
        live = [f for f in fragments if not f.expires_at or timestamp(f.expires_at) > moment]
        controls = [f for f in live if f.module in _CONTROL_MODULES]
        blocked_terms, blocked_modules, blocked_keys = set(), set(), set()
        denied = {"internal", "secret", "private"}
        for f in controls:
            blocked_terms.update(f.metadata.get("blocked_terms", ()))
            blocked_modules.update(f.metadata.get("blocked_modules", ()))
            blocked_keys.update(f.metadata.get("blocked_keys", ()))
            denied.update(f.metadata.get("deny_sensitivities", ()))

        def allowed(text):
            return not any(str(term).casefold() in text.casefold() for term in blocked_terms if term)

        def priority(f):
            return int(Priority.BOUNDARY) if f.module == "boundaries" else min(int(f.priority), int(Priority.CURRENT))

        terms = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]{2}", query.casefold()))
        def relevance(f, value):
            return int(scene in f.metadata.get("scenes", ())) * 2 + sum(t in canonical(value).casefold() for t in terms)

        candidates = []
        instructions = []
        budgets = {}
        for f in live:
            if f.sensitivity in denied or (f.module not in _CONTROL_MODULES and f.module in blocked_modules):
                continue
            budgets[f.module] = min(budgets.get(f.module, f.token_budget), f.token_budget,
                                    self.module_budgets.get(f.module, f.token_budget))
            for fact in f.facts:
                expiries = [e for e in (fact.expires_at, f.expires_at) if e]
                expiry = min(expiries, key=timestamp) if expiries else None
                if expiry and timestamp(expiry) <= moment:
                    continue
                if fact.lifecycle not in {"active", "confirmed", "pending_confirmation", "virtual", "unknown"}:
                    continue
                value = safe_value(fact.value)
                if f.module not in _CONTROL_MODULES and (fact.key in blocked_keys or not allowed(canonical(value))):
                    continue
                confidence = float(f.confidence if fact.confidence is None else fact.confidence)
                if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                    raise ValueError("Invalid context confidence")
                created = timestamp(fact.created_at or f.created_at).isoformat()
                ids = sorted(set(f.source_event_ids) | set(fact.source_event_ids))
                if not ids:
                    continue  # Unattributed facts never reach the model.
                row = dict(key=fact.key, value=value, module=f.module, source_event_ids=ids,
                           confidence=confidence, created_at=created, expires_at=expiry,
                           source=fact.source, lifecycle=fact.lifecycle)
                rank = (priority(f), confidence, _SOURCE_RANK.get(fact.source, 0), created)
                candidates.append((row, rank, relevance(f, value)))
            for text in f.instructions:
                if text and f.source_event_ids and (f.module in _CONTROL_MODULES or allowed(text)):
                    instructions.append((dict(module=f.module, text=text, source_event_ids=sorted(f.source_event_ids),
                                              confidence=f.confidence, created_at=f.created_at, expires_at=f.expires_at),
                                         priority(f), relevance(f, text)))

        # Conflict authority is boundary > confidence > evidence source > time.
        # Module priority orders inclusion, but cannot turn a weak claim into truth.
        def authority(item):
            row, rank, _ = item
            return (row["module"] == "boundaries", *rank[1:], canonical(row))

        grouped = defaultdict(list)
        for item in candidates:
            grouped[item[0]["key"]].append(item)
        winners = []
        for key in sorted(grouped):
            group = sorted(grouped[key], key=authority, reverse=True)
            row, rank, rel = group[0]
            # Only agreeing evidence is merged. Contradicting provenance is never attached to the winner.
            same = [item[0] for item in group if canonical(item[0]["value"]) == canonical(row["value"])]
            row["source_event_ids"] = sorted({sid for other in same for sid in other["source_event_ids"]})
            winners.append((row, rank[0], rel))

        frame = CompanionFrame(version="companion-frame-v5", recentConversation=[], processedFacts=[], moduleInstructions=[])
        if token_cost(frame.model_view()) > self.total_token_budget:
            raise ContextBudgetError("Frame envelope exceeds total budget")
        used = defaultdict(int)
        items = [(r, p, rel, "processedFacts") for r, p, rel in winners]
        seen_instructions = {}
        for row, p, rel in sorted(instructions, key=lambda item: (-item[1], -item[2], canonical(item[0]))):
            if row["text"] not in seen_instructions:
                seen_instructions[row["text"]] = row
                items.append((row, p, rel, "moduleInstructions"))
            else:
                existing = seen_instructions[row["text"]]
                existing["source_event_ids"] = sorted(set(existing["source_event_ids"]) | set(row["source_event_ids"]))

        # Most recent turn is mandatory and counts against both budgets.
        history = [r for r in (recent_conversation or []) if r.get("role") in {"user", "assistant"} and r.get("text")]
        for index, row in enumerate(reversed(history[-8:])):
            content = str(row["text"])
            if index and not allowed(content):
                continue
            items.append((dict(role=row["role"], content=content),
                          int(Priority.CURRENT) if index == 0 else int(Priority.CURRENT) - 1,
                          -index, "recentConversation"))
        for row, p, rel, dest in sorted(items, key=lambda x: (-x[1], -x[2], canonical(x[0]))):
            module = row.get("module", "recent_conversation")
            cost = token_cost(row) + 1
            limit = min(self.module_budgets.get(module, budgets.get(module, 12000)), budgets.get(module, 12000))
            frame[dest].append(row)
            fits = used[module] + cost <= limit and token_cost(frame.model_view()) <= self.total_token_budget
            if not fits:
                frame[dest].pop()
                if module == "boundaries" or (dest == "recentConversation" and rel == 0):
                    raise ContextBudgetError("Mandatory context exceeds budget")
            else:
                used[module] += cost
        frame["recentConversation"].reverse()
        frame["internalMetadata"] = {"budgetUnit": "utf8_bytes_upper_bound", "totalBudget": self.total_token_budget,
                                     "usedTokens": token_cost(frame.model_view()), "moduleUsage": dict(sorted(used.items()))}
        # Existing consumers can inspect old processed state shapes, but model_view never reads these.
        legacy_keys = {"sharedScene", "relationship", "scene", "emotion", "openThreads", "innerReaction",
                       "turnDecision", "speechStyle", "safety", "dailyLife", "selfTimeline", "learnedExpressions", "companionEmotion"}
        def redact(value):
            if isinstance(value, str):
                return value if allowed(value) else ""
            if isinstance(value, dict):
                return {k: redact(v) for k, v in value.items()}
            if isinstance(value, list):
                return [redact(v) for v in value]
            return value
        for key, value in (compatibility or {}).items():
            if key not in legacy_keys:
                continue
            # Compatibility data is already processed, but cannot reintroduce privacy-filtered content.
            frame[key] = value if key == "safety" else redact(value)
        return frame
