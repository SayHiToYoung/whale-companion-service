# -*- coding: utf-8 -*-
"""单用户共享记忆服务：SQLite 持久化、幂等批次与原子开场领取。"""
from __future__ import annotations

import hmac
import json
import mimetypes
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .dialogue_state import emotional_dialogue_state
from .companion_mind import build_companion_mind, contextual_fallback
from .companion_runtime.context_adapters import build_reply_frame as build_companion_frame
from .companion_runtime.companion_emotion import empty_companion_emotion, update_companion_emotion
from .companion_runtime.open_threads import build_open_threads
from .companion_runtime.proactive import decide_proactive_speak, evaluate_proactive_lifecycle
from .standing_knowledge import derive_standing_knowledge, standing_knowledge_context
from .companion_runtime.affinity import (
    apply_event as apply_affinity_event_fn,
    decay_state as decay_affinity_state,
    derive_boundary_state,
    empty_affinity_state,
    normalize_affinity_state,
    relationship_permissions,
    interaction_state_of,
    derive_affinity_event,
    stage_key_of,
)
from .companion_runtime.daily_life import (
    active_transient_events,
    advance_agenda,
    agenda_candidate_signals,
    compose_daily_state,
    transient_conditions,
    transient_life_events,
    empty_chronotype,
    generate_daily_conditions,
    meal_hunger_condition,
    period_of,
)
from .companion_runtime.expression_learning import learn_candidates
from .companion_runtime.story import (
    ArcStoryProvider,
    StoryArc,
    advance_arc,
    arc_age_days,
    apply_story_gates,
    choose_branch,
    eligible_templates,
    refuse_arc,
    select_current_arcs,
    start_arc,
    story_candidate_signals,
)
from .emotion import explicit_emotion_label
from .companion_llm import CompanionResponder
from .companion_llm import PROMPT_VERSION, SYSTEM_PROMPT, build_model_context, reply_violates_boundaries, reply_has_style_violation
from .memory_activation import SHARED_EXPERIENCE
from .memory_digest import DIGEST_KINDS, build_memory_digest, digest_periods
from .memory_lifecycle import (
    LIFECYCLE_STATUSES,
    decorate_memory_lifecycle,
    lifecycle_transition_from_text,
    memory_is_speakable,
)
from .memory_protocol import (
    ProtocolError,
    build_big_whale_opening,
    build_grounded_companion_reply,
    canonical_json,
    payload_hash,
    validate_batch,
)
from .memory_protocol import is_direct_memory_question
from .memory_protocol import is_prompt_to_speak
from .memory_retrieval import select_companion_memories
from .insight_drafts import build_insight_drafts
from .memory_protocol import is_reaction_to_companion
from .profile_memory import extract_profile_memories, profile_context, stable_id
from .persona_card import DEFAULT_PERSONA_CARD, compile_persona_card, validate_persona_card


MAX_REQUEST_BYTES = 512 * 1024
IDENTITY_VERSION = "echo-identity-v1"
CANONICAL_PET_ID = "shenshen"
# 被两个人聊到过这么多轮之后，一条情绪就不再只是那天的情绪，而是共同经历。
SHARED_EXPERIENCE_MENTIONS = 2
# 主动消息发出后这么久之内的用户回应，算"被钓出来的"，涨分额度另计。
SOLICITED_REPLY_WINDOW_MINUTES = 30.0

