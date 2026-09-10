"""Targeted repair acceptance. All writes use pytest temporary SQLite databases."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json

import pytest

from whale_companion_service.companion_mind import build_companion_mind, contextual_fallback
from whale_companion_service.companion_runtime import build_companion_frame
from whale_companion_service.companion_runtime.shared_scene import build_shared_scene
from whale_companion_service.companion_runtime.turn_decision import build_turn_decision
from whale_companion_service.memory_server import MemoryRepository


def decide(text, history=(), stage="familiar", boundary="open"):
    rows = [*history, {"role": "user", "text": text}]
    return build_turn_decision(build_shared_scene(text, rows), {"signal": ""}, {
        "stage": stage, "boundaryState": boundary,
        "permissions": {"allowPlayful": stage == "close", "allowSharedMemory": stage == "close"},
    })


def fallback(text, history=(), **kwargs):
    rows = [*history, {"role": "user", "text": text}]
    decision = decide(text, history, **kwargs)
    return decision, contextual_fallback(text, rows, build_companion_mind(text, rows), decision)


def test_share_contributes_a_grounded_observation_and_specific_hook():
    decision, reply = fallback("今天去公园散步，走了一条没走过的小路")
    assert decision["conversationMove"] == "deepen"
    assert decision["replyHook"] == "specific_curiosity"
    assert "散步" in reply and "路上" in reply and "停下来" in reply
    assert reply != decision["hookAnchor"]  # Not a paraphrase/acknowledgement.


def test_question_answers_first_without_followup():
    decision, reply = fallback("今天几点日落？")
    assert decision["answerFirst"] and not decision["askQuestion"]
    assert decision["primaryAction"] == "answer_directly"
    assert "答不上来" in reply  # No fabricated weather/astronomy without a provider.


@pytest.mark.parametrize("text", ["不想聊了", "别问", "算了"])
def test_stop_never_pivots_or_disguises_a_question(text):
    decision, reply = fallback(text)
    assert decision["conversationMove"] in {"close", "hold"}
    assert decision["replyHook"] == "none" and decision["allowSilenceAfter"]
    assert "不往下问" in reply and "换" not in reply


def test_multiturn_question_cadence_is_not_an_interview():
    history, decisions = [], []
    for text in ["今天去公园散步了", "路上看到了一只猫", "它一直在晒太阳", "后来我也坐了一会儿"]:
        decision, reply = fallback(text, history)
        decisions.append(decision)
        history.extend([{"role": "user", "text": text}, {"role": "assistant", "text": reply}])
    assert [d["askQuestion"] for d in decisions] == [True, False, False, True]
    assert all(d["contributeContent"] for d in decisions)
    assert decisions[1]["replyHook"] == "personal_reaction"


def test_happy_share_can_play_and_celebrate_without_a_question():
    decision, reply = fallback("太开心了，我终于完成了作品", stage="close")
    assert (decision["conversationMove"], decision["replyHook"]) == ("play", "playful_hook")
    assert "好事" in reply and "让个位" in reply


def test_low_mood_has_one_light_question_then_no_more_probing():
    decision, first = fallback("我今天很难过")
    assert decision["replyHook"] == "specific_curiosity" and "哪一件事" in first
    second, reply = fallback("我还是很难过", [{"role": "user", "text": "我今天很难过"}, {"role": "assistant", "text": first}])
    assert not second["askQuestion"] and second["replyHook"] == "personal_reaction"
    assert "想开点" in reply and "解释" in reply


def test_short_reply_retains_previous_topic():
    decision, reply = fallback("对啊", [{"role": "user", "text": "今天去公园散步"}, {"role": "assistant", "text": "我喜欢没有目的的那一段。"}])
    assert decision["primaryAction"] == "continue_shared_topic"
    assert "公园散步" in reply and "细节" in reply


@pytest.mark.parametrize("text,history", [
    ("换个话题", []),
    ("嗯", [{"role": "user", "text": "确实"}, {"role": "assistant", "text": "这一段差不多了。"}]),
])
def test_exhausted_topic_can_pivot_to_a_concrete_preference(text, history):
    decision, reply = fallback(text, history)
    assert decision["conversationMove"] == "pivot"
    assert "雨声" in reply and "想聊什么" not in reply


@pytest.mark.parametrize("stage,boundary", [("distant", "open"), ("close", "restricted")])
def test_distance_or_boundary_restricts_probing(stage, boundary):
    decision, reply = fallback("最近工作有点变化", stage=stage, boundary=boundary)
    assert decision["conversationMove"] == "hold" and decision["replyHook"] == "none"
    assert "？" not in reply


def test_close_relationship_can_callback_but_distance_cannot():
    text = "上次我们聊的那条小路，我又去了"
    decision = decide(text, stage="close")
    assert (decision["conversationMove"], decision["replyHook"]) == ("bridge", "memory_callback")
    assert decide(text, stage="distant")["replyHook"] == "none"


def test_move_and_hook_reach_the_model_context():
    text = "今天去公园散步了"
    frame = build_companion_frame(user_text=text, conversation=[{"role": "user", "text": text}],
        memories=[], user_facts=[], boundaries=[], persona={})
    view = json.dumps(frame.model_view(), ensure_ascii=False)
    assert "conversationMove" in view and "specific_curiosity" in view
    assert "contributeContent" in view


def make_repo(tmp_path):
    clock = {"now": datetime(2026, 3, 5, 9, tzinfo=datetime.now().astimezone().tzinfo)}
    repo = MemoryRepository(tmp_path / "acceptance.sqlite3", clock=lambda: clock["now"])
    return repo, clock


def deliver(repo, delivery="one", **kwargs):
    return repo.commit_proactive_decision({"userId": "u", "deliveryId": delivery, "deviceId": "desktop", **kwargs})


def say(repo, text, message="reply"):
    return repo.append_message({"userId": "u", "deviceId": "phone", "role": "user", "messageId": message, "text": text})


def test_readonly_evaluation_has_no_candidate_story_or_history_writes(tmp_path):
    repo, _ = make_repo(tmp_path)
    for _ in range(3):
        assert repo.proactive_decision("u")["pressure"]["dailySent"] == 0
    with repo._connect() as db:
        for table in ("proactive_candidates", "story_arcs", "conversation_messages", "opening_claims"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_delivery_is_durable_idempotent_and_incrementally_readable(tmp_path):
    repo, _ = make_repo(tmp_path)
    first = deliver(repo)
    assert first["shouldSpeak"] and first["assistantMessage"]["text"]
    assert deliver(repo) == first
    assert deliver(repo, "two", deviceId="phone")["assistantMessage"] is None
    page = repo.messages("u", after_message_seq=0, limit=1)
    assert page["messages"][0]["messageId"] == first["assistantMessage"]["messageId"]
    assert page["messages"][0]["deviceId"] == "proactive"
    assert repo.messages("u", page["nextMessageSeq"])["messages"] == []
    with repo._connect() as db:
        assert db.execute("SELECT sum(deliver_count) FROM proactive_candidates").fetchone()[0] == 1


def test_two_devices_race_for_one_candidate_without_duplicate_messages(tmp_path):
    repo, _ = make_repo(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda d: deliver(repo, d, deviceId=d), ["phone", "desktop"]))
    assert sum(bool(r["assistantMessage"]) for r in results) == 1
    assert len(repo.messages("u")["messages"]) == 1


@pytest.mark.parametrize("activity", ["meeting", "gaming", "focus"])
def test_desktop_presence_also_blocks_phone_without_creating_pressure(tmp_path, activity):
    repo, _ = make_repo(tmp_path)
    assert not deliver(repo, activity=activity)["shouldSpeak"]
    assert not deliver(repo, "phone", deviceId="phone", activity="active")["shouldSpeak"]
    assert repo.messages("u")["messages"] == []
    assert repo.proactive_decision("u")["pressure"]["noResponseStreak"] == 0
    with repo._connect() as db:
        assert db.execute("SELECT count(*) FROM story_arcs").fetchone()[0] == 0


def test_runtime_share_agenda_delivery_mobile_reply_and_solicited_cap(tmp_path):
    repo, clock = make_repo(tmp_path)
    first = say(repo, "今天去公园散步了", "share")
    assert "散步" in first["assistantMessage"]["text"] and "路上" in first["assistantMessage"]["text"]
    cursor = repo.messages("u")["nextMessageSeq"]
    clock["now"] += timedelta(hours=2)
    sent = deliver(repo)
    assert sent["selected"]["kind"] in {"daily_greeting", "meal_care", "agenda_share", "mood_checkin"}
    assert sent["assistantMessage"]["text"]
    assert repo.messages("u", cursor)["messages"][0]["messageId"] == sent["assistantMessage"]["messageId"]
    assert repo.proactive_decision("u")["pressure"]["noResponseStreak"] == 1
    clock["now"] += timedelta(minutes=1)
    say(repo, "谢谢你", "reply")
    assert repo.proactive_decision("u")["pressure"]["noResponseStreak"] == 0
    with repo._connect() as db:
        ledger = db.execute("SELECT solicited,delta FROM relationship_ledger WHERE event_key='interaction:reply'").fetchone()
        assert ledger is not None and ledger["solicited"] == 1
        assert 0 <= ledger["delta"] <= 2
        assert db.execute("SELECT count(*) FROM story_arcs").fetchone()[0] > 0
    print(json.dumps({"flow": "share→hook→agenda→delivery→mobile-cursor→reply", "messageId": sent["assistantMessage"]["messageId"], "noResponseStreak": 0, "solicited": True}))


def test_story_delivery_is_visible_and_replay_does_not_advance_twice(tmp_path):
    repo, clock = make_repo(tmp_path)
    repo.adjust_affinity({"userId": "u", "score": 650.0})
    stories = []
    for step in range(36):
        clock["now"] += timedelta(hours=1)
        if step % 3 == 0:
            say(repo, "嗯", f"user-{step}")
        clock["now"] += timedelta(hours=1)
        sent = deliver(repo, f"delivery-{step}")
        if (sent.get("selected") or {}).get("kind") != "story_beat":
            continue
        stories.append(sent["selected"]["signature"])
        with repo._connect() as db:
            before = [tuple(r) for r in db.execute("SELECT * FROM story_arcs ORDER BY arc_id")]
        assert deliver(repo, f"delivery-{step}") == sent
        with repo._connect() as db:
            assert before == [tuple(r) for r in db.execute("SELECT * FROM story_arcs ORDER BY arc_id")]
        assert any(m["messageId"] == sent["assistantMessage"]["messageId"] for m in repo.messages("u")["messages"])
    assert stories and len(stories) == len(set(stories))


def test_no_response_pause_survives_multiple_days(tmp_path):
    repo, clock = make_repo(tmp_path)
    for hour in range(12):
        deliver(repo, f"hour-{hour}")
        clock["now"] += timedelta(hours=1)
    before = repo.proactive_decision("u")["pressure"]["noResponseStreak"]
    assert before >= 3
    clock["now"] += timedelta(days=4)
    deliver(repo, "four-days-later")
    assert repo.proactive_decision("u")["pressure"]["noResponseStreak"] == before


def test_explicit_do_not_disturb_blocks_delivery_and_keeps_pressure_zero(tmp_path):
    repo, clock = make_repo(tmp_path)
    say(repo, "不要主动找我", "boundary")
    clock["now"] += timedelta(hours=2)
    sent = deliver(repo)
    assert not sent["shouldSpeak"] and sent["assistantMessage"] is None
    assert sent["pressure"]["dailySent"] == 0
    assert any(c["vetoReason"] == "user_boundary" for c in sent["candidates"])


def test_phone_conversation_prevents_desktop_idle_from_interrupting(tmp_path):
    repo, clock = make_repo(tmp_path)
    say(repo, "今天在公园散步", "share")
    clock["now"] += timedelta(seconds=30)
    result = deliver(repo, activity="idle")
    assert not result["shouldSpeak"] and result["assistantMessage"] is None
    assert result["pressure"]["noResponseStreak"] == 0


def test_topic_suppression_still_blocks_queued_content(tmp_path):
    repo, clock = make_repo(tmp_path)
    say(repo, "不要再提健身房", "topic-boundary")
    clock["now"] += timedelta(hours=2)
    with repo._connect() as db:
        db.execute("""INSERT INTO proactive_candidates
            (user_id, signature, kind, content, topic, status, decision, veto_reason, score,
             window_start, best_until, expires_at, deliver_count, created_at, updated_at, sent_at)
            VALUES ('u','blocked','agenda_share','健身房','健身房','deferred','','',99,?,?,?,?,? ,?,'')""",
            (clock["now"].isoformat(), (clock["now"] + timedelta(hours=1)).isoformat(),
             (clock["now"] + timedelta(hours=2)).isoformat(), 0, clock["now"].isoformat(), clock["now"].isoformat()))
    result = deliver(repo)
    candidate = next(c for c in result["candidates"] if c["signature"] == "blocked")
    assert candidate["decision"] == "drop" and candidate["status"] != "sent"
    assert "健身房" not in (result.get("assistantMessage") or {}).get("text", "")


def test_proactive_rendering_has_content_specific_reactions_and_no_model_call():
    from whale_companion_service.companion_runtime.proactive_expression import render_proactive
    rain = render_proactive({"kind": "agenda_share", "content": "外面下起雨来"})
    assert "虚拟" in rain and "雨声" in rain and "背景音" in rain
    assert "？" not in rain
    emotional = render_proactive({"kind": "open_emotion", "content": "sad"})
    assert "上次" in emotional and "变化" in emotional and "sad" not in emotional


def test_unavailable_model_fallback_obeys_multiturn_policy(tmp_path):
    class Broken:
        available, name = True, "test-unavailable"
        def reply(self, *_args):
            raise RuntimeError("offline")
    repo, _ = make_repo(tmp_path)
    repo.responder = Broken()
    first = say(repo, "我今天很开心", "a")
    second = say(repo, "我现在还是很开心", "b")
    assert first["replySource"] == second["replySource"] == "fallback"
    assert "好事" in second["assistantMessage"]["text"]
    assert "？" not in second["assistantMessage"]["text"]
