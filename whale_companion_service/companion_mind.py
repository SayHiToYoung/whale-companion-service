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


def contextual_fallback(
    user_text: str,
    conversation: list[dict],
    mind: dict,
    decision: dict | None = None,
    frame: dict | None = None,
) -> str:
    """Last-resort language using the same mind, decision and frame as the model path."""
    text = str(user_text or "").strip()
    intent = str(mind.get("intent") or "")
    previous_assistant = str(mind.get("previousCompanionTurn") or "")
    context = frame if isinstance(frame, dict) else {}
    if decision is None:
        from .companion_runtime.shared_scene import build_shared_scene
        from .companion_runtime.turn_decision import build_turn_decision
        decision = build_turn_decision(build_shared_scene(text, conversation), {"signal": ""})
    if decision.get("conversationMove") in {"close", "hold"}:
        if intent == "boundary" or is_explicit_stop(text):
            return "好，这件事先放下。我不往下问了。"
        if re.search(r"交上|交出|提交|做完|完成", text):
            return "交出去就好。先到这里，不急着马上复盘。"
        if re.search(r"有空|找点事|轻松|放松", text):
            return "那就给这段空闲留点余地，不必连休息也排成任务。"
        if re.search(r"去忙|先忙|忙了", text):
            return "去吧，先顾手头的事。我不在后面追着问。"
        return "我把这句话放在心里，先不替你解释。"
    if intent == "repair":
        return "对，你是在问我。刚才是我没接住，不怪你没说清楚。"
    if intent == "prompt_to_speak":
        return "我有个偏心：比起把一天安排得很满，我更喜欢给无聊留个位置。有趣的小事经常是从那里冒出来的。"
    if intent == "reaction_to_companion":
        return "怎么，我刚才那句很怪？" if not any("？" in str(row.get("text", "")) or "?" in str(row.get("text", "")) for row in conversation[-5:] if row.get("role") == "assistant") else "我刚才那句说得有点绕。我是想接着你的话，不是替你下结论。"
    if intent == "boundary":
        return "行，这件事先放下。我不往下问了。"
    if intent == "question":
        if re.search(r"早上.*跑|晚上.*跑|跑步.*(?:早上|晚上)", text):
            return "只在早晚里选，我会选你更容易长期坚持的那个：早上不容易被临时安排挤掉，晚上则通常更容易活动开。离睡觉太近就别跑得太猛。"
        if re.search(r"你那边|你在干嘛|你怎么样", text):
            daily = context.get("dailyLife") if isinstance(context.get("dailyLife"), dict) else {}
            activity = str(daily.get("currentActivity") or "").strip()
            mood = str(daily.get("moodBias") or "").strip()
            if activity:
                suffix = f"，{mood}" if mood else ""
                return f"我这边在{activity}{suffix}。刚好拿这点虚拟日常换换脑子。"
            return "我这边没有可讲的新动静；至于你刚才具体在忙什么，我没有记录，不能替你补。"
        if re.search(r"多久|多少|进度|做到哪|完成了吗", text):
            return "我手里没有对应的时长或进度记录，具体数值不能替你猜。"
        return "这件事我缺的是能落到事实上的信息；没有那一段，我现在答不上来，也不能编个确定答案。"
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
        previous = str(mind.get("previousUserTurn") or "").strip()
        topic = next((label for pattern, label in (
            (r"公园|散步|小路", "公园散步"),
            (r"同步|合并|冲突", "同步冲突"),
            (r"会议|开会", "那场会"),
            (r"项目|方案", "手头那件事"),
        ) if re.search(pattern, previous)), "刚才那件事")
        if decision.get("askQuestion") and previous:
            return f"接着刚才的{topic}，哪个细节最让你记住？"
        return f"刚才的{topic}，我更想听里面的小细节，不急着给整件事下结论。" if previous else "我偏爱小细节，大道理反而容易把有趣的地方盖住。"
    # Small, grounded observations for common shares; never invent an outcome or a shared past.
    for pattern, observation, curiosity in (
        (r"散步|走路|走了|走一圈|公园|下楼|楼下", "我偏爱散步里没有目的的那一小段，不用连走路也算成任务。", "路上有什么让你停下来多看了一眼？"),
        (r"吃|饭|面|菜|咖啡", "我偏爱食物里能记住的小特点，比‘好吃’两个字更有画面。", "最让你记住的是哪一种味道？"),
        (r"电影|小说|书|看剧", "我更容易记住作品里一个小场面，不一定是最热闹的高潮。", "哪个画面现在还留在你脑子里？"),
        (r"做完|完成|解决|搞定|交出|交了|提交", "东西交出去以后那点空白也很珍贵，不必立刻塞进下一项任务。", "最后卡住的那一步是怎么过去的？"),
    ):
        if re.search(pattern, text):
            return observation + (curiosity if decision.get("askQuestion") else "")
    if re.search(r"轻松|放空|歇会|放松", text):
        return "那就挑一件不讲效率的小事，做得一般也不扣分。" + (
            "你手边有没有那种随时能停的事？" if decision.get("askQuestion") else ""
        )
    if re.search(r"还行|就那样|平平常常|一般", text):
        return "听起来是没什么大波澜的一天。" + (
            "倒是有没有一个小插曲，回想起来还挺有意思？" if decision.get("askQuestion") else "平稳有时候也挺好。"
        )
    if decision.get("askQuestion"):
        if re.search(r"开会|会议|周报", text):
            return "会和周报挤在一起，最容易让一天只剩下交付感。中间哪一刻最想按快进？"
        if re.search(r"同事|冲突|争执|不舒服", text):
            return "我先不替任何一边判对错。真正让你不舒服的是哪一句？"
        if re.search(r"碎|零散|打断", text):
            return "这种碎不是忙得轰轰烈烈，是注意力被一点点切走。哪次打断最烦？"
        if re.search(r"去忙|先忙|忙了", text):
            return "去吧，先把手头那件事收住。忙完最想拿什么换换脑子？"
        return "这事里我最好奇的不是结论，是那个让你停了一下的小细节。哪一幕还留在脑子里？"
    if re.search(r"开心|太棒|好消息|终于", text):
        return "这件好事值得单独留个位置，别让接下来的待办把它挤没了。"
    return "我更在意这件事里让你记住的小细节，急着下结论反而容易错过它。"
