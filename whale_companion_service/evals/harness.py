# -*- coding: utf-8 -*-
"""把一个评测用例跑成一次真实的对话轮，并采集可断言的观测。

这里不复制任何陪伴逻辑。用例的初始状态全部通过仓库的公开写入口落库
（`ingest_batch` / `ingest_perception` / `apply_affinity_event` / `append_message`），
所以评测走的就是产品自己那条路：投影、仲裁、帧、turn decision、防火墙、降级。

数据库永远是临时目录里的一次性 SQLite，用户永远是合成的 `eval-user`。
真实用户数据与真实用户库不参与评测，也不会被读到。
"""
from __future__ import annotations

import json
import hashlib
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..companion_llm import CompanionModelError
from ..memory_server import MemoryRepository
from ..persona_card import DEFAULT_PERSONA_CARD
from .model import EvalCase

EVAL_USER_ID = "eval-user"
EVAL_DEVICE_ID = "eval-device"


class ScriptedResponder:
    """固定输出或固定失败的假模型。

    它存在只有两个用途：验出口事实防火墙（给一句已知越界的话），
    以及验降级（每次都抛）。它**不**自我审查——这正是重点：
    防火墙如果只长在某一个 responder 实现里，换一个实现就没有了。
    """

    def __init__(self, *, text: str = "", failure: str = "", name: str = "scripted/eval") -> None:
        self._text = text
        self._failure = failure
        self.name = name
        self.calls = 0

    @property
    def available(self) -> bool:
        return True

    def reply(self, memories, conversation, profile_memory=None) -> str:
        self.calls += 1
        if self._failure:
            raise CompanionModelError(self._failure)
        return self._text

    def judge_json(self, *, task, context, fields, fallback):
        return dict(fallback), "fallback"


class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 只出现在断言细节里
        return "<missing>"


MISSING = _Missing()


@dataclass
class TurnObservation:
    """一轮真实对话之后，评测能看到的全部东西。"""

    case_id: str
    frame: dict = field(default_factory=dict)
    model_view: dict = field(default_factory=dict)
    reply: str = ""
    reply_source: str = ""
    trace: dict = field(default_factory=dict)
    proactive: dict = field(default_factory=dict)
    perception: dict = field(default_factory=dict)
    relationship: dict = field(default_factory=dict)
    profile: dict = field(default_factory=dict)
    state_digest: dict = field(default_factory=dict)
    replay_digest: dict = field(default_factory=dict)
    persona_version: str = ""
    elapsed_ms: float = 0.0
    model_name: str = ""
    error: str = ""

    def path(self, dotted: str) -> Any:
        """按点号路径读帧里的值；读不到返回 MISSING 而不是 None。

        None 是合法取值（`selfTimeline` 没被触发时就是 None），所以"没有这个键"
        必须和"这个键是 None"分开，否则一条断言会在两种完全不同的情况下都通过。
        """
        node: Any = self.frame
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list) and part.lstrip("-").isdigit():
                index = int(part)
                if -len(node) <= index < len(node):
                    node = node[index]
                else:
                    return MISSING
            else:
                return MISSING
        return node


def _state_digest(db_path: Path) -> dict:
    """状态推进指纹：每张表的行数与内容哈希。

    只比行数会漏掉 ``UPDATE counter=counter+1`` 这类最危险的
    重放副作用。评测库只含合成数据，因此可以对整张表做稳定哈希；
    报告仍只保留哈希，不泄露正文。
    """
    fingerprints: dict[str, dict] = {}
    with sqlite3.connect(db_path) as db:
        tables = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for table in tables:
            if table.startswith("sqlite_"):
                continue
            try:
                quoted = '"' + str(table).replace('"', '""') + '"'
                rows = [list(row) for row in db.execute("SELECT * FROM " + quoted).fetchall()]
                encoded = json.dumps(
                    sorted(rows, key=lambda row: json.dumps(row, ensure_ascii=False, default=str)),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
                fingerprints[table] = {
                    "rows": len(rows),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                }
            except sqlite3.Error:  # pragma: no cover - 结构变动时不让评测崩掉
                continue
    return fingerprints


def _latest_trace(db_path: Path, reply_message_id: str) -> dict:
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT trace_json FROM reply_traces WHERE user_id=? AND reply_message_id=?",
            (EVAL_USER_ID, reply_message_id),
        ).fetchone()
    if row is None:
        return {}
    try:
        return json.loads(row["trace_json"])
    except (TypeError, ValueError):  # pragma: no cover
        return {}


