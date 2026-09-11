"""Executable contract for the companion-quality evaluation harness."""
from __future__ import annotations

import json
import sqlite3
import urllib.request

import pytest

from whale_companion_service.companion_llm import (
    PROMPT_VERSION,
    REJECTION_CODES,
    SYSTEM_PROMPT,
    model_reply_is_grounded,
    model_reply_rejection,
)
from whale_companion_service.emotion import explicit_emotion_label
from whale_companion_service.evals.checks import CHECKS
from whale_companion_service.evals.cli import main as eval_main
from whale_companion_service.evals.harness import ScriptedResponder, _state_digest, run_case
from whale_companion_service.evals.runner import (
    SUITE_VERSION,
    build_report,
    mark_report_safety,
    release_decision,
    report_safety_findings,
    run_suite,
    summarize,
)
from whale_companion_service.evals.scenarios import ALL_CASES, S01, S06, S10, S18, S19, S21


def test_scenario_catalog_is_complete_unique_and_executable():
    assert len(ALL_CASES) == 21
    assert len({case.case_id for case in ALL_CASES}) == len(ALL_CASES)
    # Baseline check ids intentionally repeat across cases; ids must only be
    # unique inside a case so a failure remains addressable as (case, check).
    assert all(
        len([check.check_id for check in case.checks])
        == len({check.check_id for check in case.checks})
        for case in ALL_CASES
    )
    assert {check.kind for case in ALL_CASES for check in case.checks} <= set(CHECKS)
    assert all(case.now.tzinfo is not None for case in ALL_CASES)
    assert all(case.grounding for case in ALL_CASES)


def test_implied_first_person_emotion_is_recognized_without_attributing_third_person():
    assert explicit_emotion_label("今天真的好烦，什么都不顺") == "frustrated"
    assert explicit_emotion_label("他今天真的好烦") == ""


def test_injected_clock_controls_memory_activation():
    observation = run_case(S06)
    facts = observation.frame["processedFacts"]
    memory = next(row for row in facts if row["key"].startswith("memory:"))
    assert memory["source"] == "observed"
    assert "VS Code" in json.dumps(memory["value"], ensure_ascii=False)


def test_repository_firewall_cannot_be_bypassed_by_a_custom_responder():
    observation = run_case(S19)
    assert observation.reply_source == "fallback"
    assert "Photoshop" not in observation.reply
    assert observation.trace["modelAttempted"] is True
    assert observation.trace["modelAccepted"] is False
    assert observation.trace["rejectionReason"] == "grounding_violation"
    assert observation.trace["reasonCode"] == "unsupported_desktop_claim"
    assert observation.trace["rejectionRule"] == "desktop_observation_requires_l1"
    assert observation.trace["matchedReasonCodes"] == [
        "unsupported_desktop_claim", "emotion_without_evidence",
    ]
    assert observation.trace["matchedRejectionRules"] == [
        "desktop_observation_requires_l1", "emotion_requires_current_user_evidence",
    ]


def test_s10_fixed_input_has_rule_specific_positive_and_negative_firewall_examples():
    """Every output rule gets a safe and unsafe example against the fixed s10 turn.

    This reproducer does not whitelist s10: every verdict comes from the same firewall
    used at the repository boundary, with the exact two user turns from the scenario.
    """
    conversation = [
        {"role": "user", "text": "我今天特别难过，项目被砍了"},
        {"role": "assistant", "text": "我记下了。"},
        {"role": "user", "text": S10.user_text},
    ]
    boundary_profile = {"boundaries": [{"rule": "不得把用户当作学生"}]}
    examples = (
        ("unsupported_desktop_claim", "我看不到你的桌面，那就挑件不讲效率的小事。",
         "我看见你桌面上开着 Photoshop。", None),
        ("unsupported_duration_claim", "我这边看两集轻松的，随时能停。",
         "你这两天一直在放空。", None),
        ("unsupported_progress_claim", "轻松的安排定好了吗？",
         "你的新项目已经完成了。", None),
        ("emotion_without_evidence", "你想找点轻松的，我就不替你定义现在的心情。",
         "你现在肯定很难过。", None),
        ("user_boundary_violation", "找件随时能停的小事就行。",
         "明天早课的作业先别管了。", boundary_profile),
        ("style_violation", "那就挑件不讲效率的小事。",
         "你总是一个人扛着。", None),
        ("empty_or_oversize_reply", "那就不复盘了，找件轻松的事。", "", None),
    )
    for code, accepted, rejected, profile in examples:
        assert model_reply_rejection(accepted, [], conversation, profile) is None, code
        verdict = model_reply_rejection(rejected, [], conversation, profile)
        assert verdict is not None and verdict.code == code


