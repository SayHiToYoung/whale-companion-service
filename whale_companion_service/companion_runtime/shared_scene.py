from __future__ import annotations

import re
from ..dialogue_state import is_explicit_stop


_REACTION = re.compile(r"^(?:[？?]+|啊[？?]?|哈[？?]?|什么意思[？?]?|你说啥[？?]?)[。！!…\s]*$")
_SPEAK = re.compile(r"^(?:说话|你说|说点什么|讲点啥|陪我说说话)[。！!？?…\s]*$")
_REPAIR = re.compile(r"^(?:我问你(?:啊|呢)?|问你呢|你没懂|不是这个意思|你听我说)[。！!？?…\s]*$")
_QUESTION = re.compile(r"(?:[？?]$|为什么|怎么|多少|几点|哪[个里一]|(?:吗|呢)$)")
_MEMORY_CHECK = re.compile(r"(?:你(?:知道|记得).*(?:今天|刚才).*(?:干嘛|做了什么|做什么)|我今天干嘛了不)")


def build_shared_scene(user_text: str, conversation: list[dict]) -> dict:
    text = str(user_text or "").strip()
    previous = conversation[:-1] if conversation and conversation[-1].get("role") == "user" else conversation
    previous_user = next((str(x.get("text") or "") for x in reversed(previous) if x.get("role") == "user"), "")
    previous_assistant = next((str(x.get("text") or "") for x in reversed(previous) if x.get("role") == "assistant"), "")
    if is_explicit_stop(text):
        intent, referent = "boundary", "current_utterance"
    elif _REACTION.fullmatch(text) and previous_assistant:
        intent, referent = "reaction_to_companion", "previous_companion_turn"
    elif _MEMORY_CHECK.search(text):
        intent, referent = "memory_check", "shared_memory"
    elif _SPEAK.fullmatch(text):
        intent, referent = "prompt_to_speak", "shared_context"
    elif _REPAIR.fullmatch(text):
        intent, referent = "repair", "previous_user_turn"
    elif _QUESTION.search(text):
        intent, referent = "question", "current_utterance"
    elif len(text) <= 10 and previous_user:
        intent, referent = "continuation", "previous_topic"
    else:
        intent, referent = "statement", "current_utterance"
    return {
        "currentUtterance": text[:400], "previousUserTurn": previous_user[:400],
        "previousCompanionTurn": previous_assistant[:400], "intent": intent,
        "referent": referent, "topicAnchor": (previous_user if referent == "previous_topic" else text)[:400],
        "unresolvedTurn": previous_assistant[:400] if previous_assistant.endswith(("？", "?")) else "",
        "topicExhausted": all(re.fullmatch(r"(?:嗯+|对|是啊|确实|差不多)[。！!\s]*", value)
                              for value in (previous_user, text)),
        "recentQuestionCount": sum(bool(re.search(r"[？?]", str(x.get("text") or "")))
                                   for x in [r for r in previous if r.get("role") == "assistant"][-2:]),
    }
