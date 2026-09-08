# -*- coding: utf-8 -*-
"""Short-lived interpretation and action selection for Big Whale."""
from __future__ import annotations

import re
from datetime import datetime

from .dialogue_state import is_emotional_bid, is_explicit_stop
from .emotion import explicit_emotion_label


MIND_VERSION = "companion-mind-v1"
_REPAIR = re.compile(r"^(?:我问你(?:啊|呢)?|问你呢|你没懂|不是这个意思|你听我说)[。！!？?…\s]*$")
_QUESTION = re.compile(r"(?:[？?]$|^(?:你|那你).*(?:吗|呢|不)$|为什么|怎么|多少|几点|哪[个里一])")
_VAGUE = re.compile(r"^(?:算了|然后呢|就这样|那个|这个|是吗|真的|行吧|好吧|没了|对啊|不是吧)[。！!？?…\s]*$")
_PROMPT_TO_SPEAK = re.compile(r"^(?:说话|你说|说点什么|讲点啥|陪我说说话)[。！!？?…\s]*$")
_REACTION = re.compile(r"^(?:[？?]+|啊[？?]?|哈[？?]?|什么意思[？?]?|你说啥[？?]?)[。！!…\s]*$")


def _recent(conversation: list[dict], role: str, *, exclude_last: bool = False) -> str:
    rows = conversation[:-1] if exclude_last else conversation
    return next((
        str(item.get("text") or "").strip() for item in reversed(rows)
        if isinstance(item, dict) and item.get("role") == role and str(item.get("text") or "").strip()
    ), "")


def build_companion_mind(user_text: str, conversation: list[dict]) -> dict:
    """Interpret the turn before selecting language; emotion is only one signal."""
    text = str(user_text or "").strip()
    previous_user = _recent(conversation, "user", exclude_last=True)
    previous_assistant = _recent(conversation, "assistant", exclude_last=True)
    emotion = explicit_emotion_label(text)
    if _REACTION.fullmatch(text) and previous_assistant:
        intent, action = "reaction_to_companion", "respond_to_own_previous_turn"
    elif _PROMPT_TO_SPEAK.fullmatch(text):
        intent, action = "prompt_to_speak", "volunteer_a_thought"
    elif _REPAIR.fullmatch(text):
        intent, action = "repair", "repair_and_answer_previous"
    elif is_explicit_stop(text):
        intent, action = "boundary", "respect_or_lightly_pivot"
    elif is_emotional_bid(text):
        intent, action = "emotional_bid", "lean_in"
    elif _QUESTION.search(text):
        intent, action = "question", "answer_directly"
    elif emotion:
        intent, action = "self_disclosure", "respond_to_content_and_emotion"
    elif _VAGUE.fullmatch(text) and previous_assistant:
        intent, action = "contextual_reply", "continue_previous_exchange"
    elif len(text) <= 8 and previous_user:
        intent, action = "continuation", "interpret_from_recent_topic"
    else:
        intent, action = "statement", "offer_personal_reaction"

    hour = datetime.now().hour
    scene = "late_night" if hour < 6 else "morning" if hour < 11 else "daytime" if hour < 18 else "evening"
    topic_anchor = text if len(text) > 8 else (previous_user or text)
    return {
        "version": MIND_VERSION,
        "scene": scene,
        "intent": intent,
        "action": action,
        "emotionSignal": emotion,
        "currentUtterance": text[:300],
        "previousUserTurn": previous_user[:300],
        "previousCompanionTurn": previous_assistant[:300],
        "topicAnchor": topic_anchor[:300],
        "needsClarification": intent == "continuation" and not previous_user,
    }


def contextual_fallback(user_text: str, conversation: list[dict], mind: dict) -> str:
    """Last-resort language for provider failure; never mechanically paraphrase input."""
    text = str(user_text or "").strip()
    intent = str(mind.get("intent") or "")
    previous_assistant = str(mind.get("previousCompanionTurn") or "")
    if intent == "repair":
        return "对，你是在问我。刚才是我没接住，不怪你没说清楚。"
    if intent == "prompt_to_speak":
        return "你今天和 DSH 较上劲的时间，比留给我的多多了。"
    if intent == "reaction_to_companion":
        return "怎么，我刚才那句很怪？"
    if intent == "boundary":
        return "行，到这儿。换个别的也可以。"
    if intent == "contextual_reply" and previous_assistant:
        if text.startswith("算了"):
            return "行，那一页先合上。"
        return "嗯，这句我接到了。"
    if intent == "question":
        return "这个我现在答不上来，硬编就更不像话了。"
    if intent == "continuation":
        return "等下——你是在接刚才那件事，对吧？"
    return "哦，这句有点意思。"