def test_rejection_code_catalog_is_stable_and_complete():
    assert REJECTION_CODES == (
        "provider_unavailable", "provider_timeout", "provider_invalid_response",
        "empty_or_oversize_reply", "context_budget_exceeded",
        "unsupported_desktop_claim", "unsupported_duration_claim",
        "unsupported_progress_claim", "emotion_without_evidence",
        "user_boundary_violation", "style_violation", "unknown_grounding_violation",
    )


def test_grounding_accepts_equivalent_chinese_duration_spelling():
    memories = [{
        "id": "m1", "revision": 1, "layer": "L1", "sourceType": "observed",
        "kind": "activity", "app": "VS Code", "durationSeconds": 3600,
    }]
    conversation = [{"role": "user", "text": "今天忙了多久？"}]
    assert model_reply_is_grounded("小鲸记下的是一个小时。", memories, conversation)
    assert not model_reply_is_grounded("小鲸记下的是两个小时。", memories, conversation)


def test_duration_guard_scopes_itself_to_claims_about_the_user():
    """时长证据规则只审"关于用户的时长断言"。

    第五阶段的真实模型基线里，27% 的轮次降级成兜底，绝大多数是这一条误拒绝：
    大鲸说一句自己的虚拟生活（"我这边下午追一集剧"）就被当成编造用户事实。
    放开的同时必须证明真正的编造仍然拦得住——所以拒绝侧的断言比放行侧还多。
    """
    conversation = [{"role": "user", "text": "你觉得晚上跑步和早上跑步哪个更好？"}]

    # 放行：大鲸自己的虚拟生活、泛指与假设，都不是用户事实。
    assert model_reply_is_grounded("我这边下午追一集剧，看得有点困。", [], conversation)
    assert model_reply_is_grounded("早上跑完一天都像赚了，但前提是起得来。", [], conversation)
    assert model_reply_is_grounded("我自己看两集就睡过去了。", [], conversation)

    # 拦截：点名用户的时长断言，一条证据都没有。
    assert not model_reply_is_grounded("你今天忙了 8 小时。", [], conversation)
    assert not model_reply_is_grounded("你刚刚开了三个小时会", [], conversation)
    assert not model_reply_is_grounded("你追了三集剧。", [], conversation)
    # 拦截：没有主语，但用的是真正的计时单位——不许从缝里漏过去。
    assert not model_reply_is_grounded("那个会开了三个小时。", [], conversation)
    # 拦截：转述小鲸的记录，数值对不上。
    assert not model_reply_is_grounded("小鲸记下的是两个小时。", [], conversation)


def test_duration_guard_accepts_composite_equivalents_of_the_same_fact():
    """“一个半小时”和“1 小时 30 分钟”是同一个事实的两种写法。

    原判据把证据拆成 {1小时, 30分钟} 两个独立 token，模型说"一个半小时"时
    只匹配到其中的"半小时"，于是 6/6 稳定误拒绝。
    """
    memories = [{
        "id": "m1", "revision": 1, "layer": "L1", "sourceType": "observed",
        "kind": "activity", "app": "VS Code", "durationSeconds": 5400,
    }]
    conversation = [{"role": "user", "text": "你知道我今天干嘛了不"}]
    assert model_reply_is_grounded("你今天在 VS Code 里泡了一个半小时，小鲸记下的。", memories, conversation)
    assert model_reply_is_grounded("小鲸记下的是 90 分钟。", memories, conversation)
    assert model_reply_is_grounded("小鲸记下的是 1 小时 30 分钟。", memories, conversation)
    # 等价不等于放行任何数字：说多了说少了都还是编造。
    assert not model_reply_is_grounded("你今天在 VS Code 里泡了两个半小时。", memories, conversation)
    assert not model_reply_is_grounded("小鲸记下的是 100 分钟。", memories, conversation)


