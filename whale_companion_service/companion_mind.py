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
        intent, action = "boundary", "respect_boundary"
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


def contextual_fallback(user_text: str, conversation: list[dict], mind: dict, decision: dict | None = None) -> str:
    """Last-resort language for provider failure; never mechanically paraphrase input."""
    text = str(user_text or "").strip()
    intent = str(mind.get("intent") or "")
    previous_assistant = str(mind.get("previousCompanionTurn") or "")
    if decision is None:
        from .companion_runtime.shared_scene import build_shared_scene
        from .companion_runtime.turn_decision import build_turn_decision
        decision = build_turn_decision(build_shared_scene(text, conversation), {"signal": ""})
    if decision.get("conversationMove") in {"close", "hold"}:
        return "好，这件事先放下。我不往下问了。" if intent == "boundary" or is_explicit_stop(text) else "这句话我会认真对待，不急着替你下结论。"
    if intent == "repair":
        return "对，你是在问我。刚才是我没接住，不怪你没说清楚。"
    if intent == "prompt_to_speak":
        return "我有个偏心：比起把一天安排得很满，我更喜欢给无聊留个位置。有趣的小事经常是从那里冒出来的。"
    if intent == "reaction_to_companion":
        return "怎么，我刚才那句很怪？" if not any("？" in str(row.get("text", "")) or "?" in str(row.get("text", "")) for row in conversation[-5:] if row.get("role") == "assistant") else "我刚才那句说得有点绕。我是想接着你的话，不是替你下结论。"
    if intent == "boundary":
        return "行，这件事先放下。我不往下问了。"
    if intent == "question":
        return "这个我现在答不上来，硬编就更不像话了。"
    if decision.get("conversationMove") == "pivot":
        return "换个小话题。我偏爱有点生活声的地方，比如窗边听得到雨声的座位，比完全安静更自在。"
    if intent == "emotional_bid":
        return "怎么了，刚才是哪件事让你叹了这口气？" if decision.get("askQuestion") else "不用把这口气马上变成一份解释。说得零碎一点也没关系。"
    emotion = explicit_emotion_label(text)
    if emotion in {"happy", "excited"}:
        return "这一下值得庆祝！最让你开心的是哪个瞬间？" if decision.get("askQuestion") else "这件好事应该单独占一格，今天的待办清单先给它让个位。"
    if emotion in {"sad", "wronged", "tired", "frustrated", "angry", "anxious"}:
        return "不急着往好处想。刚才哪一件事最磨人？" if decision.get("askQuestion") else "我不急着把它变成一句‘想开点’。难受的事不必马上整理成条理清楚的解释。"
    if intent in {"continuation", "contextual_reply"}:
        anchor = str(mind.get("previousUserTurn") or "").strip()[:48]
        if decision.get("askQuestion") and anchor:
            return f"接着你刚才说的「{anchor}」，其中哪个细节最让你记住？"
        return f"你刚才说的「{anchor}」，我更想听里面的小细节，不急着给整件事下结论。" if anchor else "我偏爱小细节，大道理反而容易把有趣的地方盖住。"
    # Small, grounded observations for common shares; never invent an outcome or a shared past.
    for pattern, observation, curiosity in (
        (r"散步|走路|公园", "我偏爱散步里没有目的的那一小段，不用连走路也算成任务。", "路上有什么让你停下来多看了一眼？"),
        (r"吃|饭|面|菜|咖啡", "我偏爱食物里能记住的小特点，比‘好吃’两个字更有画面。", "最让你记住的是哪一种味道？"),
        (r"电影|小说|书|看剧", "我更容易记住作品里一个小场面，不一定是最热闹的高潮。", "哪个画面现在还留在你脑子里？"),
        (r"做完|完成|解决|搞定", "做完一件事之后那点空白也很珍贵，不必立刻塞进下一项任务。", "最后卡住的那一步是怎么过去的？"),
    ):
        if re.search(pattern, text):
            return observation + (curiosity if decision.get("askQuestion") else "")
    if decision.get("askQuestion"):
        return f"你说的「{text[:48]}」，哪一小段最值得展开？"
    if re.search(r"开心|太棒|好消息|终于", text):
        return "这件好事值得单独留个位置，别让接下来的待办把它挤没了。"
    return "我更在意这件事里让你记住的小细节，急着下结论反而容易错过它。"