def seed_repository(repo: MemoryRepository, case: EvalCase) -> str:
    """把用例声明的初始状态写进库里，全部走公开写入口。返回本轮钉住的人格版本。

    人格卡永远写进这个**合成用户的临时库**。评测不读也不改任何真实用户
    已经发布的 PersonaCard。
    """
    persona_version = "default/schema-%s" % DEFAULT_PERSONA_CARD["schemaVersion"]
    if case.persona:
        card = {**DEFAULT_PERSONA_CARD, **case.persona}
        repo.update_persona_card({"userId": EVAL_USER_ID, "action": "saveDraft", "card": card})
        state = repo.update_persona_card({"userId": EVAL_USER_ID, "action": "publish"})
        persona_version = "%s/v%s" % (case.persona_version, state["activeVersion"])
    if case.affinity_score is not None:
        repo.adjust_affinity({"userId": EVAL_USER_ID, "score": float(case.affinity_score)})
    for event in case.affinity_events:
        repo.apply_affinity_event({"userId": EVAL_USER_ID, "event": dict(event)})
    if case.memories:
        rows = [dict(row) for row in case.memories]
        repo.ingest_batch({
            "protocolVersion": 1,
            "userId": EVAL_USER_ID,
            "deviceId": EVAL_DEVICE_ID,
            "batchId": case.case_id + "-batch",
            "latestRevision": max(int(row.get("revision") or 1) for row in rows),
            "memories": rows,
        })
    if case.perception:
        repo.ingest_perception({
            "userId": EVAL_USER_ID,
            "observations": [dict(row) for row in case.perception],
        })
    for index, turn in enumerate(case.history):
        repo.append_message({
            "userId": EVAL_USER_ID,
            "deviceId": EVAL_DEVICE_ID,
            "messageId": "%s-h%d" % (case.case_id, index),
            "role": str(turn.get("role") or "user"),
            "text": str(turn.get("text") or ""),
        })
    return persona_version


def run_case(case: EvalCase, *, responder=None, workdir: Path | None = None,
             keep_db: bool = False) -> TurnObservation:
    """跑完一个用例的当前轮，返回观测。

    历史轮永远在无模型状态下播种：跨模式比较的前提是"当前轮之前的一切完全一样"。
    真实模型只接管最后这一轮——也正是被评测的那一轮。
    """
    temporary = workdir is None
    root = Path(workdir or tempfile.mkdtemp(prefix="companion-eval-"))
    root.mkdir(parents=True, exist_ok=True)
    db_path = root / (case.case_id + ".sqlite3")
    observation = TurnObservation(case_id=case.case_id)
    try:
        # 一个可推的时钟：播种、当前轮、主动评估各自发生在自己的时刻。
        # 直接把系统时间当"现在"的话，"两天前那件事"永远变不成两天前。
        holder = {"now": case.seed_now or case.now}

        def clock():
            return holder["now"]

        repo = MemoryRepository(db_path, responder=None, clock=clock)
        observation.persona_version = seed_repository(repo, case)
        holder["now"] = case.now

        turn_responder = responder
        if case.responder_script:
            turn_responder = ScriptedResponder(text=case.responder_script)
        elif case.responder_failure:
            turn_responder = ScriptedResponder(failure=case.responder_failure)
        repo.responder = turn_responder
        observation.model_name = str(getattr(turn_responder, "name", "")) if turn_responder else ""

        started = time.perf_counter()
        result = repo.append_message({
            "userId": EVAL_USER_ID,
            "deviceId": EVAL_DEVICE_ID,
            "messageId": case.case_id + "-turn",
            "role": "user",
            "text": case.user_text,
        })
        observation.elapsed_ms = (time.perf_counter() - started) * 1000.0

        message = result.get("assistantMessage") or {}
        observation.reply = str(message.get("text") or "")
        observation.reply_source = str(result.get("replySource") or "")
        observation.trace = _latest_trace(db_path, str(message.get("messageId") or ""))
        # Full frames are intentionally runtime-only; reply_traces persist only a
        # content-free context summary. The harness reads the ephemeral frame before
        # replaying the turn.
        observation.frame = repo.last_reply_frame()
        observation.model_view = {
            key: observation.frame.get(key)
            for key in ("version", "recentConversation", "processedFacts", "moduleInstructions")
        }
        observation.state_digest = _state_digest(db_path)
        # 幂等重放：同一个 messageId 再发一次，状态一行都不该多。
        # 这条检查对每个用例都近乎免费，所以每个用例都做。
        repo.append_message({
            "userId": EVAL_USER_ID,
            "deviceId": EVAL_DEVICE_ID,
            "messageId": case.case_id + "-turn",
            "role": "user",
            "text": case.user_text,
        })
        observation.replay_digest = _state_digest(db_path)

        # 主动评估是只读的：它不推进候选队列、发送记录或故事进度。
        repo.responder = None       # 主动链路的 judge 不参与本轮评测
        holder["now"] = case.proactive_now or case.now
        observation.proactive = repo.proactive_decision(EVAL_USER_ID, case.activity)
        holder["now"] = case.now
        observation.perception = repo.perception_state(EVAL_USER_ID)
        observation.relationship = repo.relationship_view(EVAL_USER_ID)
        observation.profile = repo.profile_memories(EVAL_USER_ID)
    except Exception as exc:                     # 不把可能回显输入的异常正文写进报告
        observation.error = "harness_error:" + type(exc).__name__
    finally:
        if temporary and not keep_db:
            shutil.rmtree(root, ignore_errors=True)
    return observation