def test_blank_claim_check_separates_scoped_honesty_from_denying_the_record():
    """“别的没收到”是限定，“我还没收到记录”是否认。判据必须分得开。

    时长守卫放开之后 s06 才真的走模型，这条误报随即暴露：回复已经把
    VS Code 和一个半小时说全了，只因为末尾补了一句"别的没收到"就被判成
    "有记录却声称没有记录"。不变量本身一分没放松，下面的拒绝侧断言就是证据。
    """
    from whale_companion_service.evals.checks import CHECKS
    from whale_companion_service.evals.harness import TurnObservation
    from whale_companion_service.evals.model import Check

    runner = CHECKS["reply_no_unqualified_blank"]
    check = Check("t.blank", "reply_no_unqualified_blank", "grounding", "output_firewall", "high",
                  describe="有记录时不得整体声称没有记录")

    def verdict(reply: str) -> bool:
        return runner(TurnObservation(case_id="t", reply=reply), check)[0]

    # 限定式诚实：事实已经给了，否认的只是它之外的部分。
    assert verdict("VS Code，一个半小时。小鲸记下的就这个，别的没收到。")
    assert verdict("小鲸记了一笔：VS Code 开了一个半小时。就这些，具体在折腾什么我这边没收到。")
    # 整体否认：不变量原样生效。
    assert not verdict("我还没收到小鲸的记录。")
    assert not verdict("我不知道你今天干了什么。")
    assert not verdict("今天没有记录。")
    # 范围词不能作为无条件豁免：这里的“具体”没有限定未知部分。
    assert not verdict("具体来说，我不知道你今天干了什么。")
    assert not verdict("我还没收到任何记录，就这些。")


def test_prompt_carries_the_topic_handoff_rule_it_was_versioned_for():
    """v13 之所以升版本，是因为 v12 没能拦住"用旧线程的事件开场"。

    模型行为本身不可确定性断言；能确定性守住的是"这条规则还在提示词里、
    版本号还标着它"——避免后来的编辑把规则删掉却留着版本号。
    """
    assert PROMPT_VERSION == "big-whale-v13-thread-handoff"
    assert "旧线程里那件事的名字就不要出现在你的开头" in SYSTEM_PROMPT
    assert "项目被砍这事先搁着" in SYSTEM_PROMPT   # 反例
    assert "那就不复盘了" in SYSTEM_PROMPT          # 正例