# 「活着的陪伴」可由用户自主调优的配置（控制台可读写，跨重启保留）。
DEFAULT_LIVING_CONFIG = {
    "proactive": {
        "dailyLimit": 8,          # 每日主动上限
        "minIntervalMinutes": 15, # 最小间隔（分钟），关系阶段会在此基础上再放宽
        "noResponsePauseAfter": 3,  # 连续 N 次主动无回应后暂停
    },
    "affinity": {
        "warmDelta": 2.0,         # 暖意事件 +分
        "violationDelta": -7.0,   # 越界事件 -分
    },
    "expressionLearning": {
        "enabled": True,          # 是否从对话自动学习表达候选
        "maxItems": 60,           # 表达规则上限
    },
    "chronotype": {
        "wakeMinute": 450,        # 07:30
        "sleepMinute": 1350,      # 22:30
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class MemoryConflictError(RuntimeError):
    pass


class MemoryRepository:
    def __init__(self, path: Path | str, responder: CompanionResponder | None = None,
                 clock=None) -> None:
        self.path = Path(path)
        self.responder = responder
        # 可注入的时钟。生活节律和主动闸门的行为完全取决于"现在几点"，
        # 仿真测试要能把一整天快进一遍，而不是靠改系统时间。
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self._reply_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._last_reply_source = ""
        self._last_reply_at = ""
        self._initialize()

    def _now(self) -> str:
        """仓库内部的"现在"。所有写入的时间戳都走这里，仿真才能整体前进。"""
        return self._clock().isoformat()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _judge(self):
        """返回 LLM 判断层的 judge 回调；无模型时返回 None（走确定性兜底）。"""
        responder = self.responder
        if responder is not None and getattr(responder, "available", False) and hasattr(responder, "judge_json"):
            return responder.judge_json
        return None

    def _initialize(self) -> None:
        with self._init_lock, self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS memory_events (
                    server_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    client_revision INTEGER NOT NULL,
                    layer TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    memory_time TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    UNIQUE(user_id, memory_id)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_stream
                    ON memory_events(user_id, server_seq);
                CREATE TABLE IF NOT EXISTS memory_batches (
                    user_id TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    latest_revision INTEGER NOT NULL,
                    payload_hash TEXT NOT NULL,
                    accepted_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, batch_id)
                );
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    message_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_latest
                    ON conversation_messages(user_id, message_seq DESC);
                CREATE TABLE IF NOT EXISTS reply_traces (
                    user_id TEXT NOT NULL,
                    reply_message_id TEXT NOT NULL,
                    trace_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, reply_message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_reply_traces_latest
                    ON reply_traces(user_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS insight_reviews (
                    user_id TEXT NOT NULL,
                    insight_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('confirmed', 'rejected')),
                    insight_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, insight_id)
                );
                CREATE TABLE IF NOT EXISTS companion_cursors (
                    user_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    last_memory_seq INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, device_id)
                );
                CREATE TABLE IF NOT EXISTS opening_claims (
                    user_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    claim_id TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, device_id, claim_id)
                );
                CREATE TABLE IF NOT EXISTS memory_digests (
                    user_id TEXT NOT NULL,
                    digest_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    status TEXT NOT NULL,
                    digest_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, digest_id)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_digests_period
                    ON memory_digests(user_id, kind, period_start DESC);
                CREATE TABLE IF NOT EXISTS memory_lifecycle (
                    user_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    source_type TEXT NOT NULL,
                    source_message_id TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, memory_id),
                    FOREIGN KEY(user_id, memory_id)
                        REFERENCES memory_events(user_id, memory_id)
                );
                CREATE TABLE IF NOT EXISTS memory_lifecycle_events (
                    lifecycle_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    source_type TEXT NOT NULL,
                    source_message_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, memory_id, status, source_type, source_message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_lifecycle_events
                    ON memory_lifecycle_events(user_id, memory_id, lifecycle_seq DESC);
                CREATE TABLE IF NOT EXISTS conversation_memory_refs (
                    user_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, message_id, memory_id),
                    FOREIGN KEY(user_id, memory_id)
                        REFERENCES memory_events(user_id, memory_id)
                );
                CREATE TABLE IF NOT EXISTS emotional_threads (
                    user_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    topic TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    mention_count INTEGER NOT NULL DEFAULT 0,
                    opened_at TEXT NOT NULL,
                    last_mentioned_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    episode_id TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(user_id, thread_id),
                    UNIQUE(user_id, memory_id),
                    FOREIGN KEY(user_id, memory_id)
                        REFERENCES memory_events(user_id, memory_id)
                );
                CREATE INDEX IF NOT EXISTS idx_emotional_threads_status
                    ON emotional_threads(user_id, status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS emotional_thread_memories (
                    user_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, thread_id, memory_id),
                    FOREIGN KEY(user_id, thread_id)
                        REFERENCES emotional_threads(user_id, thread_id),
                    FOREIGN KEY(user_id, memory_id)
                        REFERENCES memory_events(user_id, memory_id)
                );
                CREATE TABLE IF NOT EXISTS memory_mentions (
                    mention_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    mention_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, memory_id, message_id, mention_type),
                    FOREIGN KEY(user_id, memory_id)
                        REFERENCES memory_events(user_id, memory_id)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_mentions_memory
                    ON memory_mentions(user_id, memory_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS daily_episodes (
                    user_id TEXT NOT NULL,
                    episode_id TEXT NOT NULL,
                    local_date TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    memory_count INTEGER NOT NULL DEFAULT 0,
                    delivered_at TEXT NOT NULL,
                    consumed_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, episode_id),
                    UNIQUE(user_id, local_date)
                );
                CREATE TABLE IF NOT EXISTS daily_episode_memories (
                    user_id TEXT NOT NULL,
                    episode_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    PRIMARY KEY(user_id, episode_id, memory_id),
                    FOREIGN KEY(user_id, episode_id)
                        REFERENCES daily_episodes(user_id, episode_id),
                    FOREIGN KEY(user_id, memory_id)
                        REFERENCES memory_events(user_id, memory_id)
                );
                CREATE TABLE IF NOT EXISTS user_facts (
                    user_id TEXT NOT NULL,
                    fact_id TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    value_text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    importance REAL NOT NULL DEFAULT 0.7,
                    allow_proactive_mention INTEGER NOT NULL DEFAULT 1,
                    mention_count INTEGER NOT NULL DEFAULT 0,
                    last_mentioned_at TEXT NOT NULL DEFAULT '',
                    cooldown_until TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, fact_id)
                );
                CREATE INDEX IF NOT EXISTS idx_user_facts_current
                    ON user_facts(user_id, predicate, status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS user_fact_evidence (
                    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    fact_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    quote TEXT NOT NULL,
                    evidence_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, fact_id, message_id, evidence_type)
                );
                CREATE TABLE IF NOT EXISTS companion_boundaries (
                    user_id TEXT NOT NULL,
                    boundary_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    rule TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 100,
                    source_message_id TEXT NOT NULL,
                    source_quote TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, boundary_id)
                );
                CREATE TABLE IF NOT EXISTS persona_versions (
                    user_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    card_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, version)
                );
                CREATE TABLE IF NOT EXISTS companion_emotion_state (
                    user_id TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id)
                );
                CREATE TABLE IF NOT EXISTS relationship_state (
                    user_id TEXT NOT NULL,
                    score REAL NOT NULL DEFAULT 0,
                    stage TEXT NOT NULL DEFAULT 'acquaintance',
                    interaction TEXT NOT NULL DEFAULT 'relaxed',
                    familiarity REAL NOT NULL DEFAULT 0,
                    trust REAL NOT NULL DEFAULT 0,
                    warmth REAL NOT NULL DEFAULT 0,
                    boundary_state TEXT NOT NULL DEFAULT 'open',
                    updated_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(user_id)
                );
                CREATE TABLE IF NOT EXISTS relationship_ledger (
                    user_id TEXT NOT NULL,
                    event_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    delta REAL NOT NULL,
                    solicited INTEGER NOT NULL DEFAULT 0,
                    at TEXT NOT NULL,
                    PRIMARY KEY(user_id, event_key, at)
                );
                CREATE TABLE IF NOT EXISTS daily_state (
                    user_id TEXT NOT NULL,
                    state_date TEXT NOT NULL,
                    conditions_json TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, state_date)
                );
                CREATE TABLE IF NOT EXISTS story_arcs (
                    user_id TEXT NOT NULL,
                    arc_id TEXT NOT NULL,
                    arc_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, arc_id)
                );
                CREATE TABLE IF NOT EXISTS proactive_candidates (
                    user_id TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    topic TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    decision TEXT NOT NULL DEFAULT '',
                    veto_reason TEXT NOT NULL DEFAULT '',
                    score REAL NOT NULL DEFAULT 0,
                    window_start TEXT NOT NULL DEFAULT '',
                    best_until TEXT NOT NULL DEFAULT '',
                    expires_at TEXT NOT NULL DEFAULT '',
                    deliver_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    sent_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(user_id, signature)
                );
                CREATE INDEX IF NOT EXISTS idx_proactive_candidates_sent
                    ON proactive_candidates(user_id, status, sent_at DESC);
                CREATE TABLE IF NOT EXISTS chronotype_profile (
                    user_id TEXT NOT NULL,
                    profile_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id)
                );
                CREATE TABLE IF NOT EXISTS self_timeline_events (
                    user_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    type TEXT NOT NULL,
                    when_text TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'confirmed',
                    keywords TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(user_id, event_id)
                );
                CREATE INDEX IF NOT EXISTS idx_self_timeline_ts
                    ON self_timeline_events(user_id, ts DESC);
                CREATE TABLE IF NOT EXISTS expression_rules (
                    user_id TEXT NOT NULL,
                    rule_id TEXT NOT NULL,
                    pattern TEXT NOT NULL,
                    instruction TEXT NOT NULL DEFAULT '',
                    scene TEXT NOT NULL DEFAULT 'casual',
                    kind TEXT NOT NULL DEFAULT 'style',
                    status TEXT NOT NULL DEFAULT 'pending',
                    usage_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, rule_id)
                );
                CREATE TABLE IF NOT EXISTS living_config (
                    user_id TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id)
                );
            """)
            thread_columns = {row[1] for row in db.execute("PRAGMA table_info(emotional_threads)")}
            for name, definition in (
                ("need", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("follow_up_hint", "TEXT NOT NULL DEFAULT ''"),
                ("last_user_message_id", "TEXT NOT NULL DEFAULT ''"),
                ("episode_id", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in thread_columns:
                    db.execute(f"ALTER TABLE emotional_threads ADD COLUMN {name} {definition}")
            # 亲密度从"一个数字"扩到七维。老库只有 score/stage/interaction，
            # 补列即可继续用，零值正是它们应有的起点。
            relationship_columns = {row[1] for row in db.execute("PRAGMA table_info(relationship_state)")}
            for name, definition in (
                ("familiarity", "REAL NOT NULL DEFAULT 0"),
                ("trust", "REAL NOT NULL DEFAULT 0"),
                ("warmth", "REAL NOT NULL DEFAULT 0"),
                ("boundary_state", "TEXT NOT NULL DEFAULT 'open'"),
            ):
                if name not in relationship_columns:
                    db.execute(f"ALTER TABLE relationship_state ADD COLUMN {name} {definition}")
            ledger_columns = {row[1] for row in db.execute("PRAGMA table_info(relationship_ledger)")}
            if "solicited" not in ledger_columns:
                db.execute("ALTER TABLE relationship_ledger ADD COLUMN solicited INTEGER NOT NULL DEFAULT 0")
            db.execute(
                """INSERT OR IGNORE INTO emotional_thread_memories
                   (user_id, thread_id, memory_id, created_at)
                   SELECT user_id, thread_id, memory_id, opened_at FROM emotional_threads"""
            )
            # 兼容升级前已入库的事实：只补齐缺失的每日单元，不改已有消费状态。
            for row in db.execute(
                "SELECT user_id, memory_id, payload_json, received_at FROM memory_events"
            ).fetchall():
                memory = json.loads(row["payload_json"])
                local_date = self._memory_local_date(memory)
                if not local_date:
                    continue
                episode_id = f"daily:{local_date}"
                db.execute(
                    """INSERT OR IGNORE INTO daily_episodes
                       (user_id, episode_id, local_date, revision, status, memory_count,
                        delivered_at, consumed_at, updated_at)
                       VALUES (?, ?, ?, 1, 'delivered', 0, ?, '', ?)""",
                    (row["user_id"], episode_id, local_date, row["received_at"], row["received_at"]),
                )
                db.execute(
                    """INSERT OR IGNORE INTO daily_episode_memories
                       (user_id, episode_id, memory_id) VALUES (?, ?, ?)""",
                    (row["user_id"], episode_id, row["memory_id"]),
                )
            db.execute(
                """UPDATE daily_episodes SET memory_count=(
                     SELECT COUNT(*) FROM daily_episode_memories AS membership
                     WHERE membership.user_id=daily_episodes.user_id
                       AND membership.episode_id=daily_episodes.episode_id)"""
            )

    @staticmethod
    def _memory_time(memory: dict) -> str:
        for key in ("occurredAt", "endedAt", "startedAt", "createdAt"):
            if memory.get(key):
                return str(memory[key])[:80]
        return ""

    @staticmethod
    def _memory_local_date(memory: dict) -> str:
        explicit = str(memory.get("localDate") or "").strip()
        if explicit:
            return explicit
        for key in ("occurredAt", "endedAt", "startedAt", "createdAt"):
            raw = str(memory.get(key) or "").strip()
            if not raw:
                continue
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone().date().isoformat()
            except ValueError:
                continue
        return ""

    def _daily_episodes(self, memories: list[dict]) -> list[dict]:
        grouped: dict[str, list[dict]] = {}
        for memory in memories:
            local_date = self._memory_local_date(memory)
            if local_date:
                grouped.setdefault(local_date, []).append(memory)
        return [{
            "id": f"daily:{local_date}",
            "date": local_date,
            "memoryCount": len(items),
            "memoryIds": [str(item.get("id") or "") for item in items],
            "layers": {
                layer: sum(1 for item in items if item.get("layer") == layer)
                for layer in ("L1", "L2", "L3")
            },
            "status": "ready",
        } for local_date, items in sorted(grouped.items(), reverse=True)]

    @staticmethod
    def _lifecycle_map(db: sqlite3.Connection, user_id: str) -> dict[str, dict]:
        """生命周期覆盖层：存下来的用户意图，加上服务端才知道的强化信号。

        提及次数和最近一次被提及的时间来自既有的 `memory_mentions`，不另立一份计数；
        记忆类型在这里被服务端改写，因为只有服务端知道哪几条已经被两个人反复聊过——
        那样的记忆不再是一次情绪，而是共同经历，理应用更长的半衰期。
        """
        overlay: dict[str, dict] = {}
        for row in db.execute(
            """SELECT memory_id, status, reason, source_type, source_message_id, updated_at
               FROM memory_lifecycle WHERE user_id=?""",
            (user_id,),
        ).fetchall():
            overlay[str(row["memory_id"])] = {
                "status": row["status"],
                "reason": row["reason"],
                "sourceType": row["source_type"],
                "sourceMessageId": row["source_message_id"],
                "updatedAt": row["updated_at"],
            }
        for row in db.execute(
            """SELECT memory_id, COUNT(*) AS mentions, MAX(created_at) AS last_mentioned
               FROM memory_mentions WHERE user_id=? GROUP BY memory_id""",
            (user_id,),
        ).fetchall():
            entry = overlay.setdefault(str(row["memory_id"]), {})
            entry["mentionCount"] = int(row["mentions"] or 0)
            entry["lastMentionedAt"] = str(row["last_mentioned"] or "")
        for row in db.execute(
            """SELECT membership.memory_id AS memory_id,
                      MAX(thread.mention_count) AS mentions
               FROM emotional_thread_memories AS membership
               JOIN emotional_threads AS thread
                 ON thread.user_id=membership.user_id AND thread.thread_id=membership.thread_id
               WHERE membership.user_id=? GROUP BY membership.memory_id""",
            (user_id,),
        ).fetchall():
            if int(row["mentions"] or 0) >= SHARED_EXPERIENCE_MENTIONS:
                overlay.setdefault(str(row["memory_id"]), {})["memoryClass"] = SHARED_EXPERIENCE
        return overlay

    def _decorate_memory_rows(
        self,
        db: sqlite3.Connection,
        user_id: str,
        rows: list[sqlite3.Row],
    ) -> list[dict]:
        lifecycle = self._lifecycle_map(db, user_id)
        memories = []
        for row in rows:
            memory = json.loads(row["payload_json"])
            memories.append(decorate_memory_lifecycle(
                memory,
                lifecycle.get(str(memory.get("id") or "")),
            ))
        return memories

    @staticmethod
    def _set_lifecycle_locked(
        db: sqlite3.Connection,
        *,
        user_id: str,
        memory_id: str,
        status: str,
        reason: str,
        source_type: str,
        source_message_id: str,
        now: str,
    ) -> dict:
        if status not in LIFECYCLE_STATUSES:
            raise ProtocolError("invalid lifecycle status")
        exists = db.execute(
            "SELECT 1 FROM memory_events WHERE user_id=? AND memory_id=?",
            (user_id, memory_id),
        ).fetchone()
        if exists is None:
            raise ProtocolError("memory not found")
        db.execute(
            """INSERT INTO memory_lifecycle
               (user_id, memory_id, status, reason, source_type, source_message_id, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, memory_id) DO UPDATE SET
                 status=excluded.status,
                 reason=excluded.reason,
                 source_type=excluded.source_type,
                 source_message_id=excluded.source_message_id,
                 updated_at=excluded.updated_at""",
            (user_id, memory_id, status, reason, source_type, source_message_id, now),
        )
        db.execute(
            """INSERT OR IGNORE INTO memory_lifecycle_events
               (user_id, memory_id, status, reason, source_type, source_message_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, memory_id, status, reason, source_type, source_message_id, now),
        )
        return {
            "memoryId": memory_id,
            "status": status,
            "reason": reason,
            "sourceType": source_type,
            "sourceMessageId": source_message_id,
            "updatedAt": now,
        }

    @staticmethod
    def _latest_focus_memory_id(db: sqlite3.Connection, user_id: str) -> str:
        row = db.execute(
            """SELECT ref.memory_id
               FROM conversation_messages AS message
               JOIN conversation_memory_refs AS ref
                 ON ref.user_id=message.user_id AND ref.message_id=message.message_id
               LEFT JOIN memory_lifecycle AS lifecycle
                 ON lifecycle.user_id=ref.user_id AND lifecycle.memory_id=ref.memory_id
               WHERE message.user_id=? AND message.role='assistant'
                 AND COALESCE(lifecycle.status, 'active') NOT IN ('resolved', 'dormant', 'suppressed')
               ORDER BY message.message_seq DESC LIMIT 1""",
            (user_id,),
        ).fetchone()
        return str(row["memory_id"] or "") if row else ""

    @staticmethod
    def _open_emotional_thread_locked(
        db: sqlite3.Connection, *, user_id: str, memory: dict, now: str
    ) -> None:
        memory_id = str(memory.get("id") or "")
        if not memory_id or memory.get("layer") != "L3":
            return
        topic = str(memory.get("quote") or "").strip()[:240]
        continuity = any(token in topic for token in ("还是", "又", "仍然", "还在", "这件事", "那个事"))
        existing = db.execute(
            """SELECT thread_id FROM emotional_threads
               WHERE user_id=? AND label=? AND status IN ('active', 'acknowledged')
               ORDER BY updated_at DESC LIMIT 1""",
            (user_id, str(memory.get("label") or "")),
        ).fetchone() if continuity else None
        thread_id = str(existing["thread_id"]) if existing else f"thread:{memory_id}"
        need = "advice" if any(token in topic for token in ("怎么办", "怎么做", "给我建议", "帮我想")) else (
            "listening" if any(token in topic for token in ("不需要建议", "听我说", "陪陪我", "别分析")) else (
                "vent" if any(token in topic for token in ("吐槽", "烦死", "气死")) else "unknown"
            )
        )
        follow_up = next((token for token in ("明天再问我", "过两天再问我", "周一再问我", "周五再问我", "之后再问我") if token in topic), "")
        source_message_id = str(memory.get("sourceMessageId") or "")
        episode_id = f"daily:{MemoryRepository._memory_local_date(memory)}" if MemoryRepository._memory_local_date(memory) else ""
        db.execute(
            """INSERT OR IGNORE INTO emotional_threads
               (user_id, thread_id, memory_id, label, topic, status,
                mention_count, opened_at, last_mentioned_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'active', 0, ?, '', ?)""",
            (user_id, thread_id, memory_id, str(memory.get("label") or ""), topic, now, now),
        )
        db.execute(
            """INSERT OR IGNORE INTO emotional_thread_memories
               (user_id, thread_id, memory_id, created_at) VALUES (?, ?, ?, ?)""",
            (user_id, thread_id, memory_id, now),
        )
        db.execute(
            """UPDATE emotional_threads SET topic=?,
                 need=CASE WHEN ?='unknown' THEN need ELSE ? END,
                 follow_up_hint=CASE WHEN ?='' THEN follow_up_hint ELSE ? END,
                 last_user_message_id=?, episode_id=?, updated_at=?
               WHERE user_id=? AND thread_id=?""",
            (topic, need, need, follow_up, follow_up, source_message_id, episode_id, now, user_id, thread_id),
        )

    @staticmethod
    def _record_memory_mention_locked(
        db: sqlite3.Connection, *, user_id: str, memory_id: str,
        message_id: str, mention_type: str, now: str, touch_thread: bool = True,
    ) -> None:
        if not memory_id:
            return
        cursor = db.execute(
            """INSERT OR IGNORE INTO memory_mentions
               (user_id, memory_id, message_id, mention_type, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, memory_id, message_id, mention_type, now),
        )
        # 线程的 mention_count 数的是"这件事被聊到过几轮"。同一轮里派生出来的强化信号
        # 已经算在这一轮里了，再加一次就把一次对话记成两次。
        if cursor.rowcount and touch_thread:
            db.execute(
                """UPDATE emotional_threads SET
                     status=CASE WHEN status='active' THEN 'acknowledged' ELSE status END,
                     mention_count=mention_count+1,
                     last_mentioned_at=?, updated_at=?
                   WHERE user_id=? AND thread_id IN (
                     SELECT thread_id FROM emotional_thread_memories WHERE user_id=? AND memory_id=?)""",
                (now, now, user_id, user_id, memory_id),
            )

    @staticmethod
    def _update_emotional_thread_status_locked(
        db: sqlite3.Connection, *, user_id: str, memory_id: str,
        status: str, now: str,
    ) -> None:
        thread_status = status if status in {"active", "resolved", "dormant", "suppressed", "corrected"} else ""
        if thread_status:
            db.execute(
                """UPDATE emotional_threads SET status=?, updated_at=?
                   WHERE user_id=? AND thread_id IN (
                     SELECT thread_id FROM emotional_thread_memories WHERE user_id=? AND memory_id=?)""",
                (thread_status, now, user_id, user_id, memory_id),
            )

    def update_lifecycle(self, payload: dict) -> dict:
        user_id = str(payload.get("userId") or "").strip()
        memory_id = str(payload.get("memoryId") or "").strip()
        status = str(payload.get("status") or "").strip()
        reason = str(payload.get("reason") or "manual_update").strip()[:160]
        if not user_id or not memory_id or status not in LIFECYCLE_STATUSES:
            raise ProtocolError("invalid lifecycle update")
        now = self._now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            lifecycle = self._set_lifecycle_locked(
                db,
                user_id=user_id,
                memory_id=memory_id,
                status=status,
                reason=reason,
                source_type="user_action",
                source_message_id="",
                now=now,
            )
            self._update_emotional_thread_status_locked(
                db, user_id=user_id, memory_id=memory_id, status=status, now=now,
            )
        return {"accepted": True, "lifecycle": lifecycle}

    def reinforce_memory(self, payload: dict) -> dict:
        """记一次"这条又被提起来了"。

        强化不改状态、不改内容，只把这条记忆重新拉近现在，并让它以后衰减得更慢。
        用户说过不要再提的事不会因为被提起而复活——那是边界，不是遗忘。
        """
        user_id = str(payload.get("userId") or "").strip()
        memory_id = str(payload.get("memoryId") or "").strip()
        message_id = str(payload.get("messageId") or "").strip()
        mention_type = str(payload.get("mentionType") or "user_recall").strip()[:32]
        if not user_id or not memory_id or mention_type not in {"user_recall", "reply", "opening"}:
            raise ProtocolError("invalid memory reinforcement")
        now = self._now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            exists = db.execute(
                "SELECT payload_json FROM memory_events WHERE user_id=? AND memory_id=?",
                (user_id, memory_id),
            ).fetchone()
            if exists is None:
                raise ProtocolError("memory not found")
            self._record_memory_mention_locked(
                db, user_id=user_id, memory_id=memory_id,
                message_id=message_id or f"reinforce:{now}", mention_type=mention_type,
                now=now, touch_thread=False,
            )
            memory = self._decorate_memory_rows(db, user_id, [exists])[0]
        return {
            "accepted": True,
            "memoryId": memory_id,
            "lifecycle": memory["lifecycle"],
        }

    def _rebuild_memory_digests_locked(
        self, db: sqlite3.Connection, user_id: str, memories: list[dict], now: str,
    ) -> list[dict]:
        """重建这批记忆所触及的日/周摘要骨架。

        只重建受影响的时间段，且重建是幂等的：摘要正文由后续阶段填写，
        本阶段写入的每个字段都能从记忆本身重新算出来。

        重算一个时间段要读全量记忆，因为客户端可以用 `localDate` 声明一条记忆属于哪一天，
        单靠时间戳范围会漏掉它。这与 `_initialize` 的每日单元补齐是同一量级的开销。
        """
        periods = digest_periods(memories)
        if not periods:
            return []
        rows = db.execute(
            "SELECT payload_json FROM memory_events WHERE user_id=? ORDER BY server_seq ASC",
            (user_id,),
        ).fetchall()
        known = self._decorate_memory_rows(db, user_id, rows)
        digests = []
        for kind, period_start in periods:
            digest = build_memory_digest(kind, known, period_start=period_start)
            db.execute(
                """INSERT INTO memory_digests
                   (user_id, digest_id, kind, period_start, period_end, status,
                    digest_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, digest_id) DO UPDATE SET
                     period_end=excluded.period_end,
                     status=excluded.status,
                     digest_json=excluded.digest_json,
                     updated_at=excluded.updated_at""",
                (user_id, digest["digestId"], kind, digest["periodStart"], digest["periodEnd"],
                 digest["status"], canonical_json(digest), now, now),
            )
            digests.append(digest)
        return digests

    def memory_digests(self, user_id: str, kind: str = "", limit: int = 60) -> dict:
        user_id = str(user_id or "").strip()
        if not user_id:
            raise ProtocolError("userId required")
        kind = str(kind or "").strip()
        if kind and kind not in DIGEST_KINDS:
            raise ProtocolError("unsupported digest kind")
        with self._connect() as db:
            rows = db.execute(
                """SELECT digest_json FROM memory_digests
                   WHERE user_id=? AND (?='' OR kind=?)
                   ORDER BY period_start DESC, kind ASC LIMIT ?""",
                (user_id, kind, kind, max(1, min(365, int(limit)))),
            ).fetchall()
        return {"userId": user_id, "digests": [json.loads(row["digest_json"]) for row in rows]}

    def ingest_batch(self, payload: dict) -> dict:
        batch = validate_batch(payload)
        body_hash = payload_hash({
            "latestRevision": batch["latestRevision"],
            "memories": batch["memories"],
        })
        now = self._now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing_batch = db.execute(
                "SELECT payload_hash FROM memory_batches WHERE user_id=? AND batch_id=?",
                (batch["userId"], batch["batchId"]),
            ).fetchone()
            if existing_batch is not None:
                if existing_batch["payload_hash"] != body_hash:
                    raise MemoryConflictError("batchId already exists with different payload")
                latest_seq = db.execute(
                    "SELECT COALESCE(MAX(server_seq), 0) FROM memory_events WHERE user_id=?",
                    (batch["userId"],),
                ).fetchone()[0]
                return {
                    "accepted": True,
                    "duplicate": True,
                    "batchId": batch["batchId"],
                    "acceptedCount": 0,
                    "latestServerSeq": int(latest_seq),
                    "receivedAt": now,
                }
            accepted = 0
            for memory in batch["memories"]:
                encoded = canonical_json(memory)
                digest = payload_hash(memory)
                existing = db.execute(
                    "SELECT payload_hash FROM memory_events WHERE user_id=? AND memory_id=?",
                    (batch["userId"], memory["id"]),
                ).fetchone()
                if existing is not None:
                    if existing["payload_hash"] != digest:
                        raise MemoryConflictError("memoryId already exists with different payload")
                    continue
                db.execute(
                    """INSERT INTO memory_events
                       (user_id, device_id, memory_id, client_revision, layer, kind,
                        memory_time, payload_json, payload_hash, received_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        batch["userId"], batch["deviceId"], memory["id"], memory["revision"],
                        memory["layer"], str(memory.get("kind") or "")[:80],
                        self._memory_time(memory), encoded, digest, now,
                    ),
                )
                self._open_emotional_thread_locked(
                    db, user_id=batch["userId"], memory=memory, now=now,
                )
                local_date = self._memory_local_date(memory)
                if local_date:
                    episode_id = f"daily:{local_date}"
                    db.execute(
                        """INSERT INTO daily_episodes
                           (user_id, episode_id, local_date, revision, status,
                            memory_count, delivered_at, consumed_at, updated_at)
                           VALUES (?, ?, ?, 1, 'delivered', 0, ?, '', ?)
                           ON CONFLICT(user_id, episode_id) DO UPDATE SET
                             revision=revision+1, status='delivered',
                             delivered_at=excluded.delivered_at, consumed_at='', updated_at=excluded.updated_at""",
                        (batch["userId"], episode_id, local_date, now, now),
                    )
                    db.execute(
                        """INSERT OR IGNORE INTO daily_episode_memories
                           (user_id, episode_id, memory_id) VALUES (?, ?, ?)""",
                        (batch["userId"], episode_id, memory["id"]),
                    )
                    db.execute(
                        """UPDATE daily_episodes SET memory_count=(
                             SELECT COUNT(*) FROM daily_episode_memories
                             WHERE user_id=? AND episode_id=?)
                           WHERE user_id=? AND episode_id=?""",
                        (batch["userId"], episode_id, batch["userId"], episode_id),
                    )
                accepted += 1
            db.execute(
                """INSERT INTO memory_batches
                   (user_id, batch_id, device_id, latest_revision, payload_hash, accepted_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    batch["userId"], batch["batchId"], batch["deviceId"],
                    batch["latestRevision"], body_hash, now,
                ),
            )
            # 摘要骨架跟着事实一起落库，纯确定性统计，不调用任何模型。
            # 一批全是已知记忆时不重建：摘要只由内容决定，没有新内容就没有新摘要。
            rebuilt = self._rebuild_memory_digests_locked(
                db, batch["userId"], batch["memories"], now,
            ) if accepted else []
            latest_seq = db.execute(
                "SELECT COALESCE(MAX(server_seq), 0) FROM memory_events WHERE user_id=?",
                (batch["userId"],),
            ).fetchone()[0]
            return {
                "accepted": True,
                "duplicate": accepted == 0,
                "digestIds": [row["digestId"] for row in rebuilt],
                "batchId": batch["batchId"],
                "acceptedCount": accepted,
                "latestServerSeq": int(latest_seq),
                "receivedAt": now,
            }

    def ingest_episode(self, payload: dict) -> dict:
        user_id = str(payload.get("userId") or "").strip()
        episode_id = str(payload.get("episodeId") or "").strip()
        local_date = str(payload.get("date") or "").strip()
        memory_ids = payload.get("memoryIds")
        try:
            revision = int(payload.get("revision") or 0)
        except (TypeError, ValueError):
            revision = 0
        if not user_id or episode_id != f"daily:{local_date}" or revision < 1 or not isinstance(memory_ids, list):
            raise ProtocolError("invalid daily episode manifest")
        now = self._now()
        with self._connect() as db:
            existing_ids = {row[0] for row in db.execute(
                "SELECT memory_id FROM memory_events WHERE user_id=?", (user_id,)
            ).fetchall()}
            if any(str(value) not in existing_ids for value in memory_ids):
                raise ProtocolError("episode references undelivered memory")
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT INTO daily_episodes
                   (user_id, episode_id, local_date, revision, status, memory_count,
                    delivered_at, consumed_at, updated_at)
                   VALUES (?, ?, ?, ?, 'delivered', ?, ?, '', ?)
                   ON CONFLICT(user_id, episode_id) DO UPDATE SET
                     revision=excluded.revision, status='delivered',
                     memory_count=excluded.memory_count, delivered_at=excluded.delivered_at,
                     consumed_at='', updated_at=excluded.updated_at""",
                (user_id, episode_id, local_date, revision, len(memory_ids), now, now),
            )
            # The manifest is authoritative. Batch ingestion may have created a
            # compatibility episode, but an explicit DailyEpisode must replace
            # that provisional membership instead of inheriting extra memories.
            db.execute(
                "DELETE FROM daily_episode_memories WHERE user_id=? AND episode_id=?",
                (user_id, episode_id),
            )
            db.executemany(
                """INSERT INTO daily_episode_memories
                   (user_id, episode_id, memory_id) VALUES (?, ?, ?)""",
                [(user_id, episode_id, str(memory_id)) for memory_id in memory_ids],
            )
        return {"accepted": True, "episodeId": episode_id, "revision": revision, "status": "delivered", "deliveredAt": now}

    def stream(self, user_id: str, after_server_seq: int = 0, limit: int = 200) -> dict:
        with self._connect() as db:
            rows = db.execute(
                """SELECT server_seq, device_id, memory_id, payload_json, received_at
                   FROM memory_events WHERE user_id=? AND server_seq>?
                   ORDER BY server_seq ASC LIMIT ?""",
                (user_id, max(0, int(after_server_seq)), max(1, min(500, int(limit)))),
            ).fetchall()
            memories = self._decorate_memory_rows(db, user_id, rows)
        for memory, row in zip(memories, rows):
            memory["serverSeq"] = int(row["server_seq"])
            memory["sourceDeviceId"] = row["device_id"]
            memory["receivedAt"] = row["received_at"]
        return {
            "memories": memories,
            "nextServerSeq": int(rows[-1]["server_seq"]) if rows else max(0, int(after_server_seq)),
            "hasMore": len(rows) >= max(1, min(500, int(limit))),
        }

    def _extract_profile_memories_locked(
        self, db: sqlite3.Connection, *, user_id: str, message_id: str, text: str, now: str,
    ) -> None:
        extracted = extract_profile_memories(text)
        for item in extracted["facts"]:
            predicate = str(item["predicate"])
            value = str(item["value"])
            fact_id = stable_id("fact", user_id, predicate, value)
            previous = db.execute(
                """SELECT fact_id, value_text FROM user_facts
                   WHERE user_id=? AND predicate=? AND status='confirmed' AND fact_id<>?""",
                (user_id, predicate, fact_id),
            ).fetchall()
            for row in previous:
                db.execute(
                    "UPDATE user_facts SET status='corrected', updated_at=? WHERE user_id=? AND fact_id=?",
                    (now, user_id, row["fact_id"]),
                )
                db.execute(
                    """INSERT OR IGNORE INTO user_fact_evidence
                       (user_id, fact_id, message_id, quote, evidence_type, created_at)
                       VALUES (?, ?, ?, ?, 'contradiction', ?)""",
                    (user_id, row["fact_id"], message_id, text, now),
                )
            db.execute(
                """INSERT INTO user_facts
                   (user_id, fact_id, predicate, value_text, status, confidence,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'confirmed', ?, ?, ?)
                   ON CONFLICT(user_id, fact_id) DO UPDATE SET
                     status='confirmed', confidence=MAX(confidence, excluded.confidence),
                     updated_at=excluded.updated_at""",
                (user_id, fact_id, predicate, value, float(item["confidence"]), now, now),
            )
            db.execute(
                """INSERT OR IGNORE INTO user_fact_evidence
                   (user_id, fact_id, message_id, quote, evidence_type, created_at)
                   VALUES (?, ?, ?, ?, 'support', ?)""",
                (user_id, fact_id, message_id, text, now),
            )
        for item in extracted["boundaries"]:
            kind, rule = str(item["kind"]), str(item["rule"])
            boundary_id = stable_id("boundary", user_id, kind, rule)
            db.execute(
                """INSERT INTO companion_boundaries
                   (user_id, boundary_id, kind, rule, status, priority,
                    source_message_id, source_quote, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, boundary_id) DO UPDATE SET
                     status='active', source_message_id=excluded.source_message_id,
                     source_quote=excluded.source_quote, updated_at=excluded.updated_at""",
                (user_id, boundary_id, kind, rule, int(item["priority"]), message_id, text, now, now),
            )

    def _open_threads_locked(self, db: sqlite3.Connection, user_id: str) -> list[dict]:
        """仍然开着的情绪线程，供陪伴帧跨会话延续。"""
        rows = db.execute(
            """SELECT thread_id, label, topic, status, need, follow_up_hint,
                      mention_count, opened_at, last_mentioned_at, updated_at
               FROM emotional_threads
               WHERE user_id=? AND status IN ('active', 'acknowledged')
               ORDER BY updated_at DESC LIMIT 20""",
            (user_id,),
        ).fetchall()
        return [{
            "threadId": row["thread_id"], "label": row["label"], "topic": row["topic"],
            "status": row["status"], "need": row["need"],
            "followUpHint": row["follow_up_hint"],
            "mentionCount": int(row["mention_count"] or 0),
            "openedAt": row["opened_at"], "lastMentionedAt": row["last_mentioned_at"],
            "updatedAt": row["updated_at"],
        } for row in rows]

    def _standing_knowledge_locked(self, db: sqlite3.Connection, user_id: str) -> list[dict]:
        """常驻认知只在读取时推导，不落第二份副本。

        来源（confirmed 事实、active 边界、用户亲口开启的线程）本身就是权威且
        可纠错的；再存一份会和来源漂移。
        """
        facts, boundaries = self._profile_memories_locked(db, user_id)
        threads = self._open_threads_locked(db, user_id)
        return derive_standing_knowledge(facts, boundaries, threads)

    def _profile_memories_locked(self, db: sqlite3.Connection, user_id: str) -> tuple[list[dict], list[dict]]:
        fact_rows = db.execute(
            """SELECT fact_id, predicate, value_text, status, confidence, importance,
                      allow_proactive_mention, mention_count, last_mentioned_at,
                      cooldown_until, created_at, updated_at
               FROM user_facts WHERE user_id=? ORDER BY updated_at DESC""",
            (user_id,),
        ).fetchall()
        evidence_rows = db.execute(
            """SELECT fact_id, message_id, quote, evidence_type, created_at
               FROM user_fact_evidence WHERE user_id=? ORDER BY evidence_id DESC""",
            (user_id,),
        ).fetchall()
        evidence: dict[str, list[dict]] = {}
        for row in evidence_rows:
            evidence.setdefault(str(row["fact_id"]), []).append({
                "messageId": row["message_id"], "quote": row["quote"],
                "type": row["evidence_type"], "createdAt": row["created_at"],
            })
        facts = [{
            "id": row["fact_id"], "predicate": row["predicate"], "value": row["value_text"],
            "status": row["status"], "confidence": float(row["confidence"]),
            "importance": float(row["importance"]),
            "allowProactiveMention": bool(row["allow_proactive_mention"]),
            "mentionCount": int(row["mention_count"]), "lastMentionedAt": row["last_mentioned_at"],
            "cooldownUntil": row["cooldown_until"], "createdAt": row["created_at"],
            "updatedAt": row["updated_at"], "evidence": evidence.get(str(row["fact_id"]), []),
        } for row in fact_rows]
        boundaries = [{
            "id": row["boundary_id"], "kind": row["kind"], "rule": row["rule"],
            "status": row["status"], "priority": int(row["priority"]),
            "sourceMessageId": row["source_message_id"], "sourceQuote": row["source_quote"],
            "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        } for row in db.execute(
            """SELECT boundary_id, kind, rule, status, priority, source_message_id,
                      source_quote, created_at, updated_at
               FROM companion_boundaries WHERE user_id=? ORDER BY priority DESC, updated_at DESC""",
            (user_id,),
        ).fetchall()]
        return facts, boundaries

    def profile_memories(self, user_id: str) -> dict:
        if not str(user_id).strip():
            raise ProtocolError("userId required")
        with self._connect() as db:
            facts, boundaries = self._profile_memories_locked(db, user_id)
        return {"userId": user_id, "userFacts": facts, "boundaries": boundaries}

    def _persona_card_locked(self, db: sqlite3.Connection, user_id: str) -> dict:
        rows = db.execute(
            """SELECT version, status, card_json, created_at, updated_at
               FROM persona_versions WHERE user_id=? ORDER BY version DESC""",
            (user_id,),
        ).fetchall()
        active_row = next((row for row in rows if row["status"] == "published"), None)
        draft_row = next((row for row in rows if row["status"] == "draft"), None)
        active = json.loads(active_row["card_json"]) if active_row else DEFAULT_PERSONA_CARD
        return {
            "userId": user_id,
            "activeVersion": int(active_row["version"]) if active_row else 1,
            "active": active,
            "draftVersion": int(draft_row["version"]) if draft_row else None,
            "draft": json.loads(draft_row["card_json"]) if draft_row else None,
            "history": [{"version": int(row["version"]), "status": row["status"], "updatedAt": row["updated_at"]} for row in rows],
            "compiledPrompt": compile_persona_card(active),
        }

    def persona_card(self, user_id: str) -> dict:
        if not str(user_id).strip():
            raise ProtocolError("userId required")
        with self._connect() as db:
            return self._persona_card_locked(db, user_id)

    def update_persona_card(self, payload: dict) -> dict:
        user_id = str(payload.get("userId") or "").strip()
        action = str(payload.get("action") or "").strip()
        if not user_id or action not in {"saveDraft", "publish", "rollback"}:
            raise ProtocolError("invalid persona action")
        now = self._now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._persona_card_locked(db, user_id)
            if not current["history"]:
                db.execute(
                    """INSERT INTO persona_versions
                       (user_id, version, status, card_json, created_at, updated_at)
                       VALUES (?, 1, 'published', ?, ?, ?)""",
                    (user_id, canonical_json(DEFAULT_PERSONA_CARD), now, now),
                )
                current = self._persona_card_locked(db, user_id)
            if action == "saveDraft":
                try:
                    card = validate_persona_card(payload.get("card"))
                except ValueError as exc:
                    raise ProtocolError(str(exc)) from exc
                version = max([item["version"] for item in current["history"]] or [current["activeVersion"]]) + 1
                db.execute("DELETE FROM persona_versions WHERE user_id=? AND status='draft'", (user_id,))
                db.execute(
                    """INSERT INTO persona_versions
                       (user_id, version, status, card_json, created_at, updated_at)
                       VALUES (?, ?, 'draft', ?, ?, ?)""",
                    (user_id, version, canonical_json(card), now, now),
                )
            elif action == "publish":
                draft = db.execute(
                    "SELECT version FROM persona_versions WHERE user_id=? AND status='draft' ORDER BY version DESC LIMIT 1",
                    (user_id,),
                ).fetchone()
                if draft is None:
                    raise ProtocolError("no persona draft to publish")
                db.execute("UPDATE persona_versions SET status='archived', updated_at=? WHERE user_id=? AND status='published'", (now, user_id))
                db.execute("UPDATE persona_versions SET status='published', updated_at=? WHERE user_id=? AND version=?", (now, user_id, draft["version"]))
            else:
                target = int(payload.get("version") or 0)
                row = db.execute(
                    "SELECT card_json FROM persona_versions WHERE user_id=? AND version=?",
                    (user_id, target),
                ).fetchone()
                if row is None:
                    raise ProtocolError("persona version not found")
                version = max([item["version"] for item in current["history"]] or [current["activeVersion"]]) + 1
                db.execute("UPDATE persona_versions SET status='archived', updated_at=? WHERE user_id=? AND status IN ('published','draft')", (now, user_id))
                db.execute(
                    """INSERT INTO persona_versions
                       (user_id, version, status, card_json, created_at, updated_at)
                       VALUES (?, ?, 'published', ?, ?, ?)""",
                    (user_id, version, row["card_json"], now, now),
                )
            return self._persona_card_locked(db, user_id)

    def preview_persona(self, payload: dict) -> dict:
        user_id = str(payload.get("userId") or "").strip()
        text = str(payload.get("text") or "").strip()
        use_draft = bool(payload.get("useDraft", True))
        if not user_id or not text or len(text) > 1000:
            raise ProtocolError("userId and preview text required")
        with self._connect() as db:
            persona = self._persona_card_locked(db, user_id)
            card = persona["draft"] if use_draft and persona["draft"] else persona["active"]
            rows = db.execute(
                "SELECT server_seq, payload_json FROM memory_events WHERE user_id=? ORDER BY server_seq DESC LIMIT 100",
                (user_id,),
            ).fetchall()
            memories = [memory for memory in self._decorate_memory_rows(db, user_id, list(reversed(rows))) if memory_is_speakable(memory)]
            facts, boundaries = self._profile_memories_locked(db, user_id)
            open_threads = self._open_threads_locked(db, user_id)
            standing = self._standing_knowledge_locked(db, user_id)
            living = self._living_companion_inputs_locked(db, user_id)
        conversation = [{"role": "user", "text": text}]
        mind = build_companion_mind(text, conversation)
        profile = profile_context(facts, boundaries)
        profile.update({"companionMind": mind, "personaCard": card})
        profile["standingKnowledge"] = standing_knowledge_context(standing)
        profile["companionFrame"] = build_companion_frame(
            user_text=text, conversation=conversation, memories=memories,
            user_facts=facts, boundaries=boundaries, persona=card,
            threads=open_threads,
            affinity_state=living["affinity"], daily_state=living["daily"],
            chronotype=living["chronotype"], timeline_events=living["timeline"], story_provider=living["story"],
            expression_rules=living["expressions"],
        )
        reply = contextual_fallback(text, conversation, mind)
        source = "fallback"
        if self.responder is not None and self.responder.available and not profile["companionFrame"].generation_blocked:
            try:
                candidate = str(self.responder.reply(memories, conversation, profile) or "").strip()
                if candidate and not reply_violates_boundaries(candidate, profile) and not reply_has_style_violation(candidate, text):
                    reply, source = candidate, "model"
            except Exception:
                pass
        return {
            "text": reply,
            "source": source,
            "using": "draft" if use_draft and persona["draft"] else "published",
            "version": persona["draftVersion"] if use_draft and persona["draft"] else persona["activeVersion"],
            "compiledPrompt": compile_persona_card(card),
            "mind": mind,
            "persisted": False,
        }

    def update_profile_memory(self, payload: dict) -> dict:
        user_id = str(payload.get("userId") or "").strip()
        memory_type = str(payload.get("memoryType") or "").strip()
        memory_id = str(payload.get("id") or "").strip()
        action = str(payload.get("action") or "").strip()
        if not user_id or memory_type not in {"userFact", "boundary"} or not memory_id:
            raise ProtocolError("invalid profile memory action")
        allowed = {"confirm", "correct", "pause", "resume", "delete"}
        if action not in allowed:
            raise ProtocolError("invalid profile memory action")
        now = self._now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if memory_type == "userFact":
                row = db.execute(
                    "SELECT predicate, value_text FROM user_facts WHERE user_id=? AND fact_id=?",
                    (user_id, memory_id),
                ).fetchone()
                if row is None:
                    raise ProtocolError("profile memory not found")
                if action == "correct":
                    value = str(payload.get("value") or "").strip()
                    if not value or len(value) > 200:
                        raise ProtocolError("corrected value required")
                    db.execute("UPDATE user_facts SET status='corrected', updated_at=? WHERE user_id=? AND fact_id=?", (now, user_id, memory_id))
                    new_id = stable_id("fact", user_id, row["predicate"], value)
                    db.execute(
                        """INSERT INTO user_facts
                           (user_id, fact_id, predicate, value_text, status, confidence, created_at, updated_at)
                           VALUES (?, ?, ?, ?, 'confirmed', 1.0, ?, ?)
                           ON CONFLICT(user_id, fact_id) DO UPDATE SET status='confirmed', updated_at=excluded.updated_at""",
                        (user_id, new_id, row["predicate"], value, now, now),
                    )
                    memory_id = new_id
                else:
                    status = {"confirm": "confirmed", "pause": "paused", "resume": "confirmed", "delete": "forgotten"}[action]
                    db.execute("UPDATE user_facts SET status=?, updated_at=? WHERE user_id=? AND fact_id=?", (status, now, user_id, memory_id))
            else:
                status = {"confirm": "active", "correct": "active", "pause": "paused", "resume": "active", "delete": "forgotten"}[action]
                cursor = db.execute(
                    "UPDATE companion_boundaries SET status=?, updated_at=? WHERE user_id=? AND boundary_id=?",
                    (status, now, user_id, memory_id),
                )
                if not cursor.rowcount:
                    raise ProtocolError("profile memory not found")
            facts, boundaries = self._profile_memories_locked(db, user_id)
        return {"updated": True, "id": memory_id, "userFacts": facts, "boundaries": boundaries}

    def append_message(self, payload: dict) -> dict:
        user_id = str(payload.get("userId") or "").strip()
        device_id = str(payload.get("deviceId") or "").strip()
        message_id = str(payload.get("messageId") or "").strip()
        role = str(payload.get("role") or "").strip()
        text = str(payload.get("text") or "").strip()
        if not user_id or not device_id or not message_id or role not in {"user", "assistant"}:
            raise ProtocolError("invalid conversation message")
        if not text or len(text) > 4000:
            raise ProtocolError("invalid conversation text")
        now = self._now()
        emotion_label = explicit_emotion_label(text) if role == "user" else ""
        emotion_id = (
            "emotion_" + payload_hash({
                "userId": user_id,
                "messageId": message_id,
                "label": emotion_label,
            })[:24]
            if emotion_label else ""
        )
        reply_id = (
            "reply_" + payload_hash({"userId": user_id, "messageId": message_id})[:24]
            if role == "user" else ""
        )
        duplicate = False
        emotion_recorded = False
        lifecycle_updates: list[dict] = []
        focus_memory_id = ""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if role == "user":
                focus_memory_id = self._latest_focus_memory_id(db, user_id)
            existing = db.execute(
                """SELECT role, text FROM conversation_messages
                   WHERE user_id=? AND message_id=?""",
                (user_id, message_id),
            ).fetchone()
            if existing is not None:
                if existing["role"] != role or existing["text"] != text:
                    raise MemoryConflictError("messageId already exists with different payload")
                duplicate = True
                emotion_recorded = bool(emotion_id and db.execute(
                    "SELECT 1 FROM memory_events WHERE user_id=? AND memory_id=?",
                    (user_id, emotion_id),
                ).fetchone())
                lifecycle_updates = [{
                    "memoryId": row["memory_id"],
                    "status": row["status"],
                    "reason": row["reason"],
                    "sourceType": row["source_type"],
                    "sourceMessageId": row["source_message_id"],
                    "updatedAt": row["created_at"],
                } for row in db.execute(
                    """SELECT memory_id, status, reason, source_type,
                              source_message_id, created_at
                       FROM memory_lifecycle_events
                       WHERE user_id=? AND source_message_id=?
                       ORDER BY lifecycle_seq ASC""",
                    (user_id, message_id),
                ).fetchall()]
            else:
                cursor = db.execute(
                    """INSERT INTO conversation_messages
                       (user_id, device_id, message_id, role, text, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (user_id, device_id, message_id, role, text, now),
                )
                if role == "user":
                    self._extract_profile_memories_locked(
                        db, user_id=user_id, message_id=message_id, text=text, now=now,
                    )
                    self._observe_interaction_locked(
                        db, user_id=user_id, text=text, message_id=message_id, now=now,
                    )
                if emotion_label:
                    memory = {
                        "id": emotion_id,
                        "revision": int(cursor.lastrowid),
                        "layer": "L3",
                        "sourceType": "user_stated",
                        "kind": "stated_emotion",
                        "label": emotion_label,
                        "quote": text,
                        "sourceMessageId": message_id,
                        "occurredAt": now,
                    }
                    encoded = canonical_json(memory)
                    db.execute(
                        """INSERT INTO memory_events
                           (user_id, device_id, memory_id, client_revision, layer, kind,
                            memory_time, payload_json, payload_hash, received_at)
                           VALUES (?, ?, ?, ?, 'L3', 'stated_emotion', ?, ?, ?, ?)""",
                        (
                            user_id, device_id, emotion_id, memory["revision"], now,
                            encoded, payload_hash(memory), now,
                        ),
                    )
                    self._open_emotional_thread_locked(
                        db, user_id=user_id, memory=memory, now=now,
                    )
                    emotion_recorded = True
                transition, reason = lifecycle_transition_from_text(text) if role == "user" else ("", "")
                if transition and focus_memory_id:
                    lifecycle_updates.append(self._set_lifecycle_locked(
                        db,
                        user_id=user_id,
                        memory_id=focus_memory_id,
                        status=transition,
                        reason=reason,
                        source_type="user_explicit",
                        source_message_id=message_id,
                        now=now,
                    ))
                    self._update_emotional_thread_status_locked(
                        db, user_id=user_id, memory_id=focus_memory_id,
                        status=transition, now=now,
                    )
                    if transition == "active":
                        # 用户亲口说"它还没过去"就是一次再次提及：记一笔强化，
                        # 这条记忆此后衰减得更慢，而不只是把状态改回 active。
                        self._record_memory_mention_locked(
                            db, user_id=user_id, memory_id=focus_memory_id,
                            message_id=message_id, mention_type="user_recall", now=now,
                            touch_thread=False,
                        )

        assistant_message = None
        reply_source = ""
        if role == "user":
            reply_focus_memory_id = emotion_id if emotion_recorded else focus_memory_id
            if lifecycle_updates and lifecycle_updates[-1]["status"] in {"resolved", "dormant", "suppressed", "corrected"}:
                reply_focus_memory_id = ""
            assistant_message, reply_source = self._ensure_assistant_reply(
                user_id=user_id,
                device_id=device_id,
                reply_id=reply_id,
                user_text=text,
                emotion_label=emotion_label,
                focus_memory_id=reply_focus_memory_id,
            )
        return {
            "accepted": True,
            "duplicate": duplicate,
            "messageId": message_id,
            "emotionRecorded": emotion_recorded,
            "emotionMemoryId": emotion_id if emotion_recorded else "",
            "lifecycleUpdates": lifecycle_updates,
            "assistantMessage": assistant_message,
            "replySource": reply_source,
        }

    def review_insight(self, payload: dict) -> dict:
        user_id = str(payload.get("userId") or "").strip()
        insight_id = str(payload.get("insightId") or "").strip()
        action = str(payload.get("action") or "").strip()
        if not user_id or not insight_id or action not in {"confirm", "reject"}:
            raise ProtocolError("invalid insight review")
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload_json FROM memory_events WHERE user_id=? ORDER BY server_seq ASC",
                (user_id,),
            ).fetchall()
            memories = [json.loads(row["payload_json"]) for row in rows]
            draft = next((row for row in build_insight_drafts(memories) if row["id"] == insight_id), None)
            if draft is None:
                raise ProtocolError("insight draft not found")
            status = "confirmed" if action == "confirm" else "rejected"
            reviewed = {**draft, "status": status, "mayEnterModelContext": status == "confirmed"}
            now = self._now()
            db.execute(
                """INSERT INTO insight_reviews
                   (user_id, insight_id, status, insight_json, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, insight_id) DO UPDATE SET
                     status=excluded.status, insight_json=excluded.insight_json,
                     updated_at=excluded.updated_at""",
                (user_id, insight_id, status, canonical_json(reviewed), now),
            )
        return reviewed

    def _ensure_assistant_reply(
        self,
        *,
        user_id: str,
        device_id: str,
        reply_id: str,
        user_text: str,
        emotion_label: str,
        focus_memory_id: str,
    ) -> tuple[dict, str]:
        with self._reply_lock:
            with self._connect() as db:
                existing = db.execute(
                    """SELECT message_id, role, text, created_at FROM conversation_messages
                       WHERE user_id=? AND message_id=?""",
                    (user_id, reply_id),
                ).fetchone()
                if existing is not None:
                    return self._message_payload(existing), "stored"
                memory_rows = db.execute(
                    """SELECT server_seq, payload_json FROM memory_events
                       WHERE user_id=? ORDER BY server_seq DESC LIMIT 200""",
                    (user_id,),
                ).fetchall()
                message_rows = db.execute(
                    """SELECT role, text, message_id, created_at FROM conversation_messages
                       WHERE user_id=? ORDER BY message_seq DESC LIMIT 24""",
                    (user_id,),
                ).fetchall()

                memories = self._decorate_memory_rows(db, user_id, list(reversed(memory_rows)))
                user_facts, boundaries = self._profile_memories_locked(db, user_id)
                open_threads = self._open_threads_locked(db, user_id)
                standing = self._standing_knowledge_locked(db, user_id)
                persona_state = self._persona_card_locked(db, user_id)
                companion_emotion = self._companion_emotion_locked(db, user_id)
                living = self._living_companion_inputs_locked(db, user_id)
                confirmed_rows = db.execute(
                    """SELECT insight_json FROM insight_reviews
                       WHERE user_id=? AND status='confirmed' ORDER BY updated_at DESC LIMIT 20""",
                    (user_id,),
                ).fetchall()
            memories = [memory for memory in memories if memory_is_speakable(memory)]
            memories.extend({
                "id": insight["id"], "revision": 1, "layer": "L2", "sourceType": "derived",
                "kind": "confirmed_insight", "statement": insight["body"],
                "evidenceMemoryIds": insight.get("evidenceMemoryIds", []),
                "confidence": insight.get("confidence", 0.5),
            } for insight in (json.loads(row["insight_json"]) for row in confirmed_rows))
            memories, retrieval_trace = select_companion_memories(memories, user_text)
            conversation = [
                {"role": row["role"], "text": row["text"], "messageId": row["message_id"], "createdAt": row["created_at"]}
                for row in reversed(message_rows)
            ]
            reply_text = build_grounded_companion_reply(
                user_text,
                memories,
                emotion_label=emotion_label,
                conversation=conversation,
            )
            mind = build_companion_mind(user_text, conversation)
            profile = profile_context(user_facts, boundaries)
            profile["companionMind"] = mind
            profile["personaCard"] = persona_state["active"]
            profile["standingKnowledge"] = standing_knowledge_context(standing)
            profile["companionFrame"] = build_companion_frame(
                user_text=user_text, conversation=conversation, memories=memories,
                user_facts=user_facts, boundaries=boundaries, persona=persona_state["active"],
                threads=open_threads, companion_emotion=companion_emotion,
                affinity_state=living["affinity"], daily_state=living["daily"],
                chronotype=living["chronotype"], timeline_events=living["timeline"], story_provider=living["story"],
                expression_rules=living["expressions"],
            )
            fallback_kind = "grounded_memory" if is_direct_memory_question(user_text) else "contextual"
            transition, _transition_reason = lifecycle_transition_from_text(user_text)
            if is_reaction_to_companion(user_text):
                reply_text = contextual_fallback(user_text, conversation, mind)
            elif (
                not is_direct_memory_question(user_text)
                and not is_prompt_to_speak(user_text)
                and not transition
                and mind["intent"] not in {"emotional_bid", "self_disclosure", "boundary"}
            ):
                reply_text = contextual_fallback(user_text, conversation, mind)
            reply_source = "fallback"
            responder = self.responder
            trace = {
                "replyMessageId": reply_id,
                "userText": user_text,
                "intent": profile["companionFrame"].get("sharedScene", {}).get("intent", ""),
                "route": "model_first" if responder is not None and responder.available else "fallback_only",
                "fallbackKind": fallback_kind,
                "selectedMemoryIds": [str(row.get("id") or "") for row in memories if row.get("id")],
                "memoryRetrieval": retrieval_trace,
                "focusMemoryId": focus_memory_id,
                "companionFrame": profile["companionFrame"],
                "model": str(responder.name) if responder is not None and responder.available else "",
                "modelAttempted": False,
                "modelAccepted": False,
                "rejectionReason": "model_unavailable" if responder is None or not responder.available else "",
            }
            if profile["companionFrame"].generation_blocked:
                trace["route"] = "fallback_only"
                trace["rejectionReason"] = "context_budget_exceeded"
            if responder is not None and responder.available and not profile["companionFrame"].generation_blocked:
                trace["modelAttempted"] = True
                try:
                    candidate = str(responder.reply(
                        memories, [{"role": r["role"], "text": r["text"]} for r in conversation], profile,
                    ) or "").strip()
                    if not candidate:
                        trace["rejectionReason"] = "empty_response"
                    elif reply_violates_boundaries(candidate, profile):
                        trace["rejectionReason"] = "boundary_violation"
                    elif reply_has_style_violation(candidate, user_text):
                        trace["rejectionReason"] = "style_violation"
                    else:
                        reply_text = candidate[:4000]
                        reply_source = "model"
                        trace["modelAccepted"] = True
                        trace["rejectionReason"] = ""
                except Exception as exc:
                    trace["rejectionReason"] = type(exc).__name__
                    reply_source = "fallback"
            trace["finalSource"] = reply_source

            reply_time = self._now()
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    """INSERT OR IGNORE INTO conversation_messages
                       (user_id, device_id, message_id, role, text, created_at)
                       VALUES (?, ?, ?, 'assistant', ?, ?)""",
                    (user_id, device_id, reply_id, reply_text, reply_time),
                )
                stored = db.execute(
                    """SELECT message_id, role, text, created_at FROM conversation_messages
                       WHERE user_id=? AND message_id=?""",
                    (user_id, reply_id),
                ).fetchone()
                db.execute(
                    """INSERT OR REPLACE INTO reply_traces
                       (user_id, reply_message_id, trace_json, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (user_id, reply_id, canonical_json(trace), reply_time),
                )
                if memory_rows:
                    through = int(memory_rows[0]["server_seq"])
                    db.execute(
                        """INSERT INTO companion_cursors
                           (user_id, device_id, last_memory_seq, updated_at)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT(user_id, device_id) DO UPDATE SET
                             last_memory_seq=MAX(last_memory_seq, excluded.last_memory_seq),
                             updated_at=excluded.updated_at""",
                        (user_id, device_id, through, reply_time),
                    )
                if focus_memory_id:
                    db.execute(
                        """INSERT OR IGNORE INTO conversation_memory_refs
                           (user_id, message_id, memory_id, created_at)
                           VALUES (?, ?, ?, ?)""",
                        (user_id, reply_id, focus_memory_id, reply_time),
                    )
                    self._record_memory_mention_locked(
                        db, user_id=user_id, memory_id=focus_memory_id,
                        message_id=reply_id, mention_type="reply", now=reply_time,
                    )
            with self._status_lock:
                self._last_reply_source = reply_source
                self._last_reply_at = reply_time
            return self._message_payload(stored), reply_source

    @staticmethod
    def _companion_emotion_locked(db: sqlite3.Connection, user_id: str) -> dict:
        row = db.execute(
            "SELECT state_json FROM companion_emotion_state WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if row is None:
            return empty_companion_emotion()
        try:
            return json.loads(row["state_json"])
        except (ValueError, TypeError):
            return empty_companion_emotion()

    @staticmethod
    def _persist_companion_emotion_locked(db: sqlite3.Connection, user_id: str, state: dict, now: str) -> None:
        db.execute(
            """INSERT INTO companion_emotion_state (user_id, state_json, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 state_json=excluded.state_json, updated_at=excluded.updated_at""",
            (user_id, canonical_json(state), now),
        )

    def companion_emotion(self, user_id: str) -> dict:
        if not str(user_id).strip():
            raise ProtocolError("userId required")
        with self._connect() as db:
            return self._companion_emotion_locked(db, user_id)

    # ---- 好感度状态机 ----

    @staticmethod
    def _affinity_state_locked(db: sqlite3.Connection, user_id: str) -> dict:
        row = db.execute(
            """SELECT score, stage, interaction, familiarity, trust, warmth,
                      boundary_state, updated_at
               FROM relationship_state WHERE user_id=?""",
            (user_id,),
        ).fetchone()
        if row is None:
            return empty_affinity_state()
        return normalize_affinity_state({
            "score": float(row["score"]),
            "stage": str(row["stage"]),
            "interaction": str(row["interaction"]),
            "familiarity": float(row["familiarity"]),
            "trust": float(row["trust"]),
            "warmth": float(row["warmth"]),
            "boundaryState": str(row["boundary_state"]),
            "updatedAt": str(row["updated_at"]),
        })

    @staticmethod
    def _persist_affinity_locked(
        db: sqlite3.Connection, user_id: str, state: dict, ledger: list[dict],
    ) -> None:
        db.execute(
            """INSERT INTO relationship_state
               (user_id, score, stage, interaction, familiarity, trust, warmth,
                boundary_state, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 score=excluded.score, stage=excluded.stage, interaction=excluded.interaction,
                 familiarity=excluded.familiarity, trust=excluded.trust, warmth=excluded.warmth,
                 boundary_state=excluded.boundary_state, updated_at=excluded.updated_at""",
            (user_id, state["score"], state["stage"], state["interaction"],
             state["familiarity"], state["trust"], state["warmth"],
             state["boundaryState"], state["updatedAt"]),
        )
        db.execute("DELETE FROM relationship_ledger WHERE user_id=?", (user_id,))
        for entry in ledger:
            db.execute(
                """INSERT INTO relationship_ledger
                   (user_id, event_key, kind, delta, solicited, at) VALUES (?,?,?,?,?,?)""",
                (user_id, entry["eventKey"], entry["kind"], entry["delta"],
                 1 if entry.get("solicited") else 0, entry["at"]),
            )

    @staticmethod
    def _affinity_ledger_locked(db: sqlite3.Connection, user_id: str) -> list[dict]:
        rows = db.execute(
            """SELECT event_key, kind, delta, solicited, at FROM relationship_ledger
               WHERE user_id=? ORDER BY at DESC LIMIT 200""",
            (user_id,),
        ).fetchall()
        return [
            {"eventKey": r["event_key"], "kind": r["kind"], "delta": float(r["delta"]),
             "solicited": bool(r["solicited"]), "at": r["at"]}
            for r in reversed(rows)
        ]

    def affinity_state(self, user_id: str) -> dict:
        if not str(user_id).strip():
            raise ProtocolError("userId required")
        with self._connect() as db:
            return self._affinity_state_locked(db, user_id)

    def relationship_view(self, user_id: str) -> dict:
        """控制台视图：内部七维 + 对外权限，两者分开陈列，永不混为一谈。"""
        if not str(user_id).strip():
            raise ProtocolError("userId required")
        with self._connect() as db:
            state = self._affinity_state_locked(db, user_id)
            _facts, boundaries = self._profile_memories_locked(db, user_id)
            config = self._living_config_locked(db, user_id)
            ledger = self._affinity_ledger_locked(db, user_id)
        return {
            "internal": state,
            "permissions": relationship_permissions(
                state, boundaries=boundaries,
                user_quota=float(config["proactive"]["dailyLimit"])),
            "boundaryState": derive_boundary_state(boundaries),
            "evidence": ledger[-20:],
        }

    def apply_affinity_event(self, payload: dict) -> dict:
        user_id = str(payload.get("userId") or "").strip()
        if not user_id:
            raise ProtocolError("userId required")
        event = payload.get("event")
        if not isinstance(event, dict):
            raise ProtocolError("event required")
        with self._connect() as db:
            state = self._affinity_state_locked(db, user_id)
            ledger = self._affinity_ledger_locked(db, user_id)
            new_state, new_ledger = apply_affinity_event_fn(state, ledger, event)
            self._persist_affinity_locked(db, user_id, new_state, new_ledger)
            return {"state": new_state}

    @staticmethod
    def _followed_a_proactive_locked(db: sqlite3.Connection, user_id: str, now: str) -> bool:
        """这条用户消息是不是刚被一条主动消息"钓"出来的。

        判断依据是持久化的主动发送记录，不是助手消息——普通回复本来就是用户先开口的。
        """
        row = db.execute(
            """SELECT sent_at FROM proactive_candidates
               WHERE user_id=? AND status='sent' AND sent_at!=''
               ORDER BY sent_at DESC LIMIT 1""",
            (user_id,),
        ).fetchone()
        spoke_at = _parse_iso(row["sent_at"]) if row else None
        moment = _parse_iso(now)
        if spoke_at is None or moment is None:
            return False
        return 0 <= (moment - spoke_at).total_seconds() <= SOLICITED_REPLY_WINDOW_MINUTES * 60

    def _observe_interaction_locked(self, db: sqlite3.Connection, user_id: str, text: str, message_id: str, now: str) -> None:
        """每次用户消息落地后，把可观察到的信号喂进三个新子系统。

        1. 好感度事件由确定性规则提取，交给原有账本执行。
        2. 表达学习由原有短句规则生成候选，默认 pending 等审核。
        3. 自我时间线：追加一条 confirmed 的 interaction 事件。
        全部在同一事务内，幂等（重复消息在 append_message 已被去重跳过）。
        """
        next_emotion = update_companion_emotion(
            self._companion_emotion_locked(db, user_id), text, explicit_emotion_label(text),
            now=datetime.fromisoformat(now),
        )
        self._persist_companion_emotion_locked(db, user_id, next_emotion, now)
        # 1. 好感度。边界态先算：用户这一轮划下的线，这一轮就要生效，
        #    不等它慢慢反映到分数上——收敛必须是立即的。
        state = self._affinity_state_locked(db, user_id)
        _facts, stored_boundaries = self._profile_memories_locked(db, user_id)
        boundary_state = derive_boundary_state(stored_boundaries)
        event = derive_affinity_event(text, event_key=f"interaction:{message_id}")
        if event is not None:
            # 这句话是被主动消息钓出来的吗？是的话它涨分的额度另算，
            # 免得多打扰几次就能把关系刷上去。
            event = {**event, "at": now, "solicited": self._followed_a_proactive_locked(db, user_id, now)}
            ledger = self._affinity_ledger_locked(db, user_id)
            new_state, new_ledger = apply_affinity_event_fn(state, ledger, event)
            self._persist_affinity_locked(
                db, user_id, {**new_state, "boundaryState": boundary_state}, new_ledger)
        elif boundary_state != state["boundaryState"]:
            self._persist_affinity_locked(
                db, user_id, {**state, "boundaryState": boundary_state, "updatedAt": now},
                self._affinity_ledger_locked(db, user_id))
        # 2. 表达学习候选（默认 pending，等审核）
        candidates = learn_candidates(text)
        for candidate in candidates:
            rule_id = "expr_" + payload_hash({"pattern": candidate["pattern"], "user": user_id})[:16]
            db.execute(
                """INSERT OR IGNORE INTO expression_rules
                   (user_id, rule_id, pattern, instruction, scene, kind, status, usage_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)""",
                (user_id, rule_id, candidate["pattern"], candidate["instruction"], candidate["scene"], candidate["kind"], now),
            )
        # 3. 自我时间线 interaction 事件
        db.execute(
            """INSERT OR IGNORE INTO self_timeline_events
               (user_id, event_id, ts, type, when_text, summary, detail, status, keywords)
               VALUES (?, ?, ?, 'interaction', ?, ?, ?, 'confirmed', '')""",
            (user_id, f"interaction:{message_id}", now, now[:16], "和你聊了会天", text[:220]),
        )

    # ---- 生活连续性 ----

    @staticmethod
    def _chronotype_locked(db: sqlite3.Connection, user_id: str) -> dict:
        row = db.execute(
            "SELECT profile_json FROM chronotype_profile WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            return empty_chronotype()
        try:
            return json.loads(row["profile_json"])
        except (ValueError, TypeError):
            return empty_chronotype()

    @staticmethod
    def _daily_state_locked(db: sqlite3.Connection, user_id: str, now: datetime) -> dict:
        chronotype = MemoryRepository._chronotype_locked(db, user_id)
        date = now.strftime("%Y-%m-%d")
        row = db.execute(
            "SELECT conditions_json FROM daily_state WHERE user_id=? AND state_date=?",
            (user_id, date),
        ).fetchone()
        if row is None:
            conditions = generate_daily_conditions(now, chronotype)
        else:
            try:
                conditions = json.loads(row["conditions_json"])
            except (ValueError, TypeError):
                conditions = generate_daily_conditions(now, chronotype)
        hunger = meal_hunger_condition(now, chronotype)
        if hunger is not None:
            conditions = conditions + [hunger]
        # 临时生活事件按日期种子确定性重算，只有此刻还在窗口里的才计入状态；
        # 它们不写进 conditions_json，因为窗口一过它们就该消失，而不是留在当日流水里。
        live = active_transient_events(transient_life_events(now, chronotype), now)
        state = compose_daily_state(conditions + transient_conditions(live), now, chronotype)
        db.execute(
            """INSERT INTO daily_state (user_id, state_date, conditions_json, state_json, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(user_id, state_date) DO UPDATE SET
                 conditions_json=excluded.conditions_json,
                 state_json=excluded.state_json, updated_at=excluded.updated_at""",
            (user_id, date, canonical_json(conditions), canonical_json(state), now.isoformat()),
        )
        return state

    def daily_state(self, user_id: str) -> dict:
        if not str(user_id).strip():
            raise ProtocolError("userId required")
        now = self._clock()
        with self._connect() as db:
            return self._daily_state_locked(db, user_id, now)

    def _living_companion_inputs_locked(self, db: sqlite3.Connection, user_id: str,
                                        now: datetime | None = None) -> dict:
        """一次连接内取齐「活着的陪伴」四个新模块的输入。"""
        now = now or self._clock()
        timeline_rows = db.execute(
            """SELECT event_id, ts, type, when_text, summary, detail, status, keywords
               FROM self_timeline_events WHERE user_id=? ORDER BY ts DESC LIMIT 50""",
            (user_id,),
        ).fetchall()
        expression_rows = db.execute(
            "SELECT rule_id, pattern, instruction, scene, kind, status, usage_count FROM expression_rules WHERE user_id=? ORDER BY usage_count DESC",
            (user_id,),
        ).fetchall()
        return {
            "affinity": self._affinity_state_locked(db, user_id),
            "story": ArcStoryProvider(self._story_arcs_locked(db, user_id)),
            "daily": self._daily_state_locked(db, user_id, now),
            "chronotype": self._chronotype_locked(db, user_id),
            "timeline": [
                {
                    "id": r["event_id"], "ts": r["ts"], "type": r["type"], "when": r["when_text"],
                    "summary": r["summary"], "detail": r["detail"], "status": r["status"], "keywords": r["keywords"],
                }
                for r in timeline_rows
            ],
            "expressions": [
                {
                    "id": r["rule_id"], "pattern": r["pattern"], "instruction": r["instruction"],
                    "scene": r["scene"], "kind": r["kind"], "status": r["status"], "usageCount": r["usage_count"],
                }
                for r in expression_rows
            ],
        }

    # ---- 自我时间线 ----

    def self_timeline_events(self, user_id: str) -> list[dict]:
        if not str(user_id).strip():
            raise ProtocolError("userId required")
        with self._connect() as db:
            rows = db.execute(
                """SELECT event_id, ts, type, when_text, summary, detail, status, keywords
                   FROM self_timeline_events WHERE user_id=? ORDER BY ts DESC LIMIT 50""",
                (user_id,),
            ).fetchall()
        return [
            {
                "id": r["event_id"], "ts": r["ts"], "type": r["type"], "when": r["when_text"],
                "summary": r["summary"], "detail": r["detail"], "status": r["status"], "keywords": r["keywords"],
            }
            for r in rows
        ]

    def append_self_timeline_event(self, payload: dict) -> dict:
        user_id = str(payload.get("userId") or "").strip()
        event = payload.get("event")
        if not user_id or not isinstance(event, dict):
            raise ProtocolError("userId and event required")
        event_id = str(event.get("id") or "").strip()
        if not event_id:
            raise ProtocolError("event.id required")
        now = datetime.now(timezone.utc)
        with self._connect() as db:
            db.execute(
                """INSERT INTO self_timeline_events
                   (user_id, event_id, ts, type, when_text, summary, detail, status, keywords)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(user_id, event_id) DO UPDATE SET
                     ts=excluded.ts, type=excluded.type, when_text=excluded.when_text,
                     summary=excluded.summary, detail=excluded.detail,
                     status=excluded.status, keywords=excluded.keywords""",
                (
                    user_id, event_id, str(event.get("ts") or now.isoformat()),
                    str(event.get("type") or "interaction"), str(event.get("when") or ""),
                    str(event.get("summary") or ""), str(event.get("detail") or ""),
                    str(event.get("status") or "confirmed"), str(event.get("keywords") or ""),
                ),
            )
            return {"ok": True, "eventId": event_id}

    # ---- 表达学习 ----

    def expression_rules(self, user_id: str) -> list[dict]:
        if not str(user_id).strip():
            raise ProtocolError("userId required")
        with self._connect() as db:
            rows = db.execute(
                "SELECT rule_id, pattern, instruction, scene, kind, status, usage_count FROM expression_rules WHERE user_id=? ORDER BY usage_count DESC",
                (user_id,),
            ).fetchall()
        return [
            {
                "id": r["rule_id"], "pattern": r["pattern"], "instruction": r["instruction"],
                "scene": r["scene"], "kind": r["kind"], "status": r["status"], "usageCount": r["usage_count"],
            }
            for r in rows
        ]

    def review_expression_rule(self, payload: dict) -> dict:
        user_id = str(payload.get("userId") or "").strip()
        rule_id = str(payload.get("ruleId") or "").strip()
        decision = str(payload.get("decision") or "").strip()
        if not user_id or not rule_id or decision not in {"approved", "rejected"}:
            raise ProtocolError("userId, ruleId and decision(approved|rejected) required")
        with self._connect() as db:
            cur = db.execute(
                "UPDATE expression_rules SET status=? WHERE user_id=? AND rule_id=?",
                (decision, user_id, rule_id),
            )
            if cur.rowcount == 0:
                raise ProtocolError("rule not found")
            return {"ok": True, "ruleId": rule_id, "status": decision}

    # ---- 自主控制：配置 / 好感度调整 / 时型 / 删除 ----

    @staticmethod
    def _merge_config(default: dict, override: dict) -> dict:
        merged = dict(default)
        for key, value in (override or {}).items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = MemoryRepository._merge_config(merged[key], value)
            else:
                merged[key] = value
        return merged

    @staticmethod
    def _living_config_locked(db: sqlite3.Connection, user_id: str) -> dict:
        row = db.execute("SELECT config_json FROM living_config WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            return dict(DEFAULT_LIVING_CONFIG)
        try:
            return MemoryRepository._merge_config(DEFAULT_LIVING_CONFIG, json.loads(row["config_json"]))
        except (ValueError, TypeError):
            return dict(DEFAULT_LIVING_CONFIG)

    def living_config(self, user_id: str) -> dict:
        if not str(user_id).strip():
            raise ProtocolError("userId required")
        with self._connect() as db:
            return self._living_config_locked(db, user_id)

    def update_living_config(self, payload: dict) -> dict:
        user_id = str(payload.get("userId") or "").strip()
        if not user_id:
            raise ProtocolError("userId required")
        config = payload.get("config")
        if not isinstance(config, dict):
            raise ProtocolError("config required")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            current = self._living_config_locked(db, user_id)
            merged = self._merge_config(current, config)
            db.execute(
                """INSERT INTO living_config (user_id, config_json, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     config_json=excluded.config_json, updated_at=excluded.updated_at""",
                (user_id, canonical_json(merged), now),
            )
            return merged

    def adjust_affinity(self, payload: dict) -> dict:
        user_id = str(payload.get("userId") or "").strip()
        if not user_id:
            raise ProtocolError("userId required")
        now = self._clock()
        with self._connect() as db:
            if payload.get("reset"):
                db.execute("DELETE FROM relationship_state WHERE user_id=?", (user_id,))
                db.execute("DELETE FROM relationship_ledger WHERE user_id=?", (user_id,))
                return {"state": empty_affinity_state()}
            # 手动直接设分（用户显式控制，不经过自动事件的上限/迟滞）。
            if "score" in payload:
                score = max(-1200.0, min(1200.0, float(payload["score"])))
                stage = stage_key_of(score)
                previous = self._affinity_state_locked(db, user_id)
                # 手动设分只动分数和阶段：熟悉、信任和用户划下的边界不是滑块能拨的。
                state = normalize_affinity_state({
                    **previous,
                    "score": score,
                    "stage": stage,
                    "interaction": interaction_state_of(stage, warmth=previous["warmth"]),
                    "updatedAt": now.isoformat(),
                })
                self._persist_affinity_locked(db, user_id, state, [])
                return {"state": state}
            delta = float(payload.get("delta") or 0.0)
            if delta == 0.0:
                raise ProtocolError("score, delta or reset required")
            state = self._affinity_state_locked(db, user_id)
            ledger = self._affinity_ledger_locked(db, user_id)
            new_state, new_ledger = apply_affinity_event_fn(
                state, ledger, {"eventKey": f"manual:{now.timestamp()}", "kind": "manual", "delta": delta, "at": now.isoformat()},
            )
            self._persist_affinity_locked(db, user_id, new_state, new_ledger)
            return {"state": new_state}

    def set_chronotype(self, payload: dict) -> dict:
        user_id = str(payload.get("userId") or "").strip()
        if not user_id:
            raise ProtocolError("userId required")
        wake = int(payload.get("wakeMinute"))
        sleep = int(payload.get("sleepMinute"))
        if not (0 <= wake < 1440) or not (0 <= sleep < 1440):
            raise ProtocolError("wakeMinute/sleepMinute must be within 0..1439")
        now = datetime.now(timezone.utc)
        with self._connect() as db:
            current = self._chronotype_locked(db, user_id)
            current.update({
                "wakeMinute": wake, "sleepMinute": sleep,
                "source": "explicit", "confidence": 0.9, "updatedAt": now.isoformat(),
            })
            db.execute(
                """INSERT INTO chronotype_profile (user_id, profile_json, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     profile_json=excluded.profile_json, updated_at=excluded.updated_at""",
                (user_id, canonical_json(current), now.isoformat()),
            )
            return current

    def delete_expression_rule(self, payload: dict) -> dict:
        user_id = str(payload.get("userId") or "").strip()
        rule_id = str(payload.get("ruleId") or "").strip()
        if not user_id or not rule_id:
            raise ProtocolError("userId and ruleId required")
        with self._connect() as db:
            db.execute("DELETE FROM expression_rules WHERE user_id=? AND rule_id=?", (user_id, rule_id))
            return {"ok": True, "ruleId": rule_id}

    def delete_self_timeline_event(self, payload: dict) -> dict:
        user_id = str(payload.get("userId") or "").strip()
        event_id = str(payload.get("eventId") or "").strip()
        if not user_id or not event_id:
            raise ProtocolError("userId and eventId required")
        with self._connect() as db:
            db.execute("DELETE FROM self_timeline_events WHERE user_id=? AND event_id=?", (user_id, event_id))
            return {"ok": True, "eventId": event_id}

    @staticmethod
    def _stored_candidates_locked(db: sqlite3.Connection, user_id: str) -> list[dict]:
        return [{
            "signature": row["signature"], "kind": row["kind"], "content": row["content"],
            "topic": row["topic"], "status": row["status"], "score": float(row["score"]),
            "windowStart": row["window_start"], "bestUntil": row["best_until"],
            "expiresAt": row["expires_at"], "deliverCount": int(row["deliver_count"]),
            "createdAt": row["created_at"], "sentAt": row["sent_at"],
        } for row in db.execute(
            """SELECT signature, kind, content, topic, status, score, window_start, best_until,
                      expires_at, deliver_count, created_at, sent_at
               FROM proactive_candidates WHERE user_id=? ORDER BY created_at ASC LIMIT 200""",
            (user_id,),
        ).fetchall()]

    # ---- 故事线 ----

    @staticmethod
    def _story_arcs_locked(db: sqlite3.Connection, user_id: str) -> list[StoryArc]:
        return [StoryArc.from_dict(json.loads(row["arc_json"])) for row in db.execute(
            "SELECT arc_json FROM story_arcs WHERE user_id=? ORDER BY arc_id ASC", (user_id,),
        ).fetchall()]

    @staticmethod
    def _persist_story_arcs_locked(db: sqlite3.Connection, user_id: str, arcs: list[StoryArc]) -> None:
        for arc in arcs:
            db.execute(
                """INSERT INTO story_arcs (user_id, arc_id, arc_json, status, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, arc_id) DO UPDATE SET
                     arc_json=excluded.arc_json, status=excluded.status,
                     updated_at=excluded.updated_at""",
                (user_id, arc.id, canonical_json(arc.to_dict()), arc.status, arc.updated_at),
            )

    def _story_signals_locked(self, db: sqlite3.Connection, user_id: str, now: datetime) -> dict:
        """故事只认真实发生过的事，所以信号全部从已落库的状态里读。"""
        facts, _boundaries = self._profile_memories_locked(db, user_id)
        threads = self._open_threads_locked(db, user_id)
        affinity = self._affinity_state_locked(db, user_id)
        timeline = db.execute(
            "SELECT COUNT(*) AS n FROM self_timeline_events WHERE user_id=? AND type='diary'",
            (user_id,),
        ).fetchone()
        mentions = max([int(row.get("mentionCount") or 0) for row in threads] or [0])
        return {
            "relationshipStage": affinity["stage"],
            "knownFactCount": sum(1 for row in facts if row.get("status") == "confirmed"),
            "threadMentions": mentions,
            "threadResolved": bool(threads) and all(
                str(row.get("status") or "") in {"resolved", "suppressed"} for row in threads),
            "timelineEvents": int(timeline["n"] or 0),
        }

    def _advance_story_locked(self, db: sqlite3.Connection, user_id: str, now: datetime) -> list[StoryArc]:
        """算出故事此刻该在哪一格：起线 → 过闸门 → 走格子。全部由已经发生的事驱动。

        **只算不写。** 只读的轮询接口也要用它，所以落库交给调用方显式决定。
        """
        arcs = self._story_arcs_locked(db, user_id)
        signals = self._story_signals_locked(db, user_id, now)
        _facts, boundaries = self._profile_memories_locked(db, user_id)
        config = self._living_config_locked(db, user_id)
        affinity = self._affinity_state_locked(db, user_id)
        permissions = relationship_permissions(
            affinity, boundaries=boundaries,
            user_quota=float(config["proactive"]["dailyLimit"]))
        period = period_of(now, self._chronotype_locked(db, user_id))

        # 同时最多一条主线加一条轻支线，所以每种分量只在空缺时起新的。
        held = {arc.weight for arc in arcs if arc.status in {"active", "paused"}}
        for template in eligible_templates(arcs, signals):
            if template.get("weight", "main") in held:
                continue
            held.add(template.get("weight", "main"))
            arcs.append(start_arc(template["id"], now=now))

        arcs = apply_story_gates(arcs, permissions=permissions,
                                 boundary_state=derive_boundary_state(boundaries),
                                 period=period, now=now)
        return [advance_arc(arc, {**signals, "daysSinceStart": arc_age_days(arc, now)},
                            now=now) for arc in arcs]

    def _tick_story_locked(self, db: sqlite3.Connection, user_id: str, now: datetime) -> list[StoryArc]:
        """推进并落库。只有真的要改变状态的路径才调它。"""
        arcs = self._advance_story_locked(db, user_id, now)
        self._persist_story_arcs_locked(db, user_id, arcs)
        return arcs

    def story_state(self, user_id: str) -> dict:
        """控制台视图：完整故事状态在这里看，模型上下文里只有当前那一格。"""
        if not str(user_id).strip():
            raise ProtocolError("userId required")
        now = self._clock()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            arcs = self._tick_story_locked(db, user_id, now)
        return {
            "arcs": [arc.to_dict() for arc in arcs],
            "current": [arc.id for arc in select_current_arcs(arcs)],
            "checkedAt": now.isoformat(),
        }

    def act_on_story(self, payload: dict) -> dict:
        """用户对故事的选择：选分支，或者明说不想聊这条线。

        这是故事唯一接受外部推动的入口——她不替用户决定故事往哪边走。
        """
        user_id = str(payload.get("userId") or "").strip()
        arc_id = str(payload.get("arcId") or "").strip()
        action = str(payload.get("action") or "").strip()
        if not user_id or not arc_id or action not in {"choose", "refuse"}:
            raise ProtocolError("invalid story action")
        now = self._clock()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            arcs = self._story_arcs_locked(db, user_id)
            target = next((arc for arc in arcs if arc.id == arc_id), None)
            if target is None:
                raise ProtocolError("story arc not found")
            if action == "refuse":
                updated = refuse_arc(target, now=now)
            else:
                branch = str(payload.get("branchId") or "").strip()
                if branch not in {row["id"] for row in target.available_branches}:
                    raise ProtocolError("branch not available")
                updated = choose_branch(target, branch, now=now)
            self._persist_story_arcs_locked(db, user_id, [updated])
        return {"accepted": True, "arc": updated.to_dict()}

    @staticmethod
    def _care_sent_today_locked(db: sqlite3.Connection, user_id: str, now: datetime) -> int:
        """今天已经用掉几次"主动关心"额度。关心和分享分开计，两者不是一回事。"""
        today = now.astimezone().date().isoformat()
        rows = db.execute(
            """SELECT sent_at FROM proactive_candidates
               WHERE user_id=? AND status='sent' AND sent_at!=''
                 AND kind IN ('daily_greeting', 'meal_care', 'mood_checkin')""",
            (user_id,),
        ).fetchall()
        return sum(1 for row in rows
                   if (_parse_iso(row["sent_at"]) or now).astimezone().date().isoformat() == today)

    @staticmethod
    def _proactive_pressure_locked(db: sqlite3.Connection, user_id: str, now: datetime) -> dict:
        """今天已经主动几次、连着几次没人理、上一次开口是什么时候。

        这三个数字决定每日上限、未回应降速和最小间隔能不能真的拦住她。
        不从持久化的发送记录里读，它们就永远是 0，三道闸门等于形同虚设。
        """
        today = now.astimezone().date().isoformat()
        sent = db.execute(
            """SELECT sent_at FROM proactive_candidates
               WHERE user_id=? AND status='sent' AND sent_at!=''
               ORDER BY sent_at DESC LIMIT 50""",
            (user_id,),
        ).fetchall()
        sent_times = [str(row["sent_at"]) for row in sent]
        daily_sent = sum(1 for value in sent_times
                         if (_parse_iso(value) or now).astimezone().date().isoformat() == today)
        last_user = db.execute(
            """SELECT created_at FROM conversation_messages
               WHERE user_id=? AND role='user' ORDER BY message_seq DESC LIMIT 1""",
            (user_id,),
        ).fetchone()
        replied_at = _parse_iso(last_user["created_at"]) if last_user else None
        streak = 0
        for value in sent_times:
            spoke_at = _parse_iso(value)
            if spoke_at is None or (replied_at is not None and replied_at > spoke_at):
                break
            streak += 1
        return {
            "dailySent": daily_sent,
            "noResponseStreak": streak,
            "lastProactiveAt": sent_times[0] if sent_times else "",
        }

    def _evaluate_proactive_locked(
        self, db: sqlite3.Connection, user_id: str, activity: str, now: datetime,
    ) -> dict:
        last_spoke = db.execute(
            """SELECT created_at FROM conversation_messages
               WHERE user_id=? AND role='assistant' ORDER BY message_seq DESC LIMIT 1""",
            (user_id,),
        ).fetchone()
        last_user = db.execute(
            """SELECT created_at FROM conversation_messages
               WHERE user_id=? AND role='user' ORDER BY message_seq DESC LIMIT 1""",
            (user_id,),
        ).fetchone()
        open_threads = build_open_threads(self._open_threads_locked(db, user_id))
        living = self._living_companion_inputs_locked(db, user_id, now)
        living_config = self._living_config_locked(db, user_id)
        _facts, boundaries = self._profile_memories_locked(db, user_id)
        memory_rows = db.execute(
            "SELECT server_seq, payload_json FROM memory_events WHERE user_id=? ORDER BY server_seq DESC LIMIT 200",
            (user_id,),
        ).fetchall()
        memories = self._decorate_memory_rows(db, user_id, list(reversed(memory_rows)))
        stored = self._stored_candidates_locked(db, user_id)
        pressure = self._proactive_pressure_locked(db, user_id, now)

        speakable = [memory for memory in memories if memory_is_speakable(memory)]
        opening, _focus_id = build_big_whale_opening(speakable)

        daily = living["daily"]
        chronotype = living["chronotype"]
        # 日程推进出此刻的生活状态，再由它产出带时间窗的候选信号。
        # 日程到此为止：它只能造候选，发送授权在下面的决策器手里。
        agenda_state = advance_agenda(now=now, chronotype=chronotype, daily_state=daily)
        signals = agenda_candidate_signals({**agenda_state, "conditions": daily.get("conditions") or []}, now=now)
        # 故事同样只能造候选，和日程走同一条闸门链，没有绕过 proactive 的路。
        signals.extend(story_candidate_signals(self._advance_story_locked(db, user_id, now), now=now))
        period = agenda_state.get("period") or period_of(now, chronotype)

        affinity_state = living["affinity"]
        # 关系权限是主动链路的一等闸门：主动关心要有许可和额度，
        # 提共同记忆要有许可。用户划下的线在这里立刻生效。
        permissions = relationship_permissions(
            affinity_state, boundaries=boundaries,
            user_quota=float(living_config["proactive"]["dailyLimit"]))
        care_sent = self._care_sent_today_locked(db, user_id, now)
        # 最小间隔要同时看两个口：普通回复和上一次主动开口。
        spoke_times = [parsed for parsed in (
            _parse_iso(last_spoke["created_at"] if last_spoke else ""),
            _parse_iso(pressure["lastProactiveAt"]),
        ) if parsed is not None]
        last_spoke_at = max(spoke_times).isoformat() if spoke_times else None
        result = evaluate_proactive_lifecycle(
            now=now,
            boundaries=boundaries,
            stored_candidates=stored,
            last_spoke_at=last_spoke_at,
            last_user_message_at=last_user["created_at"] if last_user else None,
            period=period,
            activity=activity,
            relationship_stage=str(affinity_state.get("stage") or "familiar"),
            interaction_state=str(affinity_state.get("interaction") or "relaxed"),
            pending_follow_ups=open_threads.get("pendingFollowUps") or [],
            carried_emotion=str(open_threads.get("carriedEmotion") or ""),
            carried_weight=float(open_threads.get("carriedWeight") or 0.0),
            opening=opening,
            signals=signals,
            daily_sent=pressure["dailySent"],
            daily_limit=int(living_config["proactive"]["dailyLimit"]),
            no_response_streak=pressure["noResponseStreak"],
            min_interval_minutes=float(living_config["proactive"]["minIntervalMinutes"]),
            permissions=permissions,
            care_sent=care_sent,
        )
        result["permissions"] = permissions
        result["agenda"] = {key: agenda_state[key] for key in
                            ("date", "period", "currentActivity", "location", "movedFrom",
                             "energy", "moodBias", "shareableEvents")}
        result["pressure"] = pressure
        # 向后兼容：桌宠轮询读顶层 content/kind/reason，这里展平一份。
        if result.get("selected"):
            selected = result["selected"]
            result["kind"] = selected.get("kind")
            result["reason"] = selected.get("reason")
            result["content"] = selected.get("content")
        return result

    def proactive_decision(self, user_id: str, activity: str = "idle") -> dict:
        """只读评估：轮询这个接口不会改变任何状态，也不算作"已经说过"。"""
        if not str(user_id).strip():
            raise ProtocolError("userId required")
        now = self._clock()
        with self._connect() as db:
            return self._evaluate_proactive_locked(db, user_id, activity, now)

    def commit_proactive_decision(self, payload: dict) -> dict:
        """评估并落定这一轮的结果，是主动消息真正"算作说过"的唯一入口。

        与 `claim_opening` 同构：同一个 deliveryId 重放拿到同一份结果，不会把一条主动
        记成两条。选中的候选转 `sent` 后永不再回队列，被延后的留在队列里等下一轮。
        """
        user_id = str(payload.get("userId") or "").strip()
        delivery_id = str(payload.get("deliveryId") or "").strip()
        activity = str(payload.get("activity") or "idle").strip() or "idle"
        if not user_id or not delivery_id or len(delivery_id) > 128:
            raise ProtocolError("invalid proactive delivery")
        now = self._clock()
        at = now.isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = db.execute(
                """SELECT response_json FROM opening_claims
                   WHERE user_id=? AND device_id='proactive' AND claim_id=?""",
                (user_id, delivery_id),
            ).fetchone()
            if replay is not None:
                return json.loads(replay["response_json"])
            result = self._evaluate_proactive_locked(db, user_id, activity, now)
            # 这一轮真的落定了，故事的推进才跟着写下来。
            self._tick_story_locked(db, user_id, now)
            selected = result.get("selected") or {}
            for candidate in result.get("candidates", []):
                signature = str(candidate.get("signature") or "")
                if not signature:
                    continue
                is_selected = candidate is selected or (
                    signature and signature == str(selected.get("signature") or ""))
                status = ("sent" if is_selected else
                          "expired" if candidate.get("vetoReason") == "window_expired" else
                          "dropped" if candidate.get("decision") == "drop" else "deferred")
                candidate["status"] = status
                db.execute(
                    """INSERT INTO proactive_candidates
                       (user_id, signature, kind, content, topic, status, decision, veto_reason,
                        score, window_start, best_until, expires_at, deliver_count,
                        created_at, updated_at, sent_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(user_id, signature) DO UPDATE SET
                         status=excluded.status, decision=excluded.decision,
                         veto_reason=excluded.veto_reason, score=excluded.score,
                         deliver_count=proactive_candidates.deliver_count+excluded.deliver_count,
                         updated_at=excluded.updated_at,
                         sent_at=CASE WHEN excluded.sent_at!='' THEN excluded.sent_at
                                      ELSE proactive_candidates.sent_at END""",
                    (user_id, signature, str(candidate.get("kind") or ""), str(candidate.get("content") or ""),
                     str(candidate.get("topic") or ""), status, str(candidate.get("decision") or ""),
                     str(candidate.get("vetoReason") or ""), float(candidate.get("score") or 0.0),
                     str(candidate.get("windowStart") or ""), str(candidate.get("bestUntil") or ""),
                     str(candidate.get("expiresAt") or ""), 1 if is_selected else 0,
                     str(candidate.get("createdAt") or at), at, at if is_selected else ""),
                )
            # 过期的候选清出队列。已发送的要留一阵子——它们是防重复那道锁本身——
            # 但签名都带日期，所以留 3 天足够，不必无限增长。
            db.execute(
                "DELETE FROM proactive_candidates WHERE user_id=? AND status='expired'", (user_id,))
            db.execute(
                """DELETE FROM proactive_candidates
                   WHERE user_id=? AND status='sent' AND sent_at!='' AND sent_at<?""",
                (user_id, (now - timedelta(days=3)).isoformat()),
            )
            result["deliveryId"] = delivery_id
            result["committedAt"] = at
            db.execute(
                """INSERT INTO opening_claims (user_id, device_id, claim_id, response_json, created_at)
                   VALUES (?, 'proactive', ?, ?, ?)""",
                (user_id, delivery_id, canonical_json(result), at),
            )
            return result

    def companion_status(self) -> dict:
        responder = self.responder
        enabled = bool(responder is not None and responder.available)
        with self._status_lock:
            last_source = self._last_reply_source
            last_at = self._last_reply_at
        return {
            "modelEnabled": enabled,
            "model": str(responder.name) if enabled else "",
            "fallback": "grounded_rules",
            "promptVersion": PROMPT_VERSION,
            "lastReplySource": last_source,
            "lastReplyAt": last_at,
        }

    @staticmethod
    def _message_payload(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {
            "messageId": row["message_id"],
            "role": row["role"],
            "text": row["text"],
            "createdAt": row["created_at"],
        }

    def messages(self, user_id: str, after_message_seq: int = 0, limit: int = 200) -> dict:
        with self._connect() as db:
            rows = db.execute(
                """SELECT message_seq, device_id, message_id, role, text, created_at
                   FROM conversation_messages
                   WHERE user_id=? AND message_seq>?
                   ORDER BY message_seq ASC LIMIT ?""",
                (
                    user_id,
                    max(0, int(after_message_seq)),
                    max(1, min(500, int(limit))),
                ),
            ).fetchall()
        messages = [{
            "messageSeq": int(row["message_seq"]),
            "deviceId": row["device_id"],
            "messageId": row["message_id"],
            "role": row["role"],
            "text": row["text"],
            "createdAt": row["created_at"],
        } for row in rows]
        return {
            "messages": messages,
            "nextMessageSeq": int(rows[-1]["message_seq"]) if rows else max(0, int(after_message_seq)),
            "hasMore": len(rows) >= max(1, min(500, int(limit))),
        }

    def claim_opening(self, payload: dict) -> dict:
        user_id = str(payload.get("userId") or "").strip()
        device_id = str(payload.get("deviceId") or "").strip()
        claim_id = str(payload.get("claimId") or "").strip()
        if not user_id or not device_id or not claim_id or len(claim_id) > 128:
            raise ProtocolError("invalid opening claim")
        now = self._now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous_claim = db.execute(
                """SELECT response_json FROM opening_claims
                   WHERE user_id=? AND device_id=? AND claim_id=?""",
                (user_id, device_id, claim_id),
            ).fetchone()
            if previous_claim is not None:
                response = json.loads(previous_claim["response_json"])
                response["duplicateClaim"] = True
                return response
            latest_message = db.execute(
                """SELECT message_id, role, text, created_at FROM conversation_messages
                   WHERE user_id=? ORDER BY message_seq DESC LIMIT 1""",
                (user_id,),
            ).fetchone()
            if latest_message is not None and latest_message["role"] == "assistant":
                response = {
                    "shouldSend": False,
                    "reason": "awaiting_user_reply",
                    "pendingAssistant": {
                        "messageId": latest_message["message_id"],
                        "text": latest_message["text"],
                        "createdAt": latest_message["created_at"],
                    },
                }
            else:
                cursor_row = db.execute(
                    """SELECT last_memory_seq FROM companion_cursors
                       WHERE user_id=? AND device_id=?""",
                    (user_id, device_id),
                ).fetchone()
                cursor = int(cursor_row["last_memory_seq"]) if cursor_row else 0
                episode = db.execute(
                    """SELECT episode_id, local_date, revision, status
                       FROM daily_episodes WHERE user_id=?
                       ORDER BY local_date DESC, revision DESC LIMIT 1""",
                    (user_id,),
                ).fetchone()
                if episode is None or episode["status"] != "delivered":
                    response = {"shouldSend": False, "reason": "no_new_memory"}
                else:
                    episode_id = str(episode["episode_id"])
                    episode_revision = int(episode["revision"])
                    newest_date = str(episode["local_date"])
                    rows = db.execute(
                        """SELECT event.server_seq, event.payload_json
                           FROM daily_episode_memories AS membership
                           JOIN memory_events AS event
                             ON event.user_id=membership.user_id AND event.memory_id=membership.memory_id
                           WHERE membership.user_id=? AND membership.episode_id=?
                           ORDER BY event.server_seq ASC""",
                        (user_id, episode_id),
                    ).fetchall()
                    episode_memories = self._decorate_memory_rows(db, user_id, rows)
                    memories = [memory for memory in episode_memories if memory_is_speakable(memory)]
                    through = max((int(row["server_seq"]) for row in rows), default=cursor)
                    text, focus_id = build_big_whale_opening(memories)
                    if not text:
                        response = {"shouldSend": False, "reason": "no_speakable_memory"}
                    else:
                        digest = payload_hash({
                            "userId": user_id, "deviceId": device_id,
                            "episodeId": episode_id, "episodeRevision": episode_revision,
                        })[:24]
                        message_id = f"opening_{digest}"
                        db.execute(
                            """INSERT OR IGNORE INTO conversation_messages
                               (user_id, device_id, message_id, role, text, created_at)
                               VALUES (?, ?, ?, 'assistant', ?, ?)""",
                            (user_id, device_id, message_id, text, now),
                        )
                        db.execute(
                            """INSERT INTO companion_cursors
                               (user_id, device_id, last_memory_seq, updated_at)
                               VALUES (?, ?, ?, ?)
                               ON CONFLICT(user_id, device_id) DO UPDATE SET
                                 last_memory_seq=excluded.last_memory_seq,
                                 updated_at=excluded.updated_at""",
                            (user_id, device_id, through, now),
                        )
                        if focus_id:
                            db.execute(
                                """INSERT OR IGNORE INTO conversation_memory_refs
                                   (user_id, message_id, memory_id, created_at)
                                   VALUES (?, ?, ?, ?)""",
                                (user_id, message_id, focus_id, now),
                            )
                            self._record_memory_mention_locked(
                                db, user_id=user_id, memory_id=focus_id,
                                message_id=message_id, mention_type="opening", now=now,
                            )
                        latest_memory = max(
                            memories,
                            key=lambda item: (
                                str(item.get("occurredAt") or item.get("endedAt") or item.get("createdAt") or ""),
                                int(item.get("revision") or 0),
                            ),
                        )
                        response = {
                            "shouldSend": True,
                            "reason": "new_memory",
                            "messageId": message_id,
                            "text": text,
                            "focusMemoryId": focus_id,
                            "consumedThroughServerSeq": through,
                            "latestMemory": latest_memory,
                            "episodeId": episode_id,
                            "episodeRevision": episode_revision,
                            "episodeDate": newest_date,
                        }
                    db.execute(
                        """UPDATE daily_episodes SET status='consumed', consumed_at=?, updated_at=?
                           WHERE user_id=? AND episode_id=? AND revision=?""",
                        (now, now, user_id, episode_id, episode_revision),
                    )
                    if not text:
                        db.execute(
                            """INSERT INTO companion_cursors
                               (user_id, device_id, last_memory_seq, updated_at)
                               VALUES (?, ?, ?, ?)
                               ON CONFLICT(user_id, device_id) DO UPDATE SET
                                 last_memory_seq=MAX(last_memory_seq, excluded.last_memory_seq),
                                 updated_at=excluded.updated_at""",
                            (user_id, device_id, through, now),
                        )
            db.execute(
                """INSERT INTO opening_claims
                   (user_id, device_id, claim_id, response_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, device_id, claim_id, canonical_json(response), now),
            )
            return response

    def debug_snapshot(self, user_id: str, local_date: str = "") -> dict:
        """Return inspectable evidence for the local development console."""
        with self._connect() as db:
            rows = db.execute(
                """SELECT server_seq, device_id, memory_id, payload_json, received_at
                   FROM memory_events WHERE user_id=? ORDER BY server_seq DESC LIMIT 500""",
                (user_id,),
            ).fetchall()
            memories = self._decorate_memory_rows(db, user_id, list(reversed(rows)))
            batches = db.execute(
                """SELECT batch_id, device_id, latest_revision, accepted_at
                   FROM memory_batches WHERE user_id=? ORDER BY accepted_at DESC LIMIT 100""",
                (user_id,),
            ).fetchall()
            cursors = db.execute(
                """SELECT device_id, last_memory_seq, updated_at
                   FROM companion_cursors WHERE user_id=? ORDER BY updated_at DESC""",
                (user_id,),
            ).fetchall()
            refs = db.execute(
                """SELECT message_id, memory_id, created_at
                   FROM conversation_memory_refs WHERE user_id=? ORDER BY created_at DESC LIMIT 100""",
                (user_id,),
            ).fetchall()
            threads = db.execute(
                """SELECT thread_id, memory_id, label, topic, status, mention_count,
                          need, follow_up_hint, last_user_message_id,
                          episode_id, opened_at, last_mentioned_at, updated_at
                   FROM emotional_threads WHERE user_id=?
                   ORDER BY updated_at DESC LIMIT 100""",
                (user_id,),
            ).fetchall()
            mentions = db.execute(
                """SELECT memory_id, message_id, mention_type, created_at
                   FROM memory_mentions WHERE user_id=?
                   ORDER BY mention_seq DESC LIMIT 100""",
                (user_id,),
            ).fetchall()
            persisted_episodes = db.execute(
                """SELECT episode_id AS id, local_date AS date, revision, status,
                          memory_count AS memoryCount, delivered_at AS deliveredAt,
                          consumed_at AS consumedAt, updated_at AS updatedAt
                   FROM daily_episodes WHERE user_id=? ORDER BY local_date DESC""",
                (user_id,),
            ).fetchall()
            recent_messages = db.execute(
                """SELECT role, text FROM conversation_messages
                   WHERE user_id=? ORDER BY message_seq DESC LIMIT 12""",
                (user_id,),
            ).fetchall()
            trace_rows = db.execute(
                """SELECT reply_message_id, trace_json, created_at
                   FROM reply_traces WHERE user_id=?
                   ORDER BY created_at DESC LIMIT 20""",
                (user_id,),
            ).fetchall()
            user_facts, boundaries = self._profile_memories_locked(db, user_id)
            open_threads = self._open_threads_locked(db, user_id)
            standing = self._standing_knowledge_locked(db, user_id)
            persona_state = self._persona_card_locked(db, user_id)
            living = self._living_companion_inputs_locked(db, user_id)
            review_rows = db.execute(
                "SELECT insight_id, status, insight_json, updated_at FROM insight_reviews WHERE user_id=?",
                (user_id,),
            ).fetchall()
            digests = [json.loads(row["digest_json"]) for row in db.execute(
                """SELECT digest_json FROM memory_digests WHERE user_id=?
                   ORDER BY period_start DESC, kind ASC LIMIT 30""",
                (user_id,),
            ).fetchall()]
        server_meta = {
            str(row["memory_id"]): {
                "serverSeq": int(row["server_seq"]),
                "deviceId": row["device_id"],
                "receivedAt": row["received_at"],
            }
            for row in rows
        }
        for memory in memories:
            memory["server"] = server_meta.get(str(memory.get("id") or ""), {})
        reviews = {str(row["insight_id"]): row for row in review_rows}
        insight_drafts = []
        for draft in build_insight_drafts(memories):
            review = reviews.get(draft["id"])
            insight_drafts.append(
                {**draft, **(json.loads(review["insight_json"]) if review else {}),
                 "reviewedAt": review["updated_at"] if review else ""}
            )
        all_episodes = self._daily_episodes(memories)
        if local_date:
            memories = [memory for memory in memories if self._memory_local_date(memory) == local_date]
        recent_conversation = [
            {"role": row["role"], "text": row["text"]}
            for row in reversed(recent_messages)
        ]
        latest_user_index = next((
            index for index in range(len(recent_conversation) - 1, -1, -1)
            if recent_conversation[index]["role"] == "user"
        ), -1)
        mind_conversation = recent_conversation[:latest_user_index + 1] if latest_user_index >= 0 else []
        current_mind = build_companion_mind(
            mind_conversation[-1]["text"] if mind_conversation else "", mind_conversation,
        )
        debug_profile = profile_context(user_facts, boundaries)
        debug_profile["companionMind"] = current_mind
        debug_profile["standingKnowledge"] = standing_knowledge_context(standing)
        debug_profile["personaCard"] = persona_state["active"]
        current_frame = build_companion_frame(
            user_text=mind_conversation[-1]["text"] if mind_conversation else "",
            conversation=mind_conversation, memories=memories, user_facts=user_facts,
            boundaries=boundaries, persona=persona_state["active"],
            threads=open_threads,
            affinity_state=living["affinity"], daily_state=living["daily"],
            chronotype=living["chronotype"], timeline_events=living["timeline"], story_provider=living["story"],
            expression_rules=living["expressions"],
        )
        debug_profile["companionFrame"] = current_frame
        return {
            "userId": user_id,
            "database": str(self.path.expanduser().resolve()),
            "memories": memories,
            "selectedDate": local_date,
            "episodes": [dict(row) for row in persisted_episodes] or all_episodes,
            "batches": [dict(row) for row in batches],
            "cursors": [dict(row) for row in cursors],
            "conversationMemoryRefs": [dict(row) for row in refs],
            "emotionalThreads": [dict(row) for row in threads],
            "memoryMentions": [dict(row) for row in mentions],
            "memoryDigests": digests,
            "insightDrafts": insight_drafts,
            "replyTraces": [
                {
                    **json.loads(row["trace_json"]),
                    "replyMessageId": row["reply_message_id"],
                    "createdAt": row["created_at"],
                }
                for row in trace_rows
            ],
            "userFacts": user_facts,
            "boundaries": boundaries,
            "standingKnowledge": standing,
            "companion": self.companion_status(),
            "persona": {
                "promptVersion": PROMPT_VERSION,
                "systemPrompt": persona_state["compiledPrompt"] + "\n\n" + SYSTEM_PROMPT,
                "modelContext": build_model_context(memories, debug_profile),
                "card": persona_state,
            },
            "identity": {
                "identityVersion": IDENTITY_VERSION,
                "petId": CANONICAL_PET_ID,
                "productName": "回声",
                "desktopName": "小鲸",
                "mobileName": "大鲸",
                "memoryOwner": "shared-companion-service",
            },
            "dialogueState": emotional_dialogue_state(recent_conversation),
            "companionMind": current_mind,
            "companionFrame": current_frame,
            "living": {
                "affinity": living["affinity"],
                "daily": living["daily"],
                "chronotype": living["chronotype"],
                "timeline": living["timeline"],
                "expressions": living["expressions"],
                "config": self._living_config_locked(db, user_id),
            },
        }

    def preview_opening(self, user_id: str, local_date: str = "") -> dict:
        """Regenerate an opening without advancing the phone cursor."""
        with self._connect() as db:
            episode_date = str(local_date or "").strip()
            if not episode_date:
                latest = db.execute(
                    """SELECT local_date FROM daily_episodes WHERE user_id=?
                       ORDER BY local_date DESC, revision DESC LIMIT 1""",
                    (user_id,),
                ).fetchone()
                episode_date = str(latest["local_date"] or "") if latest else ""
            rows = db.execute(
                """SELECT event.server_seq, event.payload_json
                   FROM daily_episode_memories AS membership
                   JOIN memory_events AS event
                     ON event.user_id=membership.user_id AND event.memory_id=membership.memory_id
                   WHERE membership.user_id=? AND membership.episode_id=?
                   ORDER BY event.server_seq ASC""",
                (user_id, f"daily:{episode_date}"),
            ).fetchall()
            memories = [
                memory for memory in self._decorate_memory_rows(db, user_id, rows)
                if memory_is_speakable(memory)
            ]
        text, focus_id = build_big_whale_opening(memories)
        return {
            "text": text,
            "focusMemoryId": focus_id,
            "memoryCount": len(memories),
            "episodeDate": episode_date,
            "generatedAt": _now(),
            "persisted": False,
        }

    def delete_debug_memories(self, user_id: str, memory_ids: list[str]) -> dict:
        """Delete only explicitly test-marked memories from the local database."""
        requested = {str(value).strip() for value in memory_ids if str(value).strip()}
        allowed = {value for value in requested if value.startswith("debug_")}
        if requested - allowed:
            raise ProtocolError("only debug_ memories can be deleted")
        if not allowed:
            return {"deleted": 0, "memoryIds": []}
        placeholders = ",".join("?" for _ in allowed)
        params = [user_id, *sorted(allowed)]
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for table in (
                "conversation_memory_refs", "memory_mentions", "emotional_thread_memories", "emotional_threads",
                "daily_episode_memories", "memory_lifecycle_events", "memory_lifecycle",
            ):
                db.execute(
                    f"DELETE FROM {table} WHERE user_id=? AND memory_id IN ({placeholders})",
                    params,
                )
            cursor = db.execute(
                f"DELETE FROM memory_events WHERE user_id=? AND memory_id IN ({placeholders})",
                params,
            )
            db.execute(
                """DELETE FROM daily_episodes WHERE user_id=? AND NOT EXISTS (
                     SELECT 1 FROM daily_episode_memories AS membership
                     WHERE membership.user_id=daily_episodes.user_id
                       AND membership.episode_id=daily_episodes.episode_id)""",
                (user_id,),
            )
        return {"deleted": int(cursor.rowcount), "memoryIds": sorted(allowed)}


class MemoryApiServer:
    def __init__(
        self,
        repository: MemoryRepository,
        *,
        token: str,
        host: str = "127.0.0.1",
        port: int = 47821,
        static_dir: Path | str | None = None,
    ) -> None:
        self.repository = repository
        self.token = str(token)
        self.host = host
        self.port = int(port)
        self.static_dir = Path(static_dir) if static_dir is not None else Path(__file__).resolve().parents[1] / "mobile"
        self.debug_dir = Path(__file__).resolve().parents[1] / "debug"
        self.asset_dir = Path(__file__).resolve().parents[1] / "assets"
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def _handler(self):
        repository = self.repository
        expected_token = self.token
        static_dir = self.static_dir
        debug_dir = self.debug_dir
        asset_dir = self.asset_dir

        class Handler(BaseHTTPRequestHandler):
            server_version = "WhaleMemory/1"

            def log_message(self, fmt, *args):
                return

            def _json(self, status: int, payload: dict) -> None:
                body = canonical_json(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _file(self, path: Path, *, cache: bool = True) -> None:
                try:
                    body = path.read_bytes()
                except OSError:
                    self._json(404, {"error": "not_found"})
                    return
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=86400" if cache else "no-cache")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                    "script-src 'self'; connect-src 'self' http://127.0.0.1:47890 "
                    "http://localhost:47890; manifest-src 'self'",
                )
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self) -> bool:
                value = self.headers.get("Authorization", "")
                supplied = value[7:] if value.startswith("Bearer ") else ""
                return bool(expected_token) and hmac.compare_digest(supplied, expected_token)

            def _body(self) -> dict:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise ProtocolError("invalid Content-Length") from None
                if length <= 0 or length > MAX_REQUEST_BYTES:
                    raise ProtocolError("invalid request size")
                try:
                    value = json.loads(self.rfile.read(length).decode("utf-8"))
                except (UnicodeError, ValueError):
                    raise ProtocolError("invalid JSON body") from None
                if not isinstance(value, dict):
                    raise ProtocolError("body must be an object")
                return value

            def _dispatch(self) -> None:
                parsed = urlparse(self.path)
                static_routes = {
                    "/": "index.html",
                    "/index.html": "index.html",
                    "/styles.css": "styles.css",
                    "/app.js": "app.js",
                    "/manifest.webmanifest": "manifest.webmanifest",
                    "/sw.js": "sw.js",
                }
                debug_routes = {
                    "/debug": "index.html",
                    "/debug/": "index.html",
                    "/debug/styles.css": "styles.css",
                    "/debug/app.js": "app.js",
                }
                media_routes = {
                    "/media/whale-v2.jpg": asset_dir / "chat" / "whale-v2.jpg",
                    "/media/ojingjing.jpg": asset_dir / "big_blue_fat_fish" / "ojingjing.jpg",
                }
                if self.command == "GET" and parsed.path in static_routes:
                    self._file(static_dir / static_routes[parsed.path], cache=parsed.path != "/sw.js")
                    return
                if self.command == "GET" and parsed.path in debug_routes:
                    self._file(debug_dir / debug_routes[parsed.path], cache=False)
                    return
                if self.command == "GET" and parsed.path in media_routes:
                    self._file(media_routes[parsed.path])
                    return
                if parsed.path == "/health" and self.command == "GET":
                    self._json(200, {
                        "ok": True,
                        "protocolVersion": 1,
                        "companion": repository.companion_status(),
                    })
                    return
                if not self._authorized():
                    self._json(401, {"error": "unauthorized"})
                    return
                if self.command == "POST" and parsed.path == "/v1/memory/batches":
                    self._json(200, repository.ingest_batch(self._body()))
                    return
                if self.command == "POST" and parsed.path == "/v1/daily-episodes":
                    self._json(200, repository.ingest_episode(self._body()))
                    return
                if self.command == "POST" and parsed.path == "/v1/memory/lifecycle":
                    self._json(200, repository.update_lifecycle(self._body()))
                    return
                if self.command == "POST" and parsed.path == "/v1/memory/reinforce":
                    self._json(200, repository.reinforce_memory(self._body()))
                    return
                if self.command == "GET" and parsed.path == "/v1/memory/digests":
                    query = parse_qs(parsed.query)
                    self._json(200, repository.memory_digests(
                        str((query.get("userId") or [""])[0]).strip(),
                        str((query.get("kind") or [""])[0]).strip(),
                    ))
                    return
                if self.command == "GET" and parsed.path == "/v1/memory/stream":
                    query = parse_qs(parsed.query)
                    user_id = str((query.get("userId") or [""])[0]).strip()
                    if not user_id:
                        raise ProtocolError("userId required")
                    after = int((query.get("afterServerSeq") or ["0"])[0])
                    limit = int((query.get("limit") or ["200"])[0])
                    self._json(200, repository.stream(user_id, after, limit))
                    return
                if self.command == "POST" and parsed.path == "/v1/companion/openings/claim":
                    self._json(200, repository.claim_opening(self._body()))
                    return
                if self.command == "POST" and parsed.path == "/v1/conversation/messages":
                    self._json(200, repository.append_message(self._body()))
                    return
                if self.command == "GET" and parsed.path == "/v1/profile-memories":
                    query = parse_qs(parsed.query)
                    user_id = str((query.get("userId") or [""])[0]).strip()
                    self._json(200, repository.profile_memories(user_id))
                    return
                if self.command == "POST" and parsed.path == "/v1/profile-memories/actions":
                    self._json(200, repository.update_profile_memory(self._body()))
                    return
                if self.command == "POST" and parsed.path == "/v1/insight-drafts/actions":
                    self._json(200, repository.review_insight(self._body()))
                    return
                if self.command == "POST" and parsed.path == "/v1/companion/proactive/deliveries":
                    self._json(200, repository.commit_proactive_decision(self._body()))
                    return
                if self.command == "GET" and parsed.path == "/v1/companion/proactive":
                    query = parse_qs(parsed.query)
                    user_id = str((query.get("userId") or [""])[0]).strip()
                    if not user_id:
                        raise ProtocolError("userId required")
                    activity = str((query.get("activity") or ["idle"])[0]).strip() or "idle"
                    self._json(200, repository.proactive_decision(user_id, activity))
                    return
                if self.command == "GET" and parsed.path == "/v1/companion/emotion":
                    query = parse_qs(parsed.query)
                    user_id = str((query.get("userId") or [""])[0]).strip()
                    if not user_id:
                        raise ProtocolError("userId required")
                    self._json(200, repository.companion_emotion(user_id))
                    return
                if self.command == "GET" and parsed.path == "/v1/companion/story":
                    query = parse_qs(parsed.query)
                    self._json(200, repository.story_state(
                        str((query.get("userId") or [""])[0]).strip()))
                    return
                if self.command == "POST" and parsed.path == "/v1/companion/story/actions":
                    self._json(200, repository.act_on_story(self._body()))
                    return
                if self.command == "GET" and parsed.path == "/v1/companion/relationship":
                    query = parse_qs(parsed.query)
                    self._json(200, repository.relationship_view(
                        str((query.get("userId") or [""])[0]).strip()))
                    return
                if self.command == "GET" and parsed.path == "/v1/companion/affinity":
                    query = parse_qs(parsed.query)
                    user_id = str((query.get("userId") or [""])[0]).strip()
                    if not user_id:
                        raise ProtocolError("userId required")
                    self._json(200, repository.affinity_state(user_id))
                    return
                if self.command == "POST" and parsed.path == "/v1/companion/affinity/events":
                    self._json(200, repository.apply_affinity_event(self._body()))
                    return
                if self.command == "GET" and parsed.path == "/v1/companion/daily-state":
                    query = parse_qs(parsed.query)
                    user_id = str((query.get("userId") or [""])[0]).strip()
                    if not user_id:
                        raise ProtocolError("userId required")
                    self._json(200, repository.daily_state(user_id))
                    return
                if self.command == "GET" and parsed.path == "/v1/companion/self-timeline":
                    query = parse_qs(parsed.query)
                    user_id = str((query.get("userId") or [""])[0]).strip()
                    if not user_id:
                        raise ProtocolError("userId required")
                    self._json(200, {"events": repository.self_timeline_events(user_id)})
                    return
                if self.command == "POST" and parsed.path == "/v1/companion/self-timeline/events":
                    self._json(200, repository.append_self_timeline_event(self._body()))
                    return
                if self.command == "GET" and parsed.path == "/v1/companion/expressions":
                    query = parse_qs(parsed.query)
                    user_id = str((query.get("userId") or [""])[0]).strip()
                    if not user_id:
                        raise ProtocolError("userId required")
                    self._json(200, {"rules": repository.expression_rules(user_id)})
                    return
                if self.command == "POST" and parsed.path == "/v1/companion/expressions/review":
                    self._json(200, repository.review_expression_rule(self._body()))
                    return
                if self.command == "POST" and parsed.path == "/v1/companion/expressions/delete":
                    self._json(200, repository.delete_expression_rule(self._body()))
                    return
                if self.command == "POST" and parsed.path == "/v1/companion/self-timeline/delete":
                    self._json(200, repository.delete_self_timeline_event(self._body()))
                    return
                if self.command == "POST" and parsed.path == "/v1/companion/affinity/adjust":
                    self._json(200, repository.adjust_affinity(self._body()))
                    return
                if self.command == "POST" and parsed.path == "/v1/companion/chronotype":
                    self._json(200, repository.set_chronotype(self._body()))
                    return
                if self.command == "GET" and parsed.path == "/v1/companion/living-config":
                    query = parse_qs(parsed.query)
                    user_id = str((query.get("userId") or [""])[0]).strip()
                    if not user_id:
                        raise ProtocolError("userId required")
                    self._json(200, repository.living_config(user_id))
                    return
                if self.command == "POST" and parsed.path == "/v1/companion/living-config":
                    self._json(200, repository.update_living_config(self._body()))
                    return
                if self.command == "GET" and parsed.path == "/v1/persona-card":
                    query = parse_qs(parsed.query)
                    user_id = str((query.get("userId") or [""])[0]).strip()
                    self._json(200, repository.persona_card(user_id))
                    return
                if self.command == "POST" and parsed.path == "/v1/persona-card/actions":
                    self._json(200, repository.update_persona_card(self._body()))
                    return
                if self.command == "POST" and parsed.path == "/v1/persona-card/preview":
                    self._json(200, repository.preview_persona(self._body()))
                    return
                if self.command == "GET" and parsed.path == "/v1/conversation/messages":
                    query = parse_qs(parsed.query)
                    user_id = str((query.get("userId") or [""])[0]).strip()
                    if not user_id:
                        raise ProtocolError("userId required")
                    after = int((query.get("afterMessageSeq") or ["0"])[0])
                    limit = int((query.get("limit") or ["200"])[0])
                    self._json(200, repository.messages(user_id, after, limit))
                    return
                if self.command == "GET" and parsed.path == "/v1/debug/state":
                    query = parse_qs(parsed.query)
                    user_id = str((query.get("userId") or [""])[0]).strip()
                    if not user_id:
                        raise ProtocolError("userId required")
                    local_date = str((query.get("date") or [""])[0]).strip()
                    self._json(200, repository.debug_snapshot(user_id, local_date))
                    return
                if self.command == "POST" and parsed.path == "/v1/debug/openings/regenerate":
                    payload = self._body()
                    user_id = str(payload.get("userId") or "").strip()
                    if not user_id:
                        raise ProtocolError("userId required")
                    self._json(200, repository.preview_opening(user_id, str(payload.get("date") or "")))
                    return
                if self.command == "DELETE" and parsed.path == "/v1/debug/memories":
                    payload = self._body()
                    user_id = str(payload.get("userId") or "").strip()
                    memory_ids = payload.get("memoryIds")
                    if not user_id or not isinstance(memory_ids, list):
                        raise ProtocolError("userId and memoryIds required")
                    self._json(200, repository.delete_debug_memories(user_id, memory_ids))
                    return
                self._json(404, {"error": "not_found"})

            def do_GET(self):  # noqa: N802
                try:
                    self._dispatch()
                except ProtocolError as exc:
                    self._json(400, {"error": str(exc)})
                except (TypeError, ValueError):
                    self._json(400, {"error": "invalid query"})
                except MemoryConflictError as exc:
                    self._json(409, {"error": str(exc)})
                except Exception:
                    self._json(500, {"error": "internal_error"})

            do_POST = do_GET
            do_DELETE = do_GET

        return Handler

    def start(self) -> int:
        if self._server is not None:
            return self.port
        self._server = ThreadingHTTPServer((self.host, self.port), self._handler())
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="whale-memory-api",
        )
        self._thread.start()
        return self.port

    def stop(self) -> None:
        server = self._server
        self._server = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(2.0)
