"""Cross-repository contract test for the sibling desktop checkout."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from whale_companion_service.memory_protocol import PROTOCOL_VERSION, validate_batch
from whale_companion_service.memory_server import MemoryApiServer, MemoryRepository


DESKTOP_ROOT = Path(__file__).resolve().parents[2] / "dsh-pet-indesktop"
if not DESKTOP_ROOT.is_dir():
    pytest.skip("sibling desktop repository is not available", allow_module_level=True)
if str(DESKTOP_ROOT) not in sys.path:
    sys.path.insert(0, str(DESKTOP_ROOT))

from pet.memory_protocol import (  # noqa: E402
    PROTOCOL_VERSION as DESKTOP_PROTOCOL_VERSION,
    MemorySyncClient,
    make_batch,
)


def test_desktop_client_and_service_share_memory_v1_contract(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    memory = {
        "id": "contract-fact-1",
        "revision": 1,
        "layer": "L1",
        "sourceType": "observed",
        "kind": "activity",
        "app": "Visual Studio Code",
        "context": "work",
        "durationSeconds": 3600,
        "startedAt": (now - timedelta(hours=1)).isoformat(),
        "endedAt": now.isoformat(),
    }
    batch = make_batch(
        user_id="contract-user",
        device_id="desktop-contract",
        report={"batchId": "contract-batch-1", "latestRevision": 1},
        memories=[memory],
    )

    assert DESKTOP_PROTOCOL_VERSION == PROTOCOL_VERSION == 1
    assert validate_batch(batch) == batch

    server = MemoryApiServer(
        MemoryRepository(tmp_path / "contract.sqlite3"),
        token="contract-secret",
        port=0,
    )
    port = server.start()
    try:
        client = MemorySyncClient(f"http://127.0.0.1:{port}", "contract-secret")
        ack = client.post_batch(batch)
        assert ack["batchId"] == "contract-batch-1"
        assert server.repository.stream("contract-user")["memories"][0]["id"] == "contract-fact-1"
    finally:
        server.stop()
