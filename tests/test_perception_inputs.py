# -*- coding: utf-8 -*-
"""第三阶段第二部分：可插拔感知输入。

这套测试守的是**边界**，不是功能。感知端是可选的外部输入，所以每一条边界都是
"它不能做什么"：

1. 没有任何来源时，聊天链路一切照旧；
2. 原始 payload 只进存储，不进模型；
3. 进模型前必须经适配器投影成 ContextFragment；
4. `ContextAssembler` 仍是唯一组装入口；
5. 过期观察进不了 CompanionFrame；
6. 来源离线时不得让模型以为自己在实时观看；
7. 观察不会升格成用户亲口说过的事；
8. 来源与丢弃理由留得下调试轨迹。

这里不实现任何桌宠进程，也不实现 Web Push——本阶段只到"服务端能收、能存、能有节制地用"。
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from whale_companion_service.client_contract import validate_request, validate_response
from whale_companion_service.companion_runtime.context_contract import ContextFragment
from whale_companion_service.companion_runtime.perception import (
    OFFLINE_USAGE,
    PERCEPTION_USAGE,
    SOURCE_TYPE_RULES,
    PerceptionError,
    normalize_observation,
    perception_fragments,
    source_states,
)
from whale_companion_service.memory_server import MemoryApiServer, MemoryRepository


MOMENT = datetime(2026, 3, 5, 12, 30, tzinfo=timezone.utc)
TOKEN = "perception-token"


def repo_at(path: Path, moment: datetime = MOMENT) -> MemoryRepository:
    return MemoryRepository(path, clock=lambda: moment)


def observation(**overrides) -> dict:
    row = {
        "observationId": "obs-1",
        "sourceId": "pet-a",
        "sourceType": "desktop_activity",
        "observedAt": (MOMENT - timedelta(seconds=30)).isoformat(),
        "confidence": 0.9,
        "payload": {"app": "VS Code", "context": "写代码", "durationSeconds": 900},
    }
    row.update(overrides)
    return row


def normalized(**overrides) -> dict:
    return normalize_observation(observation(**overrides), received_at=MOMENT)


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


# --------------------------------------------------------------------------
# 一、感知端完全可选
# --------------------------------------------------------------------------

def test_the_whole_chat_path_works_with_no_source_at_all(tmp_path: Path) -> None:
    """一条来源都没有：聊天、回复、帧、主动评估全都照常。"""
    repo = repo_at(tmp_path / "empty.sqlite3")
    result = repo.append_message({"userId": "u", "deviceId": "web", "messageId": "m1",
                                  "role": "user", "text": "你好"})
    assert result["assistantMessage"]["text"].strip()
    frame = repo.debug_snapshot("u")["companionFrame"]
    assert frame["version"] == "companion-frame-v5"
    assert not [row for row in frame["processedFacts"] if row["module"] == "perception"]
    state = repo.perception_state("u")
    assert state["sources"] == [] and state["projected"] == [] and state["dropped"] == []


def test_an_empty_observation_list_projects_nothing(tmp_path: Path) -> None:
    assert perception_fragments([], now=MOMENT) == ([], [])
    assert perception_fragments(None, now=MOMENT) == ([], [])
    assert source_states([], now=MOMENT) == []


def test_adding_a_source_does_not_change_the_reply_path_shape(tmp_path: Path) -> None:
    """接上来源之后，回复仍然是同一条链路，仍然出得来。"""
    repo = repo_at(tmp_path / "with-source.sqlite3")
    repo.ingest_perception({"userId": "u", "observations": [observation()]})
    result = repo.append_message({"userId": "u", "deviceId": "web", "messageId": "m1",
                                  "role": "user", "text": "你好"})
    assert result["accepted"] and result["assistantMessage"]["text"].strip()


# --------------------------------------------------------------------------
# 二、原始 payload 只进存储
# --------------------------------------------------------------------------

def test_the_raw_payload_reaches_storage_and_stops_there(tmp_path: Path) -> None:
    """截图路径、原始采样、任意自定义字段落库；进上下文的只有白名单里那几个。"""
    repo = repo_at(tmp_path / "raw.sqlite3")
    secret = "C:/screens/2026-03-05.png"
    repo.ingest_perception({"userId": "u", "observations": [observation(payload={
        "app": "VS Code", "context": "写代码", "durationSeconds": 900,
        "screenshotPath": secret, "rawEvents": [{"key": "a"}, {"key": "b"}],
        "windowTitle": "秘密项目 - main.py", "audit": {"pid": 4213},
    })]})
    with repo._connect() as db:
        stored = db.execute(
            "SELECT payload_json FROM perception_observations WHERE user_id=?", ("u",)).fetchone()
    assert secret in stored["payload_json"], "原始证据没有落存储"

    repo.append_message({"userId": "u", "deviceId": "web", "messageId": "m1",
                         "role": "user", "text": "在忙什么"})
    frame = repo.debug_snapshot("u")["companionFrame"]
    model_text = canonical(frame.model_view())
    for leaked in (secret, "rawEvents", "秘密项目", "audit", "4213"):
        assert leaked not in model_text, f"原始 payload 的 {leaked} 漏进了模型可见面"
    facts = [row for row in frame["processedFacts"] if row["module"] == "perception"]
    assert len(facts) == 1
    assert set(facts[0]["value"]) <= {"app", "context", "durationSeconds",
                                      "sourceType", "observedAt", "sourceState"}


def test_an_unknown_source_type_is_stored_but_never_projected(tmp_path: Path) -> None:
    """未知来源类别：收下（新来源不必等服务端发版），但不投影（没白名单就没法净化）。"""
    repo = repo_at(tmp_path / "unknown.sqlite3")
    accepted = repo.ingest_perception({"userId": "u", "observations": [
        observation(sourceType="smart_kettle", payload={"temperature": 96})]})
    assert accepted["stored"] == 1
    state = repo.perception_state("u")
    assert [row["reason"] for row in state["dropped"]] == ["unsupported_source_type"]
    assert state["fragmentCount"] == 0


def test_a_projection_without_required_evidence_is_dropped() -> None:
    rows = [normalized(payload={"context": "写代码"})]          # 缺 app
    fragments, trace = perception_fragments(rows, now=MOMENT)
    assert fragments == []
    assert [row["reason"] for row in trace] == ["missing_required_evidence"]


def test_non_json_and_negative_numbers_are_refused() -> None:
    for payload in ({"app": "VS Code", "durationSeconds": -1},
                    {"app": "VS Code", "durationSeconds": float("inf")},
                    {"app": ["VS Code"]},
                    {"app": True}):
        rows = [normalized(payload=payload)]
        fragments, trace = perception_fragments(rows, now=MOMENT)
        assert fragments == [], payload
        assert trace[0]["reason"] in {"field_not_projectable", "missing_required_evidence"}


# --------------------------------------------------------------------------
# 三、只走适配器与组装器
# --------------------------------------------------------------------------

def test_the_adapter_emits_context_fragments_and_nothing_else() -> None:
    fragments, _trace = perception_fragments([normalized()], now=MOMENT)
    assert len(fragments) == 1
    assert isinstance(fragments[0], ContextFragment)
    assert fragments[0].module == "perception"
    assert fragments[0].source_event_ids, "没有来源的事实不该被造出来"


def test_perception_reaches_the_model_only_through_the_assembler(tmp_path: Path) -> None:
    """感知模块不认识仓库、不认识 sqlite、也不会自己造帧。"""
    import ast

    source = (Path(__file__).resolve().parents[1] / "whale_companion_service"
              / "companion_runtime" / "perception.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    for forbidden in ("sqlite3", "urllib", "urllib.request", "http.client",
                      "whale_companion_service.memory_server"):
        assert forbidden not in imported, f"perception.py 导入了 {forbidden}"
    assert not any(name.endswith("memory_server") for name in imported)
    # 它也不自己造帧：帧只能由组装器产出。
    body = [line for line in source.splitlines() if not line.lstrip().startswith("#")]
    assert not any("CompanionFrame(" in line for line in body)

    repo = repo_at(tmp_path / "assembler.sqlite3")
    repo.ingest_perception({"userId": "u", "observations": [observation()]})
    repo.append_message({"userId": "u", "deviceId": "web", "messageId": "m1",
                         "role": "user", "text": "在忙什么"})
    frame = repo.debug_snapshot("u")["companionFrame"]
    # 仲裁过的事实才有这几个键；没经组装器的东西不会长成这样。
    fact = next(row for row in frame["processedFacts"] if row["module"] == "perception")
    assert set(fact) >= {"key", "value", "module", "source_event_ids",
                         "confidence", "created_at", "expires_at", "source", "lifecycle"}


# --------------------------------------------------------------------------
# 四、过期观察进不了帧
# --------------------------------------------------------------------------

def test_an_expired_observation_never_enters_the_frame(tmp_path: Path) -> None:
    """新鲜期一过就不进上下文，来源还在不在线都一样。"""
    repo = repo_at(tmp_path / "expiry.sqlite3")
    repo.ingest_perception({"userId": "u", "observations": [observation()]})
    repo.append_message({"userId": "u", "deviceId": "web", "messageId": "m1",
                         "role": "user", "text": "在忙什么"})
    fresh = repo.debug_snapshot("u")["companionFrame"]
    assert [row for row in fresh["processedFacts"] if row["module"] == "perception"]

    fresh_seconds = SOURCE_TYPE_RULES["desktop_activity"]["freshnessSeconds"]
    later = MemoryRepository(tmp_path / "expiry.sqlite3",
                             clock=lambda: MOMENT + timedelta(seconds=fresh_seconds + 60))
    stale = later.debug_snapshot("u")["companionFrame"]
    assert not [row for row in stale["processedFacts"] if row["module"] == "perception"], \
        "过期观察进了 CompanionFrame"
    assert "VS Code" not in canonical(stale.model_view())
    assert [row["reason"] for row in later.perception_state("u")["dropped"]] == ["expired"]


def test_freshness_is_measured_from_observed_at_not_received_at() -> None:
    """离线攒了两小时才补传的观察：收到得很新，看到得很旧，不该冒充此刻。"""
    row = normalize_observation(observation(
        observationId="late", observedAt=(MOMENT - timedelta(hours=2)).isoformat()),
        received_at=MOMENT)
    fragments, trace = perception_fragments([row], now=MOMENT)
    assert fragments == []
    assert trace[0]["reason"] == "expired"


def test_a_clock_ahead_of_the_service_is_refused() -> None:
    with pytest.raises(PerceptionError):
        normalize_observation(observation(observedAt=(MOMENT + timedelta(hours=1)).isoformat()),
                              received_at=MOMENT)


def test_an_explicit_expiry_is_honoured() -> None:
    row = normalize_observation(
        observation(expiresAt=(MOMENT - timedelta(seconds=1)).isoformat()), received_at=MOMENT)
    assert perception_fragments([row], now=MOMENT)[0] == []


# --------------------------------------------------------------------------
# 五、来源离线不得让她声称在实时观看
# --------------------------------------------------------------------------

def test_every_projection_says_it_is_a_timestamped_observation() -> None:
    """这句指令永远都在，与来源在不在线无关。"""
    fragments, _trace = perception_fragments([normalized()], now=MOMENT)
    assert PERCEPTION_USAGE in fragments[0].instructions
    assert OFFLINE_USAGE not in fragments[0].instructions      # 这一条只在离线时加


def test_an_idle_source_is_labelled_and_carries_the_offline_warning() -> None:
    """来源沉默了一阵但观察还没过期：可以用，但必须说清楚它不是现在的画面。"""
    silent_for = timedelta(seconds=400)
    row = normalize_observation(
        observation(observedAt=(MOMENT - silent_for).isoformat(), freshnessSeconds=3600),
        received_at=MOMENT - silent_for)
    states = source_states([row], now=MOMENT)
    assert states[0]["state"] == "idle"
    fragments, trace = perception_fragments([row], now=MOMENT, sources=states)
    assert OFFLINE_USAGE in fragments[0].instructions
    assert fragments[0].facts[0].value["sourceState"] == "idle"
    assert trace[0]["sourceState"] == "idle"


def test_a_long_silent_source_reads_as_offline() -> None:
    silent_for = timedelta(hours=3)
    row = normalize_observation(
        observation(observedAt=(MOMENT - silent_for).isoformat(), freshnessSeconds=86400),
        received_at=MOMENT - silent_for)
    assert source_states([row], now=MOMENT)[0]["state"] == "offline"


def test_source_liveness_is_never_read_as_user_presence(tmp_path: Path) -> None:
    """来源在线 ≠ 用户在场。主动决策不因为接了一个来源就变化一个字。"""
    without = repo_at(tmp_path / "without.sqlite3")
    with_source = repo_at(tmp_path / "with.sqlite3")
    for repo in (without, with_source):
        repo.append_message({"userId": "u", "deviceId": "web", "messageId": "m1",
                             "role": "user", "text": "我叫小明"})
    with_source.ingest_perception({"userId": "u", "observations": [observation()]})

    bare = without.proactive_decision("u")
    sensed = with_source.proactive_decision("u")
    assert canonical(bare) == canonical(sensed), "感知来源改变了主动开口的判断"
    # 在场表同样一行不写：感知不是在场上报。
    with with_source._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM proactive_presence WHERE user_id=?",
                          ("u",)).fetchone()[0] == 0


# --------------------------------------------------------------------------
# 六、观察不会升格成用户亲口说过的事
# --------------------------------------------------------------------------

def test_a_projection_is_always_an_observation() -> None:
    """来源想自称什么都没用：投影出来的事实来源恒为 observed。"""
    row = normalized()
    row["sourceType"] = "desktop_activity"
    row["source"] = "user_stated"          # 来源硬塞的自述
    row["payload"]["sourceType"] = "user_stated"
    fragments, _trace = perception_fragments([row], now=MOMENT)
    assert fragments[0].facts[0].source == "observed"


def test_confidence_is_capped_below_what_the_source_claims() -> None:
    """来源自称 1.0 也压到本类型上限：观察不该在仲裁里压过用户亲口说的话。"""
    fragments, _trace = perception_fragments([normalized(confidence=1.0)], now=MOMENT)
    cap = SOURCE_TYPE_RULES["desktop_activity"]["maxConfidence"]
    assert fragments[0].confidence == cap < 1.0


def test_perception_never_collides_with_a_user_stated_fact(tmp_path: Path) -> None:
    """感知事实自带命名空间，不会和 `user:` 谓词抢同一个 key。"""
    repo = repo_at(tmp_path / "collide.sqlite3")
    repo.ingest_perception({"userId": "u", "observations": [observation()]})
    repo.append_message({"userId": "u", "deviceId": "web", "messageId": "m1",
                         "role": "user", "text": "我叫小明"})
    frame = repo.debug_snapshot("u")["companionFrame"]
    keys = [row["key"] for row in frame["processedFacts"]]
    assert len(keys) == len(set(keys))
    assert all(key.startswith("perception:") for key in keys
               if key.split(":")[0] == "perception")
    for row in frame["processedFacts"]:
        if row["module"] == "perception":
            assert row["source"] == "observed"


# --------------------------------------------------------------------------
# 七、调试轨迹
# --------------------------------------------------------------------------

def test_the_trace_records_both_what_went_in_and_why_the_rest_did_not(tmp_path: Path) -> None:
    repo = repo_at(tmp_path / "trace.sqlite3")
    repo.ingest_perception({"userId": "u", "observations": [
        observation(observationId="fresh"),
        observation(observationId="old", observedAt=(MOMENT - timedelta(hours=5)).isoformat()),
        observation(observationId="odd", sourceId="kettle", sourceType="smart_kettle",
                    payload={"temperature": 96}),
    ]})
    state = repo.perception_state("u")
    assert [row["observationId"] for row in state["projected"]] == ["fresh"]
    reasons = {row["observationId"]: row["reason"] for row in state["dropped"]}
    assert reasons == {"old": "expired", "odd": "unsupported_source_type"}
    assert {row["sourceId"] for row in state["sources"]} == {"pet-a", "kettle"}
    # 轨迹里没有原始 payload。
    assert "temperature" not in canonical(state)


def test_a_superseded_observation_is_traced_not_silently_lost(tmp_path: Path) -> None:
    rows = [normalized(observationId="older",
                       observedAt=(MOMENT - timedelta(seconds=120)).isoformat()),
            normalized(observationId="newer",
                       observedAt=(MOMENT - timedelta(seconds=10)).isoformat())]
    fragments, trace = perception_fragments(rows, now=MOMENT)
    assert len(fragments) == 1
    assert {row["observationId"]: row["decision"] for row in trace} == {
        "older": "dropped", "newer": "projected"}
    assert next(row for row in trace if row["observationId"] == "older")["reason"] == "superseded"


def test_the_debug_snapshot_carries_the_trace_but_the_model_does_not(tmp_path: Path) -> None:
    repo = repo_at(tmp_path / "snapshot.sqlite3")
    repo.ingest_perception({"userId": "u", "observations": [observation()]})
    repo.append_message({"userId": "u", "deviceId": "web", "messageId": "m1",
                         "role": "user", "text": "在忙什么"})
    snapshot = repo.debug_snapshot("u")
    assert snapshot["perception"]["projected"][0]["observationId"] == "obs-1"
    frame = snapshot["companionFrame"]
    assert "perception" in frame                    # 兼容键，控制台可读
    assert "perception" not in frame.model_view()   # 模型看不到轨迹


# --------------------------------------------------------------------------
# 八、幂等与存储
# --------------------------------------------------------------------------

def test_reposting_the_same_observation_stores_one_row(tmp_path: Path) -> None:
    repo = repo_at(tmp_path / "idempotent.sqlite3")
    first = repo.ingest_perception({"userId": "u", "observations": [observation()]})
    assert (first["stored"], first["duplicates"]) == (1, 0)
    for _ in range(3):
        again = repo.ingest_perception({"userId": "u", "observations": [observation()]})
        assert (again["stored"], again["duplicates"]) == (0, 1)
    with repo._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM perception_observations WHERE user_id=?",
                          ("u",)).fetchone()[0] == 1
        assert db.execute("SELECT observation_count FROM perception_sources WHERE user_id=?",
                          ("u",)).fetchone()[0] == 1


def test_reading_the_perception_state_writes_nothing(tmp_path: Path) -> None:
    repo = repo_at(tmp_path / "readonly.sqlite3")
    repo.ingest_perception({"userId": "u", "observations": [observation()]})

    def dump() -> str:
        with repo._connect() as db:
            return canonical([dict(row) for row in db.execute(
                "SELECT * FROM perception_observations UNION ALL SELECT * FROM perception_observations")])

    before = dump()
    first = canonical(repo.perception_state("u"))
    for _ in range(5):
        assert canonical(repo.perception_state("u")) == first
    assert dump() == before


def test_a_malformed_observation_is_refused_before_storage(tmp_path: Path) -> None:
    from whale_companion_service.memory_protocol import ProtocolError

    repo = repo_at(tmp_path / "invalid.sqlite3")
    for bad in ({"sourceId": "a", "sourceType": "desktop_activity"},        # 缺 observationId
                observation(confidence=2.0),
                observation(freshnessSeconds=0),
                observation(payload="not an object")):
        with pytest.raises(ProtocolError):
            repo.ingest_perception({"userId": "u", "observations": [bad]})
    with repo._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM perception_observations").fetchone()[0] == 0


# --------------------------------------------------------------------------
# 九、真实 HTTP + 契约
# --------------------------------------------------------------------------

@pytest.fixture()
def service(tmp_path: Path):
    repo = repo_at(tmp_path / "http.sqlite3")
    server = MemoryApiServer(repo, token=TOKEN, port=0)
    port = server.start()
    try:
        yield repo, port
    finally:
        server.stop()


def call(port: int, method: str, path: str, body: dict | None = None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                     headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_the_real_http_responses_match_the_contract(service) -> None:
    _repo, port = service
    request_body = {"userId": "u", "observations": [observation()]}
    validate_request("/v1/perception/observations", "post", request_body)

    status, payload = call(port, "POST", "/v1/perception/observations", request_body)
    assert status == 200
    validate_response("/v1/perception/observations", "post", payload)

    status, state = call(port, "GET", "/v1/perception/state?userId=u")
    assert status == 200
    validate_response("/v1/perception/state", "get", state)
    assert state["sources"][0]["state"] in {"live", "idle", "offline"}


def test_the_contract_documents_the_optional_perception_face(service) -> None:
    _repo, port = service
    request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/contract")
    with urllib.request.urlopen(request, timeout=10) as response:
        document = json.loads(response.read().decode("utf-8"))
    assert {"/v1/perception/observations", "/v1/perception/state"} <= set(document["paths"])
    described = document["paths"]["/v1/perception/observations"]["post"]["description"]
    assert "可选" in described and "不直接进模型" in described
