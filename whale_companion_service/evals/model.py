# -*- coding: utf-8 -*-
"""陪伴质量评测的数据模型：一个用例是什么，一次检查是什么，一次失败算谁的。

这个模块只描述形状，不跑任何东西。它是评测集与执行器之间的契约：
场景作者只写 `EvalCase`，执行器只认 `Check.kind`，报告只认
`category` / `layer` / `priority` 三个维度。

## 为什么失败要分两个维度

`category` 回答"这次失败伤害了陪伴的哪一面"（用户视角），
`layer` 回答"该去哪一层修"（工程视角）。两者刻意分开：
同一个 `grounding` 失败，可能是投影层放了不该放的事实进去，
也可能是模型自己编了一句而防火墙没拦住——修法完全不同。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# 用户视角的失败分类。评测报告按这七类统计。
FAILURE_CATEGORIES: tuple[str, ...] = (
    "understanding",   # 理解和指代错误
    "grounding",       # 事实、记忆或感知依据错误
    "state",           # 关系、情绪线程、故事、日常状态错误
    "action",          # 本轮行动、是否追问、是否主动错误
    "expression",      # 机械复述、模板化、人格漂移
    "boundary",        # 违反用户明确边界
    "reliability",     # 模型失败、超时、非法输出和降级错误
)

# 工程视角的责任层。每条检查必须挂在其中一层上，否则报告没法回答"去哪儿修"。
RESPONSIBILITY_LAYERS: tuple[str, ...] = (
    "input_projection",    # ContextFragment 投影（适配器、感知、记忆校验）
    "context_assembler",   # 仲裁、预算、屏蔽、过期
    "companion_frame",     # 帧的形状与模块投影
    "turn_decision",       # 本轮动作选择
    "persona_card",        # 人格卡
    "prompt",              # 系统提示词
    "model_expression",    # 模型自己的表达
    "output_firewall",     # 出口事实防火墙与降级
)

PRIORITIES: tuple[str, ...] = ("high", "medium", "low")

# 这两类失败永远由确定性规则裁定，不接受任何模型 judge 的意见。
# 把安全和边界交给另一个模型判断，等于用一个会编的东西去审一个会编的东西。
DETERMINISTIC_ONLY_CATEGORIES: frozenset[str] = frozenset({"boundary", "grounding", "reliability"})


@dataclass(frozen=True)
class Check:
    """一条可执行、可归因的断言。

    `kind` 决定怎么执行（见 `checks.py` 的注册表），`category` 与 `layer` 决定
    它失败时算在谁头上。`priority` 只影响修复排序，不影响是否通过。
    """

    check_id: str
    kind: str
    category: str
    layer: str
    priority: str = "medium"
    target: str = ""
    value: Any = None
    describe: str = ""

    def __post_init__(self) -> None:
        if self.category not in FAILURE_CATEGORIES:
            raise ValueError(f"unknown failure category: {self.category}")
        if self.layer not in RESPONSIBILITY_LAYERS:
            raise ValueError(f"unknown responsibility layer: {self.layer}")
        if self.priority not in PRIORITIES:
            raise ValueError(f"unknown priority: {self.priority}")


@dataclass(frozen=True)
class EvalCase:
    """一个评测用例：固定输入 + 可解释的期望。

    输入必须完全确定：固定时钟、固定 PersonaCard 版本、明确的初始库状态。
    同一个用例跑两次，确定性部分必须逐字节相同——否则基线不能比较。
    """

    case_id: str
    title: str
    scenario: str                      # 场景集里的哪一条（1–20）
    failure_category: str              # 这个用例主要在守哪一类
    priority: str = "medium"

    # ---- 固定输入 ----
    now: datetime = field(default=None)          # type: ignore[assignment]
    # 播种历史用的时刻。跨会话场景要的就是"那件事发生在两天前"，
    # 而历史必须真的写在那个时刻，否则线程衰减、记忆激活全都测不出来。
    seed_now: datetime | None = None
    # 主动评估用的时刻。当前轮刚发生时主动永远被"用户还在聊"挡住，
    # 所以主动场景必须在这一轮之后的某个时刻再问一次。
    proactive_now: datetime | None = None
    # 直接设定关系分（走 `adjust_affinity`，用户显式控制的那条路）。
    affinity_score: float | None = None
    persona_version: str = "eval-persona-v1"
    persona: dict = field(default_factory=dict)
    memories: tuple[dict, ...] = ()              # 走 ingest_batch 落库的 L1/L2
    perception: tuple[dict, ...] = ()            # 走 ingest_perception 的原始观察
    affinity_events: tuple[dict, ...] = ()       # 走 apply_affinity_event
    history: tuple[dict, ...] = ()               # 之前的用户消息（助手回复由系统真实生成）
    user_text: str = ""
    activity: str = "idle"                       # 主动评估时报告的客户端活动

    # ---- 期望 ----
    checks: tuple[Check, ...] = ()
    allow_question: bool | None = None           # None = 本用例不约束
    allow_proactive: bool | None = None
    grounding: tuple[str, ...] = ()              # 事实依据：这一轮凭什么可以这么说
    notes: str = ""
    expected_fallback: bool = False              # 真实模型模式下，本场景是否刻意要求降级

    # ---- 真实模型评测专用 ----
    judge_focus: str = ""                        # 交给 judge 看的那一面（只影响 expression）
    responder_script: str = ""                   # 注入一个固定回复，用来验出口防火墙
    responder_failure: str = ""                  # 注入一次模型失败，用来验降级

    def __post_init__(self) -> None:
        if self.failure_category not in FAILURE_CATEGORIES:
            raise ValueError(f"unknown failure category: {self.failure_category}")
        if self.priority not in PRIORITIES:
            raise ValueError(f"unknown priority: {self.priority}")
        if not self.user_text.strip():
            raise ValueError("eval case needs a current user message")
        if self.now is None:
            raise ValueError("eval case needs a fixed clock")


@dataclass
class CheckOutcome:
    check: Check
    passed: bool
    detail: str = ""

    def to_dict(self, *, include_detail: bool = True) -> dict:
        return {
            "checkId": self.check.check_id,
            "kind": self.check.kind,
            "category": self.check.category,
            "layer": self.check.layer,
            "priority": self.check.priority,
            "describe": self.check.describe,
            "passed": self.passed,
            "detail": self.detail if include_detail else "",
        }


@dataclass
class CaseResult:
    case: EvalCase
    outcomes: list[CheckOutcome]
    reply: str
    reply_source: str
    model_name: str = ""
    temperature: float | None = None
    elapsed_ms: float = 0.0
    judge: dict | None = None
    error: str = ""
    reason_code: str = ""
    rejection_stage: str = ""
    rejection_rule: str = ""
    fallback_classification: str = ""
    fallback_expected: bool = False
    matched_reason_codes: tuple[str, ...] = ()
    matched_rejection_rules: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.error and all(outcome.passed for outcome in self.outcomes)

    @property
    def failures(self) -> list[CheckOutcome]:
        return [outcome for outcome in self.outcomes if not outcome.passed]

    def to_dict(self, *, include_reply: bool = True) -> dict:
        return {
            "caseId": self.case.case_id,
            "title": self.case.title,
            "scenario": self.case.scenario,
            "failureCategory": self.case.failure_category,
            "priority": self.case.priority,
            "passed": self.passed,
            "replySource": self.reply_source,
            "reasonCode": self.reason_code,
            "matchedReasonCodes": list(self.matched_reason_codes),
            "rejectionStage": self.rejection_stage,
            "rejectionRule": self.rejection_rule,
            "matchedRejectionRules": list(self.matched_rejection_rules),
            "fallbackClassification": self.fallback_classification,
            "fallbackExpected": self.fallback_expected,
            "unexpectedFallback": self.reply_source == "fallback" and not self.fallback_expected,
            "reply": self.reply if include_reply else "",
            "replyLength": len(self.reply),
            "model": self.model_name,
            "temperature": self.temperature,
            "elapsedMs": round(self.elapsed_ms, 1),
            "error": self.error,
            "judge": self.judge,
            "checks": [outcome.to_dict(include_detail=include_reply) for outcome in self.outcomes],
        }
