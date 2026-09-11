"""Deterministic and opt-in model evaluations for companion quality."""

from .model import EvalCase
from .runner import build_report, run_suite
from .scenarios import ALL_CASES

__all__ = ["ALL_CASES", "EvalCase", "build_report", "run_suite"]
