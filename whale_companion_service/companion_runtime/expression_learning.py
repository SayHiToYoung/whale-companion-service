# -*- coding: utf-8 -*-
"""大鲸的表达学习：从对话学「怎么说」，不学「是什么」。

从 astrbot_plugin_private_companion 的 expression 学习引擎提取，按本项目哲学精简：

1. 只学短表达、句法、情境风格（「怎么说」）；**不学**昵称、账号、关系事实、秘密、长句
   （「是什么」）。这两类是不同数据：关系事实进记忆，表达学习只保存说法。
2. 学习是保守的：只从「短句、非提问、非事实」里提取候选，且默认 pending，需审核通过
   才进入召回。审核不过不进回复。
3. 纯确定性：场景判定与召回打分均不调用 LLM。真正「怎么说」仍由主 LLM 按召回规则落地。

存储由调用方持有（expression_rules 表），本模块是纯函数。
"""

from __future__ import annotations

import re

# 场景分类：决定一条表达规则在什么情境下可召回。
SCENES = ("casual", "praise", "complaint", "concern", "play")

# 表达规则状态。
RULE_STATUSES = ("pending", "approved", "rejected")

# 学习边界：候选句最长 12 字、最短 2 字；超过就不是「短表达」。
MAX_CANDIDATE_LEN = 12
MIN_CANDIDATE_LEN = 2

# 黑名单：命中就不学（账户/密钥/号码/事实类/长句）。
_SENSITIVE = re.compile(r"(?:sk-|api[_-]?key|token|密码|账号|手机号|电话|\d{4,}|@)")
_QUESTION = re.compile(r"[？?]$|为什么|怎么|多少|几点|哪[个里一]|吗$|呢$")
# 事实句特征：主语+是/叫/姓 之类，属于「是什么」而非「怎么说」。
_FACT_STATEMENT = re.compile(r"(?:我|你|他|她)(?:是|叫|姓|住|在|有|喜欢|讨厌|做|工作|读)")


def scene_of(user_text: str) -> str:
    """按关键词判定情境。"""
    text = str(user_text or "").strip()
    if re.search(r"谢谢|辛苦|有你在|真好|喜欢你|想你|抱抱|好棒|太会了|厉害|你懂我", text):
        return "praise"
    if re.search(r"烦|累|困|难受|生气|气死|无语|崩溃|麻了|不想", text):
        return "complaint"
    if re.search(r"我好|我最近|我有点|我很难|我担心|怕", text):
        return "concern"
    if re.search(r"哈|哈哈|笑死|乐|冲|开摆|整活|玩", text):
        return "play"
    return "casual"


def is_learnable(user_text: str) -> bool:
    """这条消息是否可能成为表达学习素材（保守）。"""
    text = str(user_text or "").strip()
    if not (MIN_CANDIDATE_LEN <= len(text) <= MAX_CANDIDATE_LEN):
        return False
    if _SENSITIVE.search(text):
        return False
    if _QUESTION.search(text):
        return False
    if _FACT_STATEMENT.search(text):
        return False
    return True


def learn_candidates(user_text: str, scene: str | None = None, known_patterns: set[str] | None = None) -> list[dict]:
    """从一条用户消息提取表达候选（默认 pending）。

    保守策略：整句只作「情境表达」候选，命中黑名单/提问/事实句则跳过；
    已学过的 pattern 不重复产出。
    """
    text = str(user_text or "").strip()
    if not is_learnable(text):
        return []
    known = set(known_patterns or ())
    if text in known:
        return []
    return [{
        "pattern": text,
        "instruction": f"学着用「{text}」这种说法",
        "scene": scene or scene_of(text),
        "kind": "style",
        "status": "pending",
        "usageCount": 0,
    }]


def review_rule(rule: dict, decision: str) -> dict:
    """审核：approved 才可召回，rejected 丢弃，其余保持 pending。"""
    if decision not in RULE_STATUSES:
        raise ValueError(f"invalid decision: {decision}")
    updated = dict(rule)
    updated["status"] = decision
    return updated


def recall_rules(scene: str, rules: list[dict], limit: int = 3) -> list[dict]:
    """按场景召回已审核通过的规则：场景命中优先，其余按使用次数降序。"""
    approved = [dict(r) for r in rules if isinstance(r, dict) and r.get("status") == "approved"]
    in_scene = [r for r in approved if str(r.get("scene") or "casual") == scene]
    others = [r for r in approved if r not in in_scene]
    in_scene.sort(key=lambda r: -int(r.get("usageCount") or 0))
    others.sort(key=lambda r: -int(r.get("usageCount") or 0))
    picked = in_scene + others
    return picked[: max(0, limit)]


def expression_context(recalled: list[dict]) -> dict:
    """给模型看的形态：只给说法，附「别学关系事实」约束。"""
    rows = []
    for rule in recalled:
        rows.append({
            "pattern": str(rule.get("pattern") or "")[:40],
            "instruction": str(rule.get("instruction") or "")[:80],
            "scene": str(rule.get("scene") or "casual"),
        })
    return {
        "usage": "这些是你学到的「说法」，可以自然带出来，但只是语气参考，不是必须逐字复述，也不代表任何关系事实。",
        "rules": rows,
        "empty": not rows,
    }


def judge_expression_candidate(user_text: str, judge=None):
    """LLM 判定这句话值不值得学成「说法」（心脏）；judge 为 None/失败回退到保守规则。

    返回 (candidates, source)。candidates 结构同 learn_candidates。
    """
    fallback = learn_candidates(user_text)
    if judge is None:
        return fallback, "fallback"
    try:
        result, source = judge(
            task="判断这句话是不是一个可以学的「说法」（短表达、口头禅、语气）",
            context={"text": str(user_text or "")[:200]},
            fields={
                "learnable": "布尔：是否是短小、有个人风格、值得学的说法",
                "pattern": "如果要学，学哪句（照抄或提炼，不超过12字）",
                "scene": "情境：casual/praise/complaint/concern/play 之一",
                "instruction": "一句话说明怎么用这个说法",
            },
            fallback={
                "learnable": bool(fallback),
                "pattern": fallback[0]["pattern"] if fallback else "",
                "scene": fallback[0]["scene"] if fallback else "casual",
                "instruction": fallback[0]["instruction"] if fallback else "",
            },
        )
    except Exception:
        return fallback, "fallback"
    if result.get("learnable") is False:
        return [], source
    pattern = str(result.get("pattern") or "").strip()
    if not pattern or len(pattern) > MAX_CANDIDATE_LEN:
        return fallback, source
    return [{
        "pattern": pattern,
        "instruction": str(result.get("instruction") or f"学着用「{pattern}」这种说法"),
        "scene": str(result.get("scene") or "casual"),
        "kind": "style",
        "status": "pending",
        "usageCount": 0,
    }], source
