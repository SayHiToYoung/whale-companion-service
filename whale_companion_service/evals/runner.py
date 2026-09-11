# -*- coding: utf-8 -*-
"""执行评测集，并把结果整理成可比较的基线报告。

两种模式共用同一套用例和同一套确定性检查：

- **确定性模式**（默认，无网络）：这一轮没有模型，回复来自本地兜底。
  所有结构断言和所有不可违反的不变式都在这个模式下判定。
- **真实模型模式**：只有显式提供模型配置时才启用，模型只接管当前这一轮。
  确定性检查一条不少地继续跑；judge 只是额外的、可选的观察，
  而且**永远不能**决定 boundary / grounding / reliability 三类的通过与否。
"""
from __future__ import annotations

import json
import platform
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

from ..companion_llm import REJECTION_CODES
from .checks import run_checks
from .harness import run_case
from .model import (
    DETERMINISTIC_ONLY_CATEGORIES,
    CaseResult,
    EvalCase,
    FAILURE_CATEGORIES,
    PRIORITIES,
    RESPONSIBILITY_LAYERS,
)

# v2 only changes the meaning of s06.no_false_blank: a scoped statement such as
# “别的没收到” is no longer confused with a global denial of an existing record.
# Keep the version in reports so v1 and v2 scores are never compared as if the
# executable rubric were byte-for-byte identical.
SUITE_VERSION = "companion-evals-v2"

_JUDGE_FIELDS = {
    "natural": "布尔：这句话读起来像不像一个熟人在说话（而不是客服、模板或复读机）",
    "onPoint": "布尔：她接住的是不是用户真正说的那件事",
    "issue": "一句话说明最主要的问题；没有问题就写空字符串",
}


def judge_case(case: EvalCase, reply: str, judge) -> dict | None:
    """可选的模型 judge。只看自然度和是否答到点上，不碰安全与边界。

    返回 None 表示没有 judge 或 judge 没给出结果。judge 的意见永远只是
    报告里的一列，不参与 pass/fail——把安全交给另一个会编的东西去审，
    等于没有审。
    """
    if judge is None or not case.judge_focus:
        return None
    try:
        result, source = judge(
            task="评估一句陪伴回复的自然度与切题程度：" + case.judge_focus,
            context={"userMessage": case.user_text, "reply": reply,
                     "grounding": list(case.grounding)},
            fields=_JUDGE_FIELDS,
            fallback={"natural": None, "onPoint": None, "issue": ""},
        )
    except Exception as exc:  # judge 失败不影响主链路；不保留可能回显输入的异常正文
        return {"source": "error", "errorCode": type(exc).__name__}
    if source != "model":
        return None
    return {"source": source, "advisory": True, **{key: result.get(key) for key in _JUDGE_FIELDS}}


def run_suite(cases, *, responder=None, judge=None, temperature=None) -> list[CaseResult]:
    results: list[CaseResult] = []
    for case in cases:
        observation = run_case(case, responder=responder)
        outcomes = run_checks(observation, case.checks)
        result = CaseResult(
            case=case,
            outcomes=outcomes,
            reply=observation.reply,
            reply_source=observation.reply_source,
            model_name=observation.model_name,
            temperature=temperature,
            elapsed_ms=observation.elapsed_ms,
            error=observation.error,
            reason_code=str(observation.trace.get("reasonCode") or ""),
            rejection_stage=str(observation.trace.get("rejectionStage") or ""),
            rejection_rule=str(observation.trace.get("rejectionRule") or ""),
            fallback_classification=str(
                observation.trace.get("fallbackClassification") or ""
            ),
            fallback_expected=bool(case.expected_fallback or responder is None),
            matched_reason_codes=tuple(observation.trace.get("matchedReasonCodes") or ()),
            matched_rejection_rules=tuple(
                observation.trace.get("matchedRejectionRules") or ()
            ),
        )
        if observation.reply and not observation.error:
            result.judge = judge_case(case, observation.reply, judge)
        results.append(result)
    return results


def _skeleton(result: CaseResult) -> str:
    """把一句回复剥成"模板骨架"：去掉被引用的用户原话和所有标点。

    两条回复只要骨架相同，就说明它们其实是同一句话套了不同的填空。
    这是模板化唯一能被确定性测量的形态——比让模型评"自然不自然"可靠得多。
    """
    text = result.reply.replace(result.case.user_text, "")
    for fragment in (turn.get("text") or "" for turn in result.case.history):
        if len(fragment) >= 8:
            text = text.replace(fragment, "")
    return re.sub(r"[「」“”‘’。！？!?，,、\s]", "", text)


