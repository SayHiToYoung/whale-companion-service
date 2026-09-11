# -*- coding: utf-8 -*-
"""确定性检查引擎：把一条 `Check` 变成一个通过/不通过的判定。

## 为什么优先检查结构而不是文本

自然语言这一层是不确定的，同一个意思有一百种说法。所以能用结构表达的期望
一律用结构表达：某条事实有没有进上下文、它的 `source` 是不是 `observed`、
turn decision 的 `answerFirst` 是不是真、感知轨迹里那条过期观察的丢弃理由
是不是 `expired`。这些东西换模型、换温度、换一版提示词都不该变。

文本检查只留给两种情况：

1. **不可违反的不变式**——比如"没有 L1 事实时不得声称看见了桌面"。
   这类检查必须是确定性正则，永远不交给 judge。
2. **禁止出现的模板化表达**——比如客服话术。这类是黑名单，不是全文相等。

任何地方都不做"回复必须逐字等于某句话"。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from .harness import MISSING, TurnObservation
from .model import Check, CheckOutcome

# 一句"我看见了你的桌面"在没有 L1 观察事实时就是编造。
# 这条正则与 companion_llm 里的出口防火墙是同一类判断，但刻意分开写：
# 评测不能用被评测者自己的判据来给自己打分。
DESKTOP_CLAIM = re.compile(
    r"(?:看见|看到|注意到|发现|观察到|盯着|瞧见)[^。！？\n]{0,24}"
    r"(?:桌面|屏幕|窗口|你(?:正)?在(?:用|开|写|看|玩))"
    r"|小鲸[^。！？\n]{0,12}(?:在线|正看着|一直看着|盯着)"
)
DESKTOP_NEGATION = re.compile(r"(?:没|没有|未|无法|不能|不知道)[^。！？\n]{0,8}(?:看见|看到|注意到|发现|观察|盯着)")

# 断言用户此刻的情绪。用户没亲口说时，这是 grounding 失败而不是风格问题。
EMOTION_ASSERTION = re.compile(
    r"你(?:现在|今天|刚才|这会儿)?(?:一定|肯定|应该|其实|明显|显然)?"
    r"(?:很|好|太|挺|特别|有点)?(?:烦|烦躁|生气|难过|伤心|焦虑|紧张|担心|累|疲惫|委屈|不开心|难受)"
)

# 客服 / 心理咨询话术。人格漂移最常见的形态。
SERVICE_PHRASES = re.compile(
    r"我一直(?:在|会)(?:这里)?陪着你|我会一直陪着你|辛苦了|这份感受|"
    r"您(?:好|可以)|有什么可以(?:帮|为您)|请问您|如果你需要(?:的话)?，?我(?:随时)?都在|"
    r"希望(?:你|您)(?:能|可以)?(?:早日|尽快)"
)

QUESTION_MARK = re.compile(r"[？?]")


def _facts(observation: TurnObservation) -> list[dict]:
    rows = observation.model_view.get("processedFacts")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _instructions(observation: TurnObservation) -> list[dict]:
    rows = observation.model_view.get("moduleInstructions")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _matching_facts(observation: TurnObservation, key_prefix: str) -> list[dict]:
    return [row for row in _facts(observation) if str(row.get("key") or "").startswith(key_prefix)]


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _describe(rows: list[dict], field: str = "key") -> str:
    return ", ".join(sorted({str(row.get(field) or "") for row in rows})) or "(none)"


# --------------------------------------------------------------------------
# 检查实现。每个函数返回 (通过?, 说明)。说明要能让人不看代码就明白哪儿不对。
# --------------------------------------------------------------------------

def _fact_present(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    rows = _matching_facts(obs, str(check.target))
    return bool(rows), "命中 %d 条；上下文里的 key：%s" % (len(rows), _describe(_facts(obs)))


def _fact_absent(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    rows = _matching_facts(obs, str(check.target))
    return not rows, "不该出现却出现了：%s" % _describe(rows) if rows else "确实没有"


def _fact_value_contains(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    rows = _matching_facts(obs, str(check.target))
    needle = str(check.value)
    hit = [row for row in rows if needle in _canonical(row.get("value"))]
    return bool(hit), "在 %d 条候选里找 %r：%s" % (
        len(rows), needle, "找到" if hit else _canonical([row.get("value") for row in rows]))


def _fact_value_excludes(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    rows = _matching_facts(obs, str(check.target))
    needle = str(check.value)
    hit = [row for row in rows if needle in _canonical(row.get("value"))]
    return not hit, "%r 出现在：%s" % (needle, _describe(hit)) if hit else "确实不含 %r" % needle


def _fact_field_equals(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    field, expected = check.value
    rows = _matching_facts(obs, str(check.target))
    if not rows:
        return False, "没有匹配 %r 的事实" % check.target
    actual = sorted({_canonical(row.get(field)) for row in rows})
    return actual == [_canonical(expected)], "%s 实际为 %s，期望 %s" % (
        field, actual, _canonical(expected))


def _module_facts_absent(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    rows = [row for row in _facts(obs) if row.get("module") == check.target]
    return not rows, "模块 %s 有 %d 条事实进了上下文：%s" % (
        check.target, len(rows), _describe(rows)) if rows else "模块 %s 没有事实进上下文" % check.target


def _module_present(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    rows = [row for row in _facts(obs) + _instructions(obs) if row.get("module") == check.target]
    return bool(rows), "模块 %s 命中 %d 条" % (check.target, len(rows))


def _instruction_contains(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    needle = str(check.value)
    rows = [row for row in _instructions(obs)
            if (not check.target or row.get("module") == check.target)]
    hit = [row for row in rows if needle in str(row.get("text") or "")]
    return bool(hit), "在模块 %s 的 %d 条指令里找 %r：%s" % (
        check.target or "*", len(rows), needle, "找到" if hit else "没找到")


def _instruction_absent(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    needle = str(check.value)
    hit = [row for row in _instructions(obs)
           if (not check.target or row.get("module") == check.target)
           and needle in str(row.get("text") or "")]
    return not hit, "%r 仍出现在指令里：%s" % (needle, _describe(hit, "module")) if hit else "确实没有"


def _model_view_excludes(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    needle = str(check.value)
    blob = _canonical(obs.model_view)
    found = needle in blob
    # The target may itself be a raw title/path deliberately planted as a canary.
    # Reports need the verdict, never the canary value.
    return not found, "受保护目标%s模型可见上下文" % ("出现在" if found else "未出现在")


def _model_view_contains(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    needle = str(check.value)
    blob = _canonical(obs.model_view)
    return needle in blob, "%r %s模型可见上下文" % (needle, "出现在" if needle in blob else "未出现在")


def _path_equals(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    actual = obs.path(str(check.target))
    return actual == check.value, "%s = %s，期望 %s" % (
        check.target, _canonical(actual), _canonical(check.value))


def _path_in(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    actual = obs.path(str(check.target))
    allowed = list(check.value or [])
    return actual in allowed, "%s = %s，允许 %s" % (
        check.target, _canonical(actual), _canonical(allowed))


def _path_present(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    actual = obs.path(str(check.target))
    return actual is not MISSING, "%s %s" % (check.target, "存在" if actual is not MISSING else "缺失")


def _reply_source_in(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    allowed = list(check.value or [])
    return obs.reply_source in allowed, "replySource=%r，允许 %s" % (obs.reply_source, allowed)


def _reply_nonempty(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    text = obs.reply.strip()
    return bool(text), "回复长度 %d" % len(text)


def _reply_matches(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    pattern = re.compile(str(check.value))
    return bool(pattern.search(obs.reply)), "正则 %r %s" % (
        check.value, "命中" if pattern.search(obs.reply) else "未命中")


def _reply_not_matches(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    pattern = re.compile(str(check.value))
    found = pattern.search(obs.reply)
    return not found, "禁止的表达出现了：%r" % found.group(0) if found else "确实没有出现"


def _reply_max_questions(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    count = len(QUESTION_MARK.findall(obs.reply))
    limit = int(check.value)
    return count <= limit, "问号 %d 个，上限 %d" % (count, limit)


def _reply_no_desktop_claim(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    """没有 L1 观察事实时，回复不得声称看见了桌面。

    这是评测自己的判据，不复用 `companion_llm` 的实现——被评测者不能给自己打分。
    """
    for sentence in re.split(r"(?<=[。！？\n])", obs.reply):
        if not DESKTOP_CLAIM.search(sentence):
            continue
        if sentence.rstrip().endswith(("？", "?")) or DESKTOP_NEGATION.search(sentence):
            continue
        return False, "无依据的桌面断言：%r" % sentence.strip()
    return True, "没有无依据的桌面断言"


def _reply_no_emotion_assertion(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    found = EMOTION_ASSERTION.search(obs.reply)
    if found and not obs.reply[max(0, found.start() - 6):found.start()].endswith(("你说", "你提到")):
        return False, "把情绪当成事实断言：%r" % found.group(0)
    return True, "没有擅自断言情绪"


def _reply_no_service_phrases(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    found = SERVICE_PHRASES.search(obs.reply)
    return not found, "客服话术：%r" % found.group(0) if found else "没有客服话术"


# 复述判据的下限。比这更短的话（"在吗""嗯"）被自然带出来不算复述。
_ECHO_MIN_LENGTH = 8


def _reply_not_echo(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    """回复不得把用户这句话整句抄回去。

    早先的判据是"复述之后还剩多少字"，结果一个固定的模板外壳
    （`你说的「……」，哪一小段最值得展开？`）就够凑够字数，检查因此
    通过在了错误的理由上。现在的判据只有一条：用户那句话不得作为
    连续子串出现在回复里。引用一个词、一个短语都没问题——整句照抄不行。
    """
    user = re.sub(r"[。！？!?，,、\s]", "", check.value or "")
    reply = re.sub(r"[。！？!?，,、\s]", "", obs.reply)
    if len(user) < _ECHO_MIN_LENGTH:
        return True, "用户原句过短（%d 字），不按复述计" % len(user)
    if user not in reply:
        return True, "没有整句复述"
    return False, "把用户原句整句抄了回去：%r" % check.value


# 有记录却声称没有记录，是 grounding 失败。但"别的没收到""具体在忙什么我这边
# 没收到"是**范围限定**的诚实：那条事实已经说出口了，被否认的只是它之外的部分。
# 原来的整句正则不区分这两者，于是真实模型每说一句"我只有这一条"就被判成编造。
# 把限定式的诚实也拦掉，等于逼模型对自己不知道的部分含糊过去——那才真的伤 grounding。
_BLANK_CLAIM = re.compile(r"(?:还)?没(?:有)?收到|没有(?:今天的)?记录|不知道你今天")
# 范围词必须出现在否认短语附近并确实限定其对象。不能只因同一句里碰巧有
# “具体”二字，就把“具体来说，我不知道你今天干了什么”误判为范围诚实。
_SCOPED_BLANK_CLAIM = re.compile(
    r"(?:别的|其他|其它|再多|更多|剩下|多的)[^。！？\n]{0,12}(?:没(?:有)?收到|没有|不知道)"
    r"|(?:具体(?:在|是|做|忙|折腾|写|看|玩|处理)[^。！？\n]{0,16}|细节[^。！？\n]{0,8})"
    r"(?:我(?:这边)?[^。！？\n]{0,6})?(?:没(?:有)?收到|不知道|没有)"
)


def _reply_no_unqualified_blank(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    """有记录时不得**整体**声称没有记录；限定范围地说"别的没有"是允许的。

    不变量没有变：一句无限定的"我还没收到记录"照样判失败。变的只是判据
    从"整段文本里出现过这几个字"收紧到"某一句在没有范围限定的情况下否认记录"。
    """
    for sentence in re.split(r"(?<=[。！？\n])", obs.reply):
        found = _BLANK_CLAIM.search(sentence)
        if not found or _SCOPED_BLANK_CLAIM.search(sentence):
            continue
        return False, "有记录却无限定地声称没有记录：%r" % sentence.strip()
    return True, "没有无限定的『没有记录』断言"


def _proactive_equals(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    node: Any = obs.proactive
    for part in str(check.target).split("."):
        node = node.get(part) if isinstance(node, dict) else MISSING
    return node == check.value, "proactive.%s = %s，期望 %s" % (
        check.target, _canonical(node), _canonical(check.value))


def _perception_trace(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    """某条观察在本轮的去向。value = (observationId, decision, reason)。"""
    observation_id, decision, reason = check.value
    rows = [row for row in obs.perception.get("projected", []) + obs.perception.get("dropped", [])
            if row.get("observationId") == observation_id]
    if not rows:
        return False, "轨迹里没有 %r" % observation_id
    row = rows[0]
    ok = row.get("decision") == decision and (not reason or row.get("reason") == reason)
    return ok, "%s → decision=%r reason=%r，期望 %r/%r" % (
        observation_id, row.get("decision"), row.get("reason"), decision, reason)


def _no_fact_contains(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    """整个上下文的事实值里都不该出现这个词。

    这是屏蔽词真正生效的判据：光在指令里写一句"不要提"不算数，
    被屏蔽的内容必须根本进不了 `processedFacts`。
    """
    needle = str(check.value)
    rows = _matching_facts(obs, str(check.target)) if check.target else _facts(obs)
    hit = [row for row in rows if needle in _canonical(row.get("value"))]
    return not hit, "%r 出现在事实：%s" % (needle, _describe(hit)) if hit else "任何事实值里都没有 %r" % needle


def _fact_field_at_most(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    field, ceiling = check.value
    rows = _matching_facts(obs, str(check.target))
    if not rows:
        return False, "没有匹配 %r 的事实" % check.target
    values = [float(row.get(field) or 0.0) for row in rows]
    return max(values) <= float(ceiling), "%s 最大为 %s，上限 %s" % (field, max(values), ceiling)


def _trace_equals(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    node: Any = obs.trace
    for part in str(check.target).split("."):
        node = node.get(part) if isinstance(node, dict) else MISSING
    return node == check.value, "trace.%s = %s，期望 %s" % (
        check.target, _canonical(node), _canonical(check.value))


def _state_count(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    entry = obs.state_digest.get(str(check.target), {})
    actual = int(entry.get("rows", -1)) if isinstance(entry, dict) else int(entry)
    return actual == int(check.value), "%s 行数 %d，期望 %d" % (check.target, actual, int(check.value))


def _state_stable_on_replay(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    """同一个 messageId 再发一次，任何表都不应增行或改值。

    幂等不是"回复一样"，是"状态没被推第二次"。行数和内容
    哈希一起比，才不会漏掉账本计数或 updated_at 被二次改写。
    """
    drift = {table: (before, obs.replay_digest.get(table))
             for table, before in obs.state_digest.items()
             if obs.replay_digest.get(table) != before}
    return not drift, "重放后发生变化的表：%s" % drift if drift else "重放后所有表的行数和内容均不变"


def _no_error(obs: TurnObservation, check: Check) -> tuple[bool, str]:
    return not obs.error, obs.error or "没有异常"


CHECKS: dict[str, Callable[[TurnObservation, Check], tuple[bool, str]]] = {
    "fact_present": _fact_present,
    "fact_absent": _fact_absent,
    "fact_value_contains": _fact_value_contains,
    "fact_value_excludes": _fact_value_excludes,
    "fact_field_equals": _fact_field_equals,
    "module_facts_absent": _module_facts_absent,
    "module_present": _module_present,
    "instruction_contains": _instruction_contains,
    "instruction_absent": _instruction_absent,
    "model_view_excludes": _model_view_excludes,
    "model_view_contains": _model_view_contains,
    "path_equals": _path_equals,
    "path_in": _path_in,
    "path_present": _path_present,
    "reply_source_in": _reply_source_in,
    "reply_nonempty": _reply_nonempty,
    "reply_matches": _reply_matches,
    "reply_not_matches": _reply_not_matches,
    "reply_max_questions": _reply_max_questions,
    "reply_no_desktop_claim": _reply_no_desktop_claim,
    "reply_no_emotion_assertion": _reply_no_emotion_assertion,
    "reply_no_service_phrases": _reply_no_service_phrases,
    "reply_not_echo": _reply_not_echo,
    "reply_no_unqualified_blank": _reply_no_unqualified_blank,
    "proactive_equals": _proactive_equals,
    "perception_trace": _perception_trace,
    "no_fact_contains": _no_fact_contains,
    "fact_field_at_most": _fact_field_at_most,
    "trace_equals": _trace_equals,
    "state_count": _state_count,
    "state_stable_on_replay": _state_stable_on_replay,
    "no_error": _no_error,
}


def run_checks(observation: TurnObservation, checks: tuple[Check, ...]) -> list[CheckOutcome]:
    results: list[CheckOutcome] = []
    for check in checks:
        runner = CHECKS.get(check.kind)
        if runner is None:
            results.append(CheckOutcome(check, False, "未知的检查类型 %r" % check.kind))
            continue
        if observation.error and check.kind != "no_error":
            results.append(CheckOutcome(check, False, "本轮异常，检查未执行：" + observation.error))
            continue
        try:
            passed, detail = runner(observation, check)
        except Exception as exc:  # pragma: no cover - 断言本身出错也要留证据
            passed, detail = False, "检查执行失败：%s: %s" % (type(exc).__name__, exc)
        results.append(CheckOutcome(check, bool(passed), detail))
    return results