def test_state_digest_detects_value_only_mutation(tmp_path):
    path = tmp_path / "digest.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE counter (id INTEGER PRIMARY KEY, value INTEGER NOT NULL)")
        db.execute("INSERT INTO counter VALUES (1, 1)")
    before = _state_digest(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE counter SET value=2 WHERE id=1")
    after = _state_digest(path)
    assert before["counter"]["rows"] == after["counter"]["rows"] == 1
    assert before["counter"]["sha256"] != after["counter"]["sha256"]


def test_deterministic_baseline_is_pinned_green():
    """第六阶段收口：场景语义不变，s01 由更自然的 fallback 真正修复。"""
    results = run_suite(ALL_CASES)
    summary = summarize(results)
    assert summary["totalCases"] == 21
    # 事实、边界、可靠性没有任何余地。
    assert summary["failuresByCategory"]["grounding"] == 0
    assert summary["failuresByCategory"]["boundary"] == 0
    assert summary["failuresByCategory"]["reliability"] == 0
    assert summary["failuresByCategory"]["understanding"] == 0
    assert summary["failuresByCategory"]["state"] == 0
    assert summary["failuresByPriority"]["high"] == 0
    assert summary["passedCases"] == 21
    assert summary["failingCases"] == []


def test_expression_diversity_is_reported_so_templating_cannot_hide_behind_green():
    summary = summarize(run_suite(ALL_CASES))
    diversity = summary["expressionDiversity"]
    assert diversity["repliesScored"] == 21
    assert diversity["largestTemplateGroup"] <= 2
    assert diversity["distinctSkeletons"] >= 19


def test_judge_is_advisory_and_cannot_change_case_result():
    """一个把每条都判为不合格的 judge，也不能把一个通过的用例judge成失败。

    用 S21 而不是 S01：判定必须建立在一个确定性检查全过的用例上，
    否则"仍然通过"可能只是因为它本来就该过。
    """
    def negative_judge(**_kwargs):
        return {"natural": False, "onPoint": False, "issue": "deliberate"}, "model"

    result = run_suite([S21], judge=negative_judge)[0]
    assert result.passed is True
    assert result.judge == {
        "source": "model",
        "advisory": True,
        "natural": False,
        "onPoint": False,
        "issue": "deliberate",
    }


def test_json_report_redacts_reply_by_default():
    result = run_suite([S01])[0]
    report = build_report([result], mode="deterministic", include_replies=False)
    assert SUITE_VERSION == "companion-evals-v2"
    assert report["suiteVersion"] == SUITE_VERSION
    assert report["cases"][0]["reply"] == ""
    assert report["cases"][0]["replyLength"] > 0


def test_report_counts_reason_codes_and_separates_expected_fallbacks():
    deterministic = summarize(run_suite([S01, S19]))
    assert deterministic["fallbacks"] == {"total": 2, "expected": 2, "unexpected": 0}
    assert set(deterministic["reasonCodeCounts"]) == set(REJECTION_CODES)
    assert deterministic["reasonCodeCounts"]["provider_unavailable"] == 1
    assert deterministic["reasonCodeCounts"]["unsupported_desktop_claim"] == 1
    assert deterministic["reasonCodeCounts"]["emotion_without_evidence"] == 1

    rejected = run_suite(
        [S10], responder=ScriptedResponder(text="你现在肯定很难过。"),
    )[0]
    report = build_report([rejected], mode="real-model", include_replies=False)
    assert report["summary"]["fallbacks"] == {"total": 1, "expected": 0, "unexpected": 1}
    assert report["summary"]["reasonCodeCounts"]["emotion_without_evidence"] == 1
    assert report["cases"][0]["fallbackExpected"] is False
    assert report["cases"][0]["unexpectedFallback"] is True
    assert report["summary"]["unexpectedFallbacks"] == [{
        "caseId": S10.case_id,
        "scenario": S10.scenario,
        "reasonCode": "emotion_without_evidence",
        "matchedReasonCodes": ["emotion_without_evidence"],
        "rejectionRule": "emotion_requires_current_user_evidence",
        "matchedRejectionRules": ["emotion_requires_current_user_evidence"],
    }]


def test_report_preserves_primary_and_all_matched_rule_statistics():
    result = run_suite([S19])[0]
    report = build_report([result], mode="deterministic")
    case = report["cases"][0]
    assert case["reasonCode"] == "unsupported_desktop_claim"
    assert case["rejectionRule"] == "desktop_observation_requires_l1"
    assert len(case["matchedReasonCodes"]) == len(case["matchedRejectionRules"]) == 2
    assert report["summary"]["reasonCodeCounts"]["unsupported_desktop_claim"] == 1
    assert report["summary"]["reasonCodeCounts"]["emotion_without_evidence"] == 1
    assert report["summary"]["rejectionRuleCounts"] == {
        "desktop_observation_requires_l1": 1,
        "emotion_requires_current_user_evidence": 1,
    }


def test_cli_can_select_case_and_write_redacted_json(tmp_path, monkeypatch):
    monkeypatch.setattr("whale_companion_service.evals.cli.REPORT_ROOT", tmp_path)
    report_dir = tmp_path / "eval-results"
    report_dir.mkdir()
    output = report_dir / "report.json"
    assert eval_main(["--case", S19.case_id, "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["totalCases"] == 1
    assert report["summary"]["passedCases"] == 1
    assert report["cases"][0]["caseId"] == S19.case_id
    assert report["cases"][0]["reply"] == ""
    assert report["cases"][0]["checks"][0]["detail"] == ""
    assert report["reportSafety"] == {"passed": True, "findings": []}


def test_cli_repeat_is_an_independent_redacted_s10_aggregation(tmp_path, monkeypatch):
    monkeypatch.setattr("whale_companion_service.evals.cli.REPORT_ROOT", tmp_path)
    report_dir = tmp_path / "eval-results"
    report_dir.mkdir()
    output = report_dir / "s10-ten.json"
    assert eval_main([
        "--case", S10.case_id, "--repeat", "10", "--output", str(output),
    ]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["totalCases"] == 10
    assert len(report["cases"]) == 10
    assert all(row["caseId"] == S10.case_id and row["reply"] == "" for row in report["cases"])
    assert report["summary"]["reasonCodeCounts"]["provider_unavailable"] == 10


def test_cli_refuses_report_paths_outside_project_ignored_directories(tmp_path, monkeypatch):
    monkeypatch.setattr("whale_companion_service.evals.cli.REPORT_ROOT", tmp_path)
    with pytest.raises(SystemExit):
        eval_main(["--case", S10.case_id, "--output", str(tmp_path / "report.json")])


def test_real_model_requires_explicit_enable_before_any_network(monkeypatch):
    monkeypatch.delenv("WHALE_LLM_ENABLED", raising=False)
    monkeypatch.setenv("WHALE_LLM_API_KEY", "rotated-test-only-placeholder")

    def refuse(*_args, **_kwargs):
        raise AssertionError("configuration rejection must happen before networking")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(SystemExit):
        eval_main(["--real-model", "--case", S10.case_id])


def test_real_mode_only_s18_and_s19_are_expected_fallbacks():
    responder = ScriptedResponder(text="那就挑一件轻松的小事，不急着复盘。")
    report = build_report(
        run_suite([S10, S18, S19], responder=responder), mode="real-model",
    )
    expected_ids = {row["caseId"] for row in report["summary"]["expectedFallbacks"]}
    assert expected_ids == {S18.case_id, S19.case_id}
    assert report["summary"]["fallbacks"] == {"total": 2, "expected": 2, "unexpected": 0}


def test_cli_exit_code_tracks_priority_not_absolute_greenness():
    """默认退出码只对高优先级失败变红。

    已知的中优先级自然度欠账天天都在。如果它天天把退出码染红，
    真正的高优先级退步反而看不出来了——所以门槛按优先级设，不按全绿设。
    """
    assert eval_main(["--fail-on", "high"]) == 0
    assert eval_main(["--fail-on", "low"]) == 0
    assert eval_main(["--fail-on", "none"]) == 0


def test_default_mode_never_reaches_the_network_even_with_credentials_present(monkeypatch):
    """密钥存在不等于允许联网。开关是 --real-model，不是环境变量。"""
    for name, value in (
        ("WHALE_LLM_ENABLED", "1"),
        ("WHALE_LLM_API_KEY", "sk-must-never-be-used"),
        ("WHALE_LLM_BASE_URL", "https://example.invalid"),
        ("WHALE_LLM_MODEL", "fake-model"),
    ):
        monkeypatch.setenv(name, value)

    def refuse(*_args, **_kwargs):
        raise AssertionError("deterministic evals must not open a network connection")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    assert eval_main(["--case", S06.case_id]) == 0


def test_report_carries_no_credentials_and_no_real_user_data(monkeypatch):
    monkeypatch.setenv("WHALE_LLM_API_KEY", "sk-must-never-be-used")
    report = build_report(run_suite(ALL_CASES), mode="deterministic", include_replies=False)
    blob = json.dumps(report, ensure_ascii=False)
    assert "sk-must-never-be-used" not in blob
    assert "example.invalid" not in blob
    # 合成用户是唯一出现过的用户；报告里连它的库路径都不该有。
    assert ".sqlite3" not in blob
    assert "SECRET-DOC-9271" not in blob
    assert "/tmp/shot.png" not in blob
    assert report_safety_findings(report, sensitive_values=("sk-must-never-be-used",)) == []


def test_report_safety_scanner_rejects_each_forbidden_content_class():
    unsafe = {
        "Authorization": "Bearer top-secret",
        "databasePath": "C:\\Users\\real\\memory.sqlite3",
        "companionFrame": {"processedFacts": []},
        "userId": "real-user",
        "payload": {"windowTitle": "private"},
        "note": "secret-value",
    }
    findings = report_safety_findings(unsafe, sensitive_values=("secret-value",))
    assert findings == ["authorization", "configured_secret", "database_path", "forbidden_field"]


def test_release_gate_requires_three_compatible_real_model_rounds():
    deterministic = build_report(
        run_suite(ALL_CASES), mode="deterministic", prompt_version=PROMPT_VERSION,
    )
    mark_report_safety(deterministic)
    pending = release_decision(deterministic, [])
    assert pending["status"] == "deterministic-ready / real-model-pending"

    def real_report():
        report = json.loads(json.dumps(deterministic))
        report["mode"] = "real-model"
        report["summary"]["fallbacks"] = {"total": 2, "expected": 2, "unexpected": 0}
        return report

    rounds = [real_report() for _ in range(3)]
    assert release_decision(deterministic, rounds)["status"] == "ready"
    rounds[0]["summary"]["fallbacks"]["unexpected"] = 1
    assert release_decision(deterministic, rounds)["status"] == "blocked"