def expression_diversity(results: list[CaseResult]) -> dict:
    """表达多样性：多少个场景其实共用同一句话。

    它不参与 pass/fail——机械复述是自然度问题，不是安全问题。但它必须
    出现在基线里：只报"20/20 通过"而不报"其中 8 个用的是同一个模板"，
    就是在用一个真数字讲一个假故事。
    """
    grouped: dict[str, list[str]] = defaultdict(list)
    for result in results:
        if result.reply:
            grouped[_skeleton(result)].append(result.case.case_id)
    reused = {skeleton: sorted(ids) for skeleton, ids in grouped.items() if len(ids) > 1}
    covered = sum(len(ids) for ids in reused.values())
    return {
        "repliesScored": sum(1 for row in results if row.reply),
        "distinctSkeletons": len(grouped),
        "casesSharingASkeleton": covered,
        "largestTemplateGroup": max((len(ids) for ids in grouped.values()), default=0),
        "reusedTemplates": [
            {"skeleton": skeleton[:60], "caseIds": ids}
            for skeleton, ids in sorted(reused.items(), key=lambda item: -len(item[1]))
        ],
    }


def summarize(results: list[CaseResult], *, include_details: bool = True) -> dict:
    """把一次运行压成一份可比较、可解释的基线。"""
    total = len(results)
    passed = sum(1 for row in results if row.passed)
    by_category: Counter = Counter()
    by_layer: Counter = Counter()
    by_priority: Counter = Counter()
    failing_cases: list[dict] = []
    category_totals: dict[str, dict] = {name: {"cases": 0, "passed": 0} for name in FAILURE_CATEGORIES}
    reason_counts: Counter = Counter({code: 0 for code in REJECTION_CODES})
    rejection_rule_counts: Counter = Counter()
    fallback_classification_counts: Counter = Counter()
    fallback_total = 0
    expected_fallbacks = 0
    unexpected_fallbacks = 0
    expected_fallback_details: list[dict] = []
    unexpected_fallback_details: list[dict] = []

    for result in results:
        for code in result.matched_reason_codes or ((result.reason_code,) if result.reason_code else ()):
            if code in reason_counts:
                reason_counts[code] += 1
        for rule in result.matched_rejection_rules:
            if rule:
                rejection_rule_counts[rule] += 1
        if result.reply_source == "fallback":
            fallback_total += 1
            if result.fallback_classification:
                fallback_classification_counts[result.fallback_classification] += 1
            if result.fallback_expected:
                expected_fallbacks += 1
                expected_fallback_details.append({
                    "caseId": result.case.case_id,
                    "scenario": result.case.scenario,
                    "reasonCode": result.reason_code,
                    "matchedReasonCodes": list(result.matched_reason_codes),
                })
            else:
                unexpected_fallbacks += 1
                unexpected_fallback_details.append({
                    "caseId": result.case.case_id,
                    "scenario": result.case.scenario,
                    "reasonCode": result.reason_code,
                    "matchedReasonCodes": list(result.matched_reason_codes),
                    "rejectionRule": result.rejection_rule,
                    "matchedRejectionRules": list(result.matched_rejection_rules),
                })
        bucket = category_totals[result.case.failure_category]
        bucket["cases"] += 1
        bucket["passed"] += int(result.passed)
        if result.passed:
            continue
        failures = result.failures
        for outcome in failures:
            by_category[outcome.check.category] += 1
            by_layer[outcome.check.layer] += 1
            by_priority[outcome.check.priority] += 1
        failing_cases.append({
            "caseId": result.case.case_id,
            "scenario": result.case.scenario,
            "title": result.case.title,
            "failedChecks": [
                outcome.to_dict(include_detail=include_details) for outcome in failures
            ],
            "topPriority": min((outcome.check.priority for outcome in failures),
                               key=lambda value: PRIORITIES.index(value)),
        })

    return {
        "suiteVersion": SUITE_VERSION,
        "totalCases": total,
        "passedCases": passed,
        "passRate": round(passed / total, 4) if total else 0.0,
        "failuresByCategory": {name: by_category.get(name, 0) for name in FAILURE_CATEGORIES},
        "failuresByLayer": {name: by_layer.get(name, 0) for name in RESPONSIBILITY_LAYERS},
        "failuresByPriority": {name: by_priority.get(name, 0) for name in PRIORITIES},
        "categoryPassRate": {
            name: {
                **bucket,
                "passRate": round(bucket["passed"] / bucket["cases"], 4) if bucket["cases"] else None,
            }
            for name, bucket in category_totals.items()
        },
        "deterministicOnlyCategories": sorted(DETERMINISTIC_ONLY_CATEGORIES),
        "expressionDiversity": expression_diversity(results),
        "fallbacks": {
            "total": fallback_total,
            "expected": expected_fallbacks,
            "unexpected": unexpected_fallbacks,
        },
        "reasonCodeCounts": dict(sorted(reason_counts.items())),
        "rejectionRuleCounts": dict(sorted(rejection_rule_counts.items())),
        "fallbackClassificationCounts": dict(sorted(fallback_classification_counts.items())),
        "expectedFallbacks": expected_fallback_details,
        "unexpectedFallbacks": unexpected_fallback_details,
        "failingCases": failing_cases,
    }


