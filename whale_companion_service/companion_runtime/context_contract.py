"""Versioned, JSON-only protocol between state modules and expression models."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any


class Priority(IntEnum):
    BOUNDARY = 800
    CURRENT = 700
    OPEN_THREAD = 600
    MEMORY = 500
    RELATIONSHIP = 400
    DAILY_LIFE = 300
    STORY = 200
    STYLE = 100


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def token_cost(value: Any) -> int:
    """Conservative UTF-8 byte upper bound, independent of provider tokenizer."""
    return len(canonical(value).encode("utf-8"))


def timestamp(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class ContextFact:
    # Same key means mutually exclusive values; different time-bound events use different keys.
    key: str
    value: Any
    source_event_ids: tuple[str, ...] = ()
    confidence: float | None = None
    created_at: str | None = None
    expires_at: str | None = None
    source: str = "derived"
    lifecycle: str = "active"


@dataclass(frozen=True)
class ContextFragment:
    module: str
    priority: int
    facts: tuple[ContextFact, ...] = ()
    instructions: tuple[str, ...] = ()
    source_event_ids: tuple[str, ...] = ()
    confidence: float = 1.0
    created_at: str = "1970-01-01T00:00:00+00:00"
    expires_at: str | None = None
    token_budget: int = 8000
    sensitivity: str = "public"
    # Control data only: scenes, blocked_terms/modules/keys, deny_sensitivities.
    # Never copied to the frame or model input.
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return json.loads(canonical(asdict(self)))


class ContextBudgetError(ValueError):
    """Mandatory boundaries/current message cannot fit; caller must use local fallback."""


class CompanionFrame(dict):
    """JSON-compatible frame. Legacy module aliases are diagnostic API projections only."""

    def model_view(self) -> dict:
        return {key: self[key] for key in (
            "version", "recentConversation", "processedFacts", "moduleInstructions",
        )}

    @property
    def generation_blocked(self) -> bool:
        return bool(self.get("internalMetadata", {}).get("generationBlocked"))

    def to_dict(self) -> dict:
        return json.loads(canonical(self))
