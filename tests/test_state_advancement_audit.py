# -*- coding: utf-8 -*-
"""第三阶段第一部分：状态推进语义审计。

这里锁的**不是**"GET 不许写库"。本项目允许惰性初始化（当日基线）和基于时间的状态推进
（故事走格子），那些是陪伴该有的行为。锁的是比它更强、也更有意义的一条：

    同一输入、同一固定时刻、同一已有状态下，重复调用结果一致，
    且不重复累计任何副作用。

"副作用"具体指五样可数的东西：事件、账本记录、故事进度、主动投递、对话消息。
它们各有一节测试。时钟一律注入并钉死——时间不走，答案就没有任何理由变。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from whale_companion_service.memory_server import MemoryApiServer, MemoryRepository


MOMENT = datetime(2026, 3, 5, 12, 30, tzinfo=timezone.utc)


def repo_at(path: Path, moment: datetime = MOMENT) -> MemoryRepository:
    return MemoryRepository(path, clock=lambda: moment)


def seeded(path: Path, moment: datetime = MOMENT) -> MemoryRepository:
    """一个有真实历史的仓库：说过话、留下情绪、开了线程、涨过好感度。"""
    repo = repo_at(path, moment)
    repo.append_message({"userId": "u", "deviceId": "web", "messageId": "m1",
                         "role": "user", "text": "我叫小明，今天有点难过"})
    repo.append_message({"userId": "u", "deviceId": "web", "messageId": "m2",
                         "role": "user", "text": "还是那件事，一直没过去"})
    return repo


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def dump(repo: MemoryRepository) -> dict:
    """整库快照。比只看返回值严格：状态有没有被悄悄推一格，这里看得见。"""
    snapshot = {}
    with repo._connect() as db:
        tables = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for table in tables:
            snapshot[table] = sorted(
                canonical(dict(row)) for row in db.execute(f"SELECT * FROM {table}"))
    return snapshot


def counts(repo: MemoryRepository) -> dict:
    """五样可数的副作用。它们才是"重复累计"真正的度量。"""
    with repo._connect() as db:
        def one(sql: str) -> int:
            return int(db.execute(sql, ("u",)).fetchone()[0])
        return {
            "memory_events": one("SELECT COUNT(*) FROM memory_events WHERE user_id=?"),
            "memory_mentions": one("SELECT COUNT(*) FROM memory_mentions WHERE user_id=?"),
            "lifecycle_events": one("SELECT COUNT(*) FROM memory_lifecycle_events WHERE user_id=?"),
            "relationship_ledger": one("SELECT COUNT(*) FROM relationship_ledger WHERE user_id=?"),
            "self_timeline": one("SELECT COUNT(*) FROM self_timeline_events WHERE user_id=?"),
            "messages": one("SELECT COUNT(*) FROM conversation_messages WHERE user_id=?"),
            "proactive_sent": one(
                "SELECT COUNT(*) FROM proactive_candidates WHERE user_id=? AND status='sent'"),
            "story_beats": one(
                "SELECT COALESCE(SUM(LENGTH(arc_json)),0) FROM story_arcs WHERE user_id=?"),
        }


# 审计表里列出的每一个由 GET / 查询方法 / 上下文构建触发的入口。
# 三类语义都在，因为本套测试要证明的正是"三类都能满足同一条不变式"：
#   纯读取 · 惰性初始化 · 基于时间的状态推进
READ_ENTRIES = {
    "daily_state": lambda repo: repo.daily_state("u"),                 # 惰性初始化
    "story_state": lambda repo: repo.story_state("u"),                 # 时间推进 + 落库
    "proactive_decision": lambda repo: repo.proactive_decision("u"),   # 只读评估
    "affinity_state": lambda repo: repo.affinity_state("u"),
    "relationship_view": lambda repo: repo.relationship_view("u"),
    "companion_emotion": lambda repo: repo.companion_emotion("u"),
    "self_timeline": lambda repo: repo.self_timeline_events("u"),
    "expressions": lambda repo: repo.expression_rules("u"),
    "living_config": lambda repo: repo.living_config("u"),
    "persona_card": lambda repo: repo.persona_card("u"),
    "profile_memories": lambda repo: repo.profile_memories("u"),
    "memory_digests": lambda repo: repo.memory_digests("u"),
    "stream": lambda repo: repo.stream("u"),
    "messages": lambda repo: repo.messages("u"),
    "perception_state": lambda repo: repo.perception_state("u"),
    "debug_snapshot": lambda repo: repo.debug_snapshot("u"),           # CompanionFrame 构建
    "preview_opening": lambda repo: repo.preview_opening("u"),
    "health_status": lambda repo: repo.health_status(),
}


# --------------------------------------------------------------------------
# 一、固定时钟重复调用
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(READ_ENTRIES))
def test_a_frozen_clock_makes_every_read_entry_repeatable(tmp_path: Path, name: str) -> None:
    """钉死时钟连读五次：答案逐字节相同，库在第一次之后不再变。

    第一次允许写（惰性初始化、时间推进都在这一刻落定），第二次起必须一动不动。
    """
    repo = seeded(tmp_path / f"{name}.sqlite3")
    read = READ_ENTRIES[name]

    first = canonical(read(repo))
    settled = dump(repo)
    for attempt in range(2, 6):
        assert canonical(read(repo)) == first, f"{name} 第 {attempt} 次读取与第 1 次不同"
        assert dump(repo) == settled, f"{name} 第 {attempt} 次读取改变了库"


def test_reading_everything_in_any_order_lands_on_one_state(tmp_path: Path) -> None:
    """把全部入口按正序和逆序各读一遍：终局状态必须一样。

    读取顺序会改变结果的话，"谁先打开控制台"就成了状态的一部分。
    """
    forward = seeded(tmp_path / "forward.sqlite3")
    for name in sorted(READ_ENTRIES):
        READ_ENTRIES[name](forward)
    backward = seeded(tmp_path / "backward.sqlite3")
    for name in sorted(READ_ENTRIES, reverse=True):
        READ_ENTRIES[name](backward)

    ignore = {"conversation_messages", "memory_events", "reply_traces"}   # 含消息 id 与轨迹
    forward_state = {k: v for k, v in dump(forward).items() if k not in ignore}
    backward_state = {k: v for k, v in dump(backward).items() if k not in ignore}
    assert forward_state == backward_state


# --------------------------------------------------------------------------
# 二、服务重启一致性
# --------------------------------------------------------------------------

def without_process_local(value):
    """去掉进程内的观测字段。

    `lastReplySource` / `lastReplyAt` 记的是"这个进程上一次回复发生在什么时候"，
    本来就随进程生灭，不是持久状态。除它们之外，重启前后必须逐字节相同。
    """
    if isinstance(value, dict):
        return {key: without_process_local(item) for key, item in value.items()
                if key not in {"lastReplySource", "lastReplyAt"}}
    if isinstance(value, list):
        return [without_process_local(item) for item in value]
    return value


@pytest.mark.parametrize("name", sorted(READ_ENTRIES))
def test_a_restart_does_not_change_what_a_read_returns(tmp_path: Path, name: str) -> None:
    """同一个库、同一时刻，换一个仓库实例（等价于重启服务）读出来必须一样。

    `_initialize` 每次启动都会跑一遍补齐与自愈，所以"重启"是真的会执行迁移逻辑的路径。
    """
    path = tmp_path / f"restart-{name}.sqlite3"
    before_repo = seeded(path)
    before = canonical(without_process_local(READ_ENTRIES[name](before_repo)))
    settled = dump(before_repo)

    after_repo = MemoryRepository(path, clock=lambda: MOMENT)
    after = canonical(without_process_local(READ_ENTRIES[name](after_repo)))
    assert after == before, f"重启后 {name} 的答案变了"
    assert dump(after_repo) == settled, f"重启本身改变了 {name} 涉及的状态"


def test_a_restart_does_not_hand_the_phone_a_new_opening(tmp_path: Path) -> None:
    """回归：重启不该把聊天里的情绪变成一条可领取的开场。

    每日单元由 `POST /v1/memory/batches` 建；`append_message` 提取出的 L3 情绪不属于
    任何单元。但启动时的兼容补齐曾经对**所有**记忆一视同仁，于是重启一次，
    手机就能领到一条由刚才那句聊天生成的开场——服务不重启则永远不会发。
    """
    path = tmp_path / "restart-opening.sqlite3"
    repo = seeded(path)
    assert repo.claim_opening(
        {"userId": "u", "deviceId": "phone", "claimId": "before"})["shouldSend"] is False

    restarted = MemoryRepository(path, clock=lambda: MOMENT)
    after = restarted.claim_opening({"userId": "u", "deviceId": "phone", "claimId": "after"})
    assert after["shouldSend"] is False, f"重启凭空造出了一条开场：{after}"
    with restarted._connect() as db:
        assert int(db.execute(
            "SELECT COUNT(*) FROM daily_episodes WHERE user_id=?", ("u",)).fetchone()[0]) == 0


def test_restarting_twice_does_not_accumulate_anything(tmp_path: Path) -> None:
    """启动三次，五样可数的副作用一个都不许涨。"""
    path = tmp_path / "restarts.sqlite3"
    repo = seeded(path)
    repo.story_state("u")
    repo.daily_state("u")
    baseline = counts(repo)
    for _ in range(3):
        again = MemoryRepository(path, clock=lambda: MOMENT)
        again.story_state("u")
        again.daily_state("u")
        assert counts(again) == baseline


# --------------------------------------------------------------------------
# 三、多客户端交错读取
# --------------------------------------------------------------------------

def test_two_clients_interleaving_reads_see_one_answer(tmp_path: Path) -> None:
    """网页和手机交替读同一批接口，两边拿到的必须逐字节相同。

    这正是控制台和 PWA 同时开着时的真实形状：谁先读一步，不该改变另一个人看到的世界。
    """
    repo = seeded(tmp_path / "interleaved.sqlite3")
    names = sorted(READ_ENTRIES)
    web, phone = {}, {}
    for name in names:                      # 交错：web 读一个，phone 立刻读同一个
        web[name] = canonical(READ_ENTRIES[name](repo))
        phone[name] = canonical(READ_ENTRIES[name](repo))
    for name in names:
        if name == "health_status":
            continue
        assert web[name] == phone[name], f"两个客户端读到的 {name} 不一致"


def test_interleaved_reads_do_not_split_the_shared_history(tmp_path: Path) -> None:
    """交错读取之后，共享历史仍然只有一份，游标仍然是同一条。"""
    repo = seeded(tmp_path / "history.sqlite3")
    first = repo.messages("u")
    for name in sorted(READ_ENTRIES):
        READ_ENTRIES[name](repo)
    assert canonical(repo.messages("u")) == canonical(first)
    assert repo.messages("u", first["nextMessageSeq"])["messages"] == []


# --------------------------------------------------------------------------
# 四、并发
# --------------------------------------------------------------------------

def test_concurrent_readers_do_not_advance_state_twice(tmp_path: Path) -> None:
    """八个线程同时读三条会写库的路径：答案唯一，副作用不翻倍。

    故事推进和当日基线都在事务里落库，并发是它们唯一可能被推两格的地方。
    """
    repo = seeded(tmp_path / "concurrent.sqlite3")
    repo.story_state("u")                 # 先让第一次落定，之后的并发只该读到同一份
    repo.daily_state("u")
    baseline = counts(repo)
    settled = dump(repo)

    results: list[tuple[str, str]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def hammer() -> None:
        try:
            for reader in ("story_state", "daily_state", "proactive_decision"):
                value = canonical(READ_ENTRIES[reader](repo))
                with lock:
                    results.append((reader, value))
        except BaseException as exc:       # noqa: BLE001 - 线程里的失败必须能被看见
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert not errors, f"并发读取抛了异常：{errors[:3]}"
    for reader in ("story_state", "daily_state", "proactive_decision"):
        answers = {value for name, value in results if name == reader}
        assert len(answers) == 1, f"{reader} 在并发下给出了 {len(answers)} 种答案"
    assert counts(repo) == baseline
    assert dump(repo) == settled


def test_concurrent_deliveries_with_one_id_send_one_message(tmp_path: Path) -> None:
    """六个线程拿同一个 deliveryId 同时落定：最多一条主动消息。"""
    repo = seeded(tmp_path / "delivery-race.sqlite3")
    payloads: list[dict] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def deliver() -> None:
        try:
            result = repo.commit_proactive_decision(
                {"userId": "u", "deliveryId": "same-delivery", "deviceId": "web"})
            with lock:
                payloads.append(result)
        except BaseException as exc:       # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=deliver) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert not errors, f"并发投递抛了异常：{errors[:3]}"
    with repo._connect() as db:
        sent = int(db.execute(
            "SELECT COUNT(*) FROM conversation_messages WHERE user_id=? AND device_id='proactive'",
            ("u",)).fetchone()[0])
    assert sent <= 1, f"同一个 deliveryId 写出了 {sent} 条主动消息"
    assert len({canonical(row) for row in payloads}) == 1, "同一个 deliveryId 拿到了不同的响应"


# --------------------------------------------------------------------------
# 五、五样副作用一件都不许重复
# --------------------------------------------------------------------------

def test_replaying_one_message_does_not_double_any_side_effect(tmp_path: Path) -> None:
    """同一条用户消息重放五次：事件、账本、提及、时间线一件都不许多。"""
    repo = repo_at(tmp_path / "replay.sqlite3")
    body = {"userId": "u", "deviceId": "web", "messageId": "same",
            "role": "user", "text": "我叫小明，今天很难过"}
    first = repo.append_message(body)
    baseline = counts(repo)
    for _ in range(4):
        again = repo.append_message(body)
        assert again["duplicate"] is True
        assert again["assistantMessage"] == first["assistantMessage"]
        assert counts(repo) == baseline


def test_polling_the_readonly_proactive_endpoint_never_delivers(tmp_path: Path) -> None:
    """只读评估轮询 20 次：不写候选队列、不写发送记录、不多一条消息。"""
    repo = seeded(tmp_path / "poll.sqlite3")
    repo.proactive_decision("u")
    baseline = counts(repo)
    settled = dump(repo)
    for _ in range(20):
        repo.proactive_decision("u")
    assert counts(repo) == baseline
    assert dump(repo) == settled


def test_replaying_a_delivery_does_not_advance_the_story_again(tmp_path: Path) -> None:
    """同一个 deliveryId 重放：故事、账本、发送记录都停在原地。"""
    repo = seeded(tmp_path / "delivery.sqlite3")
    first = repo.commit_proactive_decision(
        {"userId": "u", "deliveryId": "d1", "deviceId": "web"})
    baseline = counts(repo)
    settled = dump(repo)
    for _ in range(4):
        again = repo.commit_proactive_decision(
            {"userId": "u", "deliveryId": "d1", "deviceId": "web"})
        assert canonical(again) == canonical(first)
        assert counts(repo) == baseline
        assert dump(repo) == settled


def test_reading_the_story_repeatedly_does_not_walk_a_beat(tmp_path: Path) -> None:
    """故事按时间推进是允许的；按"被读了几次"推进不是。"""
    repo = seeded(tmp_path / "story.sqlite3")
    first = repo.story_state("u")
    for _ in range(10):
        assert canonical(repo.story_state("u")) == canonical(first)
    beats = {arc["id"]: arc["completed_beats"] for arc in first["arcs"]}

    # 时间真的走了，故事才走。这是允许的那一类推进。
    later = MemoryRepository(tmp_path / "story.sqlite3",
                             clock=lambda: MOMENT + timedelta(days=30))
    moved = later.story_state("u")
    assert {arc["id"] for arc in moved["arcs"]} == set(beats)
    assert any(arc["completed_beats"] != beats[arc["id"]] for arc in moved["arcs"]), \
        "时间过了一个月，故事一格都没走"
    # 但在新的时刻上，它同样是幂等的。
    assert canonical(later.story_state("u")) == canonical(moved)


def test_the_arc_order_is_stable_between_a_first_and_a_later_read(tmp_path: Path) -> None:
    """回归：起线那一次的顺序和从库里读回来的顺序必须一致。

    曾经不一致——刚起线的 arcs 是按模板顺序追加的，读回来是按 arc_id 排的，
    于是同一时刻先读一次和后读一次会拿到两份顺序不同的答案，状态明明没变。
    """
    repo = seeded(tmp_path / "order.sqlite3")
    first = [arc["id"] for arc in repo.story_state("u")["arcs"]]
    second = [arc["id"] for arc in repo.story_state("u")["arcs"]]
    restarted = MemoryRepository(tmp_path / "order.sqlite3", clock=lambda: MOMENT)
    third = [arc["id"] for arc in restarted.story_state("u")["arcs"]]
    assert first == second == third == sorted(first)


def test_a_delivery_receipt_can_never_be_read_back_as_an_opening_claim(tmp_path: Path) -> None:
    """回归：`deliveryId` 和 `claimId` 作用域不同，不能共用一个命名空间。

    投递回执曾经和开场领取共用一张表，靠写死的 device_id='proactive' 区分。
    于是一个把 deviceId 报成 "proactive" 的客户端，拿某个 deliveryId 当 claimId
    就能从开场接口拿回一份投递响应——形状还对不上 OpeningClaim 契约。
    """
    repo = seeded(tmp_path / "namespace.sqlite3")
    delivery = repo.commit_proactive_decision(
        {"userId": "u", "deliveryId": "shared-id", "deviceId": "web"})
    assert "committedAt" in delivery

    claim = repo.claim_opening(
        {"userId": "u", "deviceId": "proactive", "claimId": "shared-id"})
    assert "shouldSend" in claim, "开场领取返回了不是 OpeningClaim 的东西"
    assert not (set(claim) & {"committedAt", "deliveryId", "candidates"}), \
        f"投递响应的字段漏进了开场领取：{sorted(claim)}"


def test_an_old_database_keeps_its_delivery_replay_guarantee(tmp_path: Path) -> None:
    """老库里的投递回执写在 opening_claims 上。升级后重放仍然不能多发一条。"""
    path = tmp_path / "legacy.sqlite3"
    repo = seeded(path)
    original = repo.commit_proactive_decision(
        {"userId": "u", "deliveryId": "legacy-d", "deviceId": "web"})
    # 把它搬回老位置，模拟一个升级前写下的回执。
    with repo._connect() as db:
        row = db.execute(
            "SELECT response_json, created_at FROM proactive_deliveries WHERE user_id=? AND delivery_id=?",
            ("u", "legacy-d")).fetchone()
        db.execute("DELETE FROM proactive_deliveries WHERE user_id=?", ("u",))
        db.execute(
            """INSERT INTO opening_claims (user_id, device_id, claim_id, response_json, created_at)
               VALUES (?, 'proactive', ?, ?, ?)""",
            ("u", "legacy-d", row["response_json"], row["created_at"]))

    upgraded = MemoryRepository(path, clock=lambda: MOMENT)   # 启动时把老回执搬过来
    replayed = upgraded.commit_proactive_decision(
        {"userId": "u", "deliveryId": "legacy-d", "deviceId": "web"})
    assert canonical(replayed) == canonical(original)
    with upgraded._connect() as db:
        assert int(db.execute(
            "SELECT COUNT(*) FROM opening_claims WHERE user_id=? AND device_id='proactive'",
            ("u",)).fetchone()[0]) == 0


def test_a_claim_is_consumed_once_even_across_devices(tmp_path: Path) -> None:
    """两台设备各领一次同一份开场：只有一台拿到，另一台被告知没有新的。"""
    repo = repo_at(tmp_path / "claim.sqlite3")
    repo.ingest_batch({
        "protocolVersion": 1,
        "userId": "u", "deviceId": "pet", "batchId": "b1", "latestRevision": 1,
        "memories": [{"id": "mem1", "revision": 1, "layer": "L3", "kind": "stated_emotion",
                      "sourceType": "user_stated", "label": "sad", "quote": "今天很难过",
                      "occurredAt": MOMENT.isoformat()}],
    })
    first = repo.claim_opening({"userId": "u", "deviceId": "phone", "claimId": "c1"})
    second = repo.claim_opening({"userId": "u", "deviceId": "tablet", "claimId": "c2"})
    assert first["shouldSend"] != second["shouldSend"] or not first["shouldSend"]
    assert not (first["shouldSend"] and second["shouldSend"]), "同一份开场被领了两次"
    # 重放各自的 claimId 也不产生第二条消息。
    assert repo.claim_opening(
        {"userId": "u", "deviceId": "phone", "claimId": "c1"})["duplicateClaim"] is True
    with repo._connect() as db:
        openings = int(db.execute(
            "SELECT COUNT(*) FROM conversation_messages WHERE user_id=? AND message_id LIKE 'opening_%'",
            ("u",)).fetchone()[0])
    assert openings <= 1


# --------------------------------------------------------------------------
# 六、真实 HTTP：读取入口在网线上也一样
# --------------------------------------------------------------------------

@pytest.fixture()
def service(tmp_path: Path):
    repo = seeded(tmp_path / "http.sqlite3")
    server = MemoryApiServer(repo, token="audit-token", port=0)
    port = server.start()
    try:
        yield repo, port
    finally:
        server.stop()


def test_every_read_route_is_idempotent_over_real_http(service) -> None:
    """经过 HTTP 再读一遍：同一时刻两次 GET 的响应体逐字节相同，库不再变。"""
    import urllib.request

    repo, port = service
    routes = [
        "/v1/companion/daily-state?userId=u",
        "/v1/companion/story?userId=u",
        "/v1/companion/proactive?userId=u",
        "/v1/companion/relationship?userId=u",
        "/v1/companion/affinity?userId=u",
        "/v1/companion/self-timeline?userId=u",
        "/v1/companion/expressions?userId=u",
        "/v1/companion/living-config?userId=u",
        "/v1/persona-card?userId=u",
        "/v1/profile-memories?userId=u",
        "/v1/perception/state?userId=u",
        "/v1/conversation/messages?userId=u",
        "/v1/debug/state?userId=u",
    ]

    def get(path: str) -> str:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            headers={"Authorization": "Bearer audit-token"})
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == 200, path
            return response.read().decode("utf-8")

    for path in routes:
        get(path)                       # 第一次允许惰性落定
    settled = dump(repo)
    for path in routes:
        first = get(path)
        assert get(path) == first, f"{path} 两次 GET 返回不同"
    assert dump(repo) == settled, "一轮只读 GET 改变了库"