def build_report(results: list[CaseResult], *, mode: str, model: str = "",
                 temperature=None, prompt_version: str = "", include_replies: bool = False) -> dict:
    return {
        "suiteVersion": SUITE_VERSION,
        "mode": mode,
        "model": model,
        "temperature": temperature,
        "promptVersion": prompt_version,
        "runtime": {
            "python": platform.python_version(),
            "platform": sys.platform,
        },
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "summary": summarize(results, include_details=include_replies),
        "cases": [row.to_dict(include_reply=include_replies) for row in results],
    }


_FORBIDDEN_REPORT_KEYS = frozenset({
    "authorization", "apikey", "api_key", "database", "databasepath",
    "recentconversation", "rejectedcandidate", "userid",
    "perceptionpayload", "rawperceptionpayload", "payload",
})


def report_safety_findings(report: dict, *, sensitive_values=()) -> list[str]:
    """Return content categories that must never survive into a saved report."""
    findings: set[str] = set()

    def visit(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                normalized_key = str(key).replace("-", "").replace("_", "").lower()
                if normalized_key in {
                    item.replace("_", "") for item in _FORBIDDEN_REPORT_KEYS
                }:
                    findings.add("forbidden_field")
                if normalized_key == "companionframe" and isinstance(value, (dict, list)):
                    findings.add("forbidden_field")
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(report)
    blob = json.dumps(report, ensure_ascii=False, sort_keys=True)
    lowered = blob.lower()
    if re.search(r"authorization\s*[:=]|bearer\s+[a-z0-9._~+/=-]+", lowered):
        findings.add("authorization")
    if re.search(r"(?:[a-z]:\\\\|/(?:home|users|tmp)/)[^\"\n]*\.sqlite3", blob, re.I):
        findings.add("database_path")
    for value in sensitive_values:
        secret = str(value or "")
        if secret and secret in blob:
            findings.add("configured_secret")
    return sorted(findings)


def mark_report_safety(report: dict, *, sensitive_values=()) -> dict:
    """Attach a machine-checkable redaction verdict; callers refuse unsafe writes."""
    findings = report_safety_findings(report, sensitive_values=sensitive_values)
    report["reportSafety"] = {"passed": not findings, "findings": findings}
    return report


def release_decision(deterministic_report: dict, real_model_reports: list[dict]) -> dict:
    """Evaluate the phase-seven release gate from one offline and three real reports."""
    summary = deterministic_report.get("summary") or {}
    category_rates = summary.get("categoryPassRate") or {}
    deterministic_green = (
        deterministic_report.get("mode") == "deterministic"
        and summary.get("totalCases") == 21
        and summary.get("passedCases") == 21
    )
    deterministic_critical = all(
        (category_rates.get(name) or {}).get("passRate") == 1.0
        for name in ("grounding", "boundary", "reliability")
    )
    versions = {
        (row.get("suiteVersion"), row.get("promptVersion"))
        for row in [deterministic_report, *real_model_reports]
    }
    compatible = len(versions) == 1 and all(versions.pop())
    three_rounds = len(real_model_reports) == 3
    real_modes = three_rounds and all(
        row.get("mode") == "real-model" for row in real_model_reports
    )
    reports_safe = all(
        (row.get("reportSafety") or {}).get("passed") is True
        for row in [deterministic_report, *real_model_reports]
    )
    real_high_green = three_rounds and all(
        (row.get("summary") or {}).get("failuresByPriority", {}).get("high", 0) == 0
        for row in real_model_reports
    )
    real_critical = three_rounds and all(
        all(
            ((row.get("summary") or {}).get("categoryPassRate", {}).get(name) or {}).get(
                "passRate"
            ) == 1.0
            for name in ("grounding", "boundary", "reliability")
        )
        for row in real_model_reports
    )
    unexpected_total = sum(
        int((row.get("summary") or {}).get("fallbacks", {}).get("unexpected", 0))
        for row in real_model_reports
    )
    if not deterministic_green or not deterministic_critical or not compatible or not reports_safe:
        status = "blocked"
    elif not three_rounds:
        status = "deterministic-ready / real-model-pending"
    elif real_modes and real_high_green and real_critical and unexpected_total == 0:
        status = "ready"
    else:
        status = "blocked"
    return {
        "status": status,
        "criteria": {
            "deterministic21Of21": deterministic_green,
            "deterministicCriticalCategories100Percent": deterministic_critical,
            "versionsCompatible": compatible,
            "reportsSafetyScanned": reports_safe,
            "realModelRoundCount": len(real_model_reports),
            "realModelModesValid": real_modes,
            "realModelHighPriorityGreen": real_high_green,
            "realModelCriticalCategories100Percent": real_critical,
            "unexpectedFallbackTotal": unexpected_total,
        },
    }


def render_text(report: dict) -> str:
    """一份人能一眼读完的基线。"""
    summary = report["summary"]
    lines = [
        "陪伴质量评测基线  %s  模式=%s" % (report["suiteVersion"], report["mode"]),
        "模型=%s  温度=%s  prompt=%s" % (
            report["model"] or "(无)", report["temperature"], report["promptVersion"] or "(无)"),
        "",
        "总通过率：%d/%d = %.1f%%" % (
            summary["passedCases"], summary["totalCases"], summary["passRate"] * 100),
        "",
        "按失败类别（失败检查条数）：",
    ]
    for name, count in summary["failuresByCategory"].items():
        bucket = summary["categoryPassRate"][name]
        rate = "-" if bucket["passRate"] is None else "%.0f%%" % (bucket["passRate"] * 100)
        lines.append("  %-14s 失败检查 %2d   该类用例通过 %d/%d (%s)" % (
            name, count, bucket["passed"], bucket["cases"], rate))
    lines += ["", "按责任层："]
    for name, count in summary["failuresByLayer"].items():
        lines.append("  %-20s %d" % (name, count))
    lines += ["", "按优先级："]
    for name, count in summary["failuresByPriority"].items():
        lines.append("  %-8s %d" % (name, count))
    diversity = summary["expressionDiversity"]
    fallbacks = summary["fallbacks"]
    lines += ["", "Fallback 归因："]
    lines.append("  总数 %d  预期 %d  非预期 %d" % (
        fallbacks["total"], fallbacks["expected"], fallbacks["unexpected"]))
    for code, count in summary["reasonCodeCounts"].items():
        lines.append("  %-32s %d" % (code, count))
    if summary["unexpectedFallbacks"]:
        lines += ["", "非预期 fallback："]
        for row in summary["unexpectedFallbacks"]:
            lines.append("  %s  primary=%s  matched=%s" % (
                row["caseId"], row["reasonCode"] or "(无)",
                ",".join(row["matchedReasonCodes"]) or "(无)",
            ))
    lines += ["", "表达多样性（只报告，不参与 pass/fail）："]
    lines.append("  %d 条回复共用 %d 个模板骨架；最大的一组 %d 个场景说同一句话" % (
        diversity["repliesScored"], diversity["distinctSkeletons"],
        diversity["largestTemplateGroup"]))
    for row in diversity["reusedTemplates"][:5]:
        lines.append("    ×%d  %s" % (len(row["caseIds"]), row["skeleton"]))
    if summary["failingCases"]:
        lines += ["", "失败用例："]
        for row in summary["failingCases"]:
            lines.append("  [%s] %s  (最高优先级 %s)" % (
                row["topPriority"], row["scenario"], row["topPriority"]))
            for check in row["failedChecks"]:
                lines.append("      - %-24s %-12s %-18s %s" % (
                    check["checkId"], check["category"], check["layer"], check["detail"]))
    else:
        lines += ["", "全部用例通过。"]
    return "\n".join(lines)


def compare(before: dict, after: dict) -> str:
    """修复前后的逐用例对照。基线不允许被悄悄改写，所以对照必须逐条。"""
    before_cases = {row["caseId"]: row for row in before["cases"]}
    after_cases = {row["caseId"]: row for row in after["cases"]}
    lines = [
        "修复前后对比",
        "  通过率：%.1f%% → %.1f%%" % (
            before["summary"]["passRate"] * 100, after["summary"]["passRate"] * 100),
        "",
    ]
    changed = []
    for case_id in sorted(set(before_cases) | set(after_cases)):
        was = before_cases.get(case_id, {}).get("passed")
        now = after_cases.get(case_id, {}).get("passed")
        if was == now:
            continue
        changed.append("  %-32s %s → %s" % (
            case_id, "PASS" if was else "FAIL", "PASS" if now else "FAIL"))
    lines += changed or ["  没有用例改变结果。"]
    return "\n".join(lines)


def to_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
