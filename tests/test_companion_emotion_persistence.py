from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from whale_companion_service.memory_server import MemoryRepository


def _batch() -> dict:
    now = datetime.now(timezone.utc)
    fact = {
        "id": "fact-1",
        "revision": 1,
        "layer": "L1",
        "sourceType": "observed",
        "kind": "activity",
        "app": "Visual Studio Code",
        "context": "work",
        "durationSeconds": 3600,
        "startedAt": (now - timedelta(hours=1)).isoformat(),
        "endedAt": now.isoformat(),
        "project": {"name": "小鲸"},
    }
    return {
        "protocolVersion": 1,
        "userId": "user-1",
        "deviceId": "desktop-1",
        "batchId": "batch-1",
        "latestRevision": fact["revision"],
        "memories": [fact],
    }


def test_reply_persists_companion_emotion(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "emotion.sqlite3")
    repository.ingest_batch(_batch())
    assert repository.companion_emotion("user-1")["mood"] == ""

    repository.append_message({
        "userId": "user-1",
        "deviceId": "phone-1",
        "messageId": "m-1",
        "role": "user",
        "text": "谢谢你，有你在真好",
    })

    state = repository.companion_emotion("user-1")
    assert state["mood"] == "被你这句话暖到了"
    assert state["valence"] > 0.3
    assert state["lastEvent"] == "warmed"


def test_proactive_decision_empty_is_quiet(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "proactive.sqlite3")
    result = repository.proactive_decision("user-1", "idle")
    assert result["shouldSpeak"] is False
    assert result["vetoReason"] == "nothing_grounded"


def test_proactive_decision_returns_valid_structure(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "proactive-structure.sqlite3")
    repository.ingest_batch(_batch())
    result = repository.proactive_decision("user-1", "idle")
    assert "shouldSpeak" in result
    assert "checkedAt" in result
    if result["shouldSpeak"]:
        assert result["kind"] in {"follow_up", "check_in", "observation"}
        assert result["content"]
    else:
        assert result["vetoReason"] in {
            "user_still_chatting", "too_soon", "quiet_hours", "nothing_grounded",
        }
