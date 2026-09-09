from __future__ import annotations

from datetime import datetime, timedelta, timezone

from whale_companion_service.companion_runtime.self_timeline import (
    asks_about_self_timeline,
    recall_self_timeline,
    score_self_timeline,
    self_timeline_context,
)

NOW = datetime(2026, 1, 10, 15, 0, 0, tzinfo=timezone.utc)


def _event(**kwargs) -> dict:
    base = {
        "id": kwargs.pop("id", "e1"),
        "ts": kwargs.pop("ts", NOW.isoformat()),
        "type": kwargs.pop("type", "interaction"),
        "when": kwargs.pop("when", "今天 14:00"),
        "summary": kwargs.pop("summary", "和你聊了会天"),
        "detail": kwargs.pop("detail", ""),
        "status": kwargs.pop("status", "confirmed"),
        "keywords": kwargs.pop("keywords", ""),
    }
    base.update(kwargs)
    return base


def test_asks_about_self_timeline_positive():
    assert asks_about_self_timeline("你昨天做了什么") is True
    assert asks_about_self_timeline("你今天干嘛了") is True
    assert asks_about_self_timeline("大鲸你最近忙什么") is True
    assert asks_about_self_timeline("你自己今天干了啥") is True


def test_asks_about_self_timeline_negative():
    # 用户问的是自己（我），不是大鲸。
    assert asks_about_self_timeline("我昨天做了什么") is False
    assert asks_about_self_timeline("今天天气怎么样") is False
    assert asks_about_self_timeline("你会做什么") is False


def test_recall_only_confirmed():
    events = [
        _event(id="a", status="confirmed", ts=NOW.isoformat()),
        _event(id="b", status="pending", ts=NOW.isoformat()),
        _event(id="c", status="obsolete", ts=NOW.isoformat()),
    ]
    recalled = recall_self_timeline(events, "你今天做了什么", now=NOW)
    assert [e["id"] for e in recalled] == ["a"]


def test_today_beats_yesterday():
    today = _event(id="today", ts=NOW.isoformat(), summary="今天写的日记")
    yesterday = _event(id="yesterday", ts=(NOW - timedelta(days=1)).isoformat(), summary="昨天写的日记")
    recalled = recall_self_timeline([yesterday, today], "你今天做了什么", now=NOW)
    assert recalled[0]["id"] == "today"


def test_yesterday_query_targets_yesterday():
    today = _event(id="today", ts=NOW.isoformat())
    yesterday = _event(id="yesterday", ts=(NOW - timedelta(days=1)).isoformat())
    recalled = recall_self_timeline([today, yesterday], "你昨天做了什么", now=NOW)
    assert recalled[0]["id"] == "yesterday"


def test_keyword_boost_prefers_matching_type():
    diary = _event(id="diary", ts=NOW.isoformat(), type="diary", summary="写日记")
    chat = _event(id="chat", ts=NOW.isoformat(), type="interaction", summary="聊天")
    # 同为今天、同时刻，问「写」应优先日记。
    recalled = recall_self_timeline([chat, diary], "你今天写了什么", now=NOW)
    assert recalled[0]["id"] == "diary"


def test_empty_recall_context_marks_empty():
    ctx = self_timeline_context([])
    assert ctx["empty"] is True
    assert ctx["events"] == []


def test_context_rows_are_trimmed():
    ctx = self_timeline_context([_event(summary="今天 14:00 和你聊了会天，内容有点多" * 10)])
    assert len(ctx["events"][0]["summary"]) <= 160


def test_score_returns_empty_for_no_parseable_time():
    scored = score_self_timeline([_event(ts="not-a-time")], "你今天做了什么", now=NOW)
    assert scored == []
