# -*- coding: utf-8 -*-
"""单用户共享记忆服务：SQLite 持久化、幂等批次与原子开场领取。"""
from __future__ import annotations

import hmac
import json
import mimetypes
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .dialogue_state import emotional_dialogue_state
from .companion_mind import build_companion_mind, contextual_fallback
from .companion_runtime import build_companion_frame
from .standing_knowledge import derive_standing_knowledge, standing_knowledge_context
from .emotion import explicit_emotion_label
from .companion_llm import CompanionResponder
from .companion_llm import PROMPT_VERSION, SYSTEM_PROMPT, build_model_context, reply_violates_boundaries, reply_has_style_violation
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryConflictError(RuntimeError):
    pass


class MemoryRepository:
    def __init__(self, path: Path | str, responder: CompanionResponder | None = None) -> None:
        self.path = Path(path)
        self.responder = responder
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self._reply_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._last_reply_source = ""
        self._last_reply_at = ""
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

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
        rows = db.execute(
            """SELECT memory_id, status, reason, source_type, source_message_id, updated_at
               FROM memory_lifecycle WHERE user_id=?""",
            (user_id,),
        ).fetchall()
        return {
            str(row["memory_id"]): {
                "status": row["status"],
                "reason": row["reason"],
                "sourceType": row["source_type"],
                "sourceMessageId": row["source_message_id"],
                "updatedAt": row["updated_at"],
            }
            for row in rows
        }

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
        message_id: str, mention_type: str, now: str,
    ) -> None:
        if not memory_id:
            return
        cursor = db.execute(
            """INSERT OR IGNORE INTO memory_mentions
               (user_id, memory_id, message_id, mention_type, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, memory_id, message_id, mention_type, now),
        )
        if cursor.rowcount:
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
        thread_status = status if status in {"active", "resolved", "dormant", "suppressed"} else ""
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
        now = _now()
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

    def ingest_batch(self, payload: dict) -> dict:
        batch = validate_batch(payload)
        body_hash = payload_hash({
            "latestRevision": batch["latestRevision"],
            "memories": batch["memories"],
        })
        now = _now()
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
            latest_seq = db.execute(
                "SELECT COALESCE(MAX(server_seq), 0) FROM memory_events WHERE user_id=?",
                (batch["userId"],),
            ).fetchone()[0]
            return {
                "accepted": True,
                "duplicate": accepted == 0,
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
        now = _now()
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
        now = _now()
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
        conversation = [{"role": "user", "text": text}]
        mind = build_companion_mind(text, conversation)
        profile = profile_context(facts, boundaries)
        profile.update({"companionMind": mind, "personaCard": card})
        profile["standingKnowledge"] = standing_knowledge_context(standing)
        profile["companionFrame"] = build_companion_frame(
            user_text=text, conversation=conversation, memories=memories,
            user_facts=facts, boundaries=boundaries, persona=card,
            threads=open_threads,
        )
        reply = contextual_fallback(text, conversation, mind)
        source = "fallback"
        if self.responder is not None and self.responder.available:
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
        now = _now()
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
        now = _now()
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

        assistant_message = None
        reply_source = ""
        if role == "user":
            reply_focus_memory_id = emotion_id if emotion_recorded else focus_memory_id
            if lifecycle_updates and lifecycle_updates[-1]["status"] in {"resolved", "dormant", "suppressed"}:
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
            now = _now()
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
                    """SELECT role, text FROM conversation_messages
                       WHERE user_id=? ORDER BY message_seq DESC LIMIT 24""",
                    (user_id,),
                ).fetchall()

                memories = self._decorate_memory_rows(db, user_id, list(reversed(memory_rows)))
                user_facts, boundaries = self._profile_memories_locked(db, user_id)
                open_threads = self._open_threads_locked(db, user_id)
                standing = self._standing_knowledge_locked(db, user_id)
                persona_state = self._persona_card_locked(db, user_id)
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
                {"role": row["role"], "text": row["text"]}
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
                threads=open_threads,
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
            if responder is not None and responder.available:
                trace["modelAttempted"] = True
                try:
                    candidate = str(responder.reply(
                        memories, conversation, profile,
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

            reply_time = _now()
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
        now = _now()
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
            review_rows = db.execute(
                "SELECT insight_id, status, insight_json, updated_at FROM insight_reviews WHERE user_id=?",
                (user_id,),
            ).fetchall()
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
