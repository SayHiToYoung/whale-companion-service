"""Command-line entry point for companion quality evaluations."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ..cli import configured_responder
from ..companion_llm import PROMPT_VERSION
from ..configuration import ConfigurationError, ServiceConfig
from .runner import (
    build_report,
    compare,
    mark_report_safety,
    release_decision,
    render_text,
    run_suite,
    to_json,
)
from .scenarios import ALL_CASES, cases_by_id

REPORT_ROOT = Path(__file__).resolve().parents[2]


def _selected_cases(values: list[str]) -> list:
    if not values:
        return list(ALL_CASES)
    requested = [item.strip() for value in values for item in value.split(",") if item.strip()]
    known = cases_by_id()
    missing = [item for item in requested if item not in known]
    if missing:
        raise ValueError("unknown case id: " + ", ".join(missing))
    return [known[item] for item in requested]


def _safe_output_path(value: Path) -> Path:
    """Reports may only be written below an already-existing ignored report dir."""
    resolved = value.resolve()
    if not resolved.parent.is_dir():
        raise ValueError("report output directory must already exist")
    allowed_roots = tuple((REPORT_ROOT / name).resolve() for name in ("eval-results", ".run"))
    if not any(root.is_dir() and resolved.is_relative_to(root) for root in allowed_roots):
        raise ValueError("report output must be under eval-results/ or .run/")
    return resolved


def _load_report(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("unable to read release report: " + type(exc).__name__) from None
    if not isinstance(data, dict):
        raise ValueError("release report must be a JSON object")
    return data


def main(argv: list[str] | None = None) -> int:
    # Windows may otherwise encode redirected output with the legacy code page,
    # which makes Chinese reports unreadable in CI logs and agent terminals.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except (AttributeError, OSError):  # StringIO and embedded hosts
                pass
    parser = argparse.ArgumentParser(description="AI 陪伴质量评测")
    parser.add_argument("--case", action="append", default=[], help="用例 ID，可重复或逗号分隔")
    parser.add_argument("--list", action="store_true", help="列出用例后退出")
    parser.add_argument("--real-model", action="store_true", help="显式启用 WHALE_LLM_* 配置的真实模型")
    parser.add_argument("--judge", action="store_true", help="用同一模型追加自然度与切题度建议")
    parser.add_argument("--output", type=Path, help="写入结构化 JSON 报告")
    parser.add_argument("--include-replies", action="store_true", help="在 JSON 中保留合成场景的回复正文")
    parser.add_argument("--compare", type=Path, help="与旧 JSON 基线做逐用例对比")
    parser.add_argument("--repeat", type=int, default=1, help="每个选中用例独立运行次数")
    parser.add_argument(
        "--release-check", nargs="+", type=Path, metavar="REPORT",
        help="离线验收已有报告：第一个为确定性报告，其后必须是三轮真实模型报告",
    )
    # 退出码回答的是"有没有退步"，不是"是不是全绿"。已知的自然度欠账
    # 每天都在，让它天天把退出码染红，红色就不再有意义了。
    parser.add_argument(
        "--fail-on", choices=["high", "medium", "low", "none"], default="high",
        help="哪一档失败才让退出码非零（默认 high；low 表示任何失败都算）")
    args = parser.parse_args(argv)

    if args.list:
        for item in ALL_CASES:
            print(f"{item.case_id}\t{item.title}")
        return 0
    if args.release_check:
        try:
            reports = [_load_report(path) for path in args.release_check]
        except ValueError as exc:
            parser.error(str(exc))
        decision = release_decision(reports[0], reports[1:])
        print(json.dumps(decision, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if decision["status"] == "ready" else 1
    if args.judge and not args.real_model:
        parser.error("--judge requires --real-model")
    if args.repeat < 1 or args.repeat > 100:
        parser.error("--repeat must be between 1 and 100")
    try:
        cases = _selected_cases(args.case) * args.repeat
    except ValueError as exc:
        parser.error(str(exc))

    responder = None
    mode = "deterministic"
    model = ""
    temperature = None
    if args.real_model:
        enabled_value = str(os.environ.get("WHALE_LLM_ENABLED", "")).strip().lower()
        if enabled_value not in {"1", "true", "yes", "on"}:
            parser.error("--real-model requires WHALE_LLM_ENABLED to explicitly allow model use")
        try:
            config = ServiceConfig.from_env()
        except ConfigurationError as exc:
            parser.error(str(exc))
        responder = configured_responder(config)
        if responder is None or not config.provider.api_key or not config.provider.provider_id:
            parser.error(
                "--real-model requires a valid provider, base URL, model and WHALE_LLM_API_KEY"
            )
        mode = "real-model"
        model = config.provider.model
        temperature = config.provider.temperature

    judge = responder.judge_json if args.judge and responder is not None else None
    results = run_suite(cases, responder=responder, judge=judge, temperature=temperature)
    report = build_report(
        results,
        mode=mode,
        model=model,
        temperature=temperature,
        prompt_version=PROMPT_VERSION,
        include_replies=args.include_replies,
    )
    secret_values = (config.provider.api_key,) if args.real_model else ()
    mark_report_safety(report, sensitive_values=secret_values)
    print(render_text(report))

    if args.output:
        if not report["reportSafety"]["passed"]:
            print("报告安全扫描失败，未写入文件。", file=sys.stderr)
            return 2
        try:
            output = _safe_output_path(args.output)
        except ValueError as exc:
            parser.error(str(exc))
        output.write_text(to_json(report) + "\n", encoding="utf-8")
        print(f"\nJSON 报告：{output}")
    if args.compare:
        try:
            previous = json.loads(args.compare.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            print(f"无法读取对比基线：{exc}", file=sys.stderr)
            return 2
        print("\n" + compare(previous, report))
    if args.fail_on == "none":
        return 0
    # 按名字取，不按字典顺序取：报告的键序将来变了也不该悄悄改变退出码语义。
    ladder = ("high", "medium", "low")
    blocking_levels = ladder[:ladder.index(args.fail_on) + 1]
    counts = report["summary"]["failuresByPriority"]
    return 1 if any(counts.get(level, 0) for level in blocking_levels) else 0


if __name__ == "__main__":
    raise SystemExit(main())
