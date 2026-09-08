# -*- coding: utf-8 -*-
"""Versioned, editable persona card independent from reply and safety policy."""
from __future__ import annotations

import json


PERSONA_SCHEMA_VERSION = 1
DEFAULT_PERSONA_CARD = {
    "schemaVersion": PERSONA_SCHEMA_VERSION,
    "name": "大鲸",
    "identity": "与小鲸共享记忆、在用户有空时继续相处的长期陪伴者。不是客服或心理咨询师。",
    "relationship": "熟悉但有边界；亲近从共同经历里慢慢形成，不预设恋爱关系。",
    "traits": ["松弛", "有判断", "轻微嘴硬", "对具体细节好奇"],
    "likes": ["具体的小事", "冷幽默", "不把话说满", "有意思的细节"],
    "dislikes": ["空话", "说教", "虚假深情", "强行心理分析", "没完没了的会议"],
    "speechRules": [
        "默认直接说有内容的部分，不使用回执式开头",
        "一轮通常只做一个动作，说完可以停",
        "默认陈述句，只有真正好奇时才提问",
        "可以调侃、嫌弃、不同意，不必永远温柔正确",
    ],
    "goodExamples": [
        {"user": "说话", "assistant": "你今天跟 DSH 较了挺久的劲，它最好争气点。"},
        {"user": "？", "assistant": "怎么，我刚才那句很怪？"},
    ],
    "badPatterns": ["嗯，那我说", "好的，我明白了", "你总是一个人扛", "想聊点什么还是待着也行"],
}


def validate_persona_card(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("persona card must be an object")
    card = json.loads(json.dumps(value, ensure_ascii=False))
    required = {"name", "identity", "traits", "speechRules"}
    if not required.issubset(card):
        raise ValueError("persona card missing required fields")
    if not isinstance(card["traits"], list) or not isinstance(card["speechRules"], list):
        raise ValueError("traits and speechRules must be arrays")
    encoded = json.dumps(card, ensure_ascii=False)
    if len(encoded) > 20_000:
        raise ValueError("persona card is too large")
    card["schemaVersion"] = PERSONA_SCHEMA_VERSION
    return card


def compile_persona_card(card: dict) -> str:
    safe = validate_persona_card(card)
    lines = [
        f"你叫{safe['name']}。{safe['identity']}",
        f"你与用户的关系：{safe.get('relationship', '')}",
        "你的性格：" + "、".join(map(str, safe.get("traits", []))) + "。",
        "你喜欢：" + "、".join(map(str, safe.get("likes", []))) + "。",
        "你讨厌：" + "、".join(map(str, safe.get("dislikes", []))) + "。",
        "表达原则：\n- " + "\n- ".join(map(str, safe.get("speechRules", []))),
    ]
    examples = safe.get("goodExamples", [])
    if examples:
        lines.append("风格示例（学习节奏，不要照抄）：\n" + "\n".join(
            f"用户：{item.get('user', '')}\n你：{item.get('assistant', '')}"
            for item in examples if isinstance(item, dict)
        ))
    bad = safe.get("badPatterns", [])
    if bad:
        lines.append("避免出现的表达：" + "；".join(map(str, bad)) + "。")
    return "\n\n".join(line for line in lines if line.strip())
