from __future__ import annotations
import re


def build_turn_decision(shared_scene: dict, emotion: dict, relationship: dict | None = None) -> dict:
    intent = shared_scene["intent"]
    action = {
        "reaction_to_companion": "respond_to_own_previous_turn",
        "prompt_to_speak": "volunteer_observation",
        "repair": "repair_and_answer_previous",
        "memory_check": "answer_from_shared_memory",
        "question": "answer_directly",
        "continuation": "continue_shared_topic",
    }.get(intent, "offer_personal_reaction")
    if emotion["signal"] == "emotional_bid":
        action = "lean_in_without_assuming"
    relation = relationship or {}
    permissions = relation.get("permissions", {})
    restricted = relation.get("stage") in {"deeply_distant", "strongly_distant", "distant"} or relation.get("boundaryState") in {"guarded", "restricted", "closed"}
    text = shared_scene.get("currentUtterance", "")
    move, hook = "reciprocate", "personal_reaction"
    if intent == "boundary":
        action, move, hook = "respect_boundary", "close", "none"
    elif restricted and intent not in {"question", "memory_check", "repair"}:
        move, hook = "hold", "none"
    elif intent in {"question", "memory_check", "repair", "reaction_to_companion"}:
        pass  # Answer first; a user question is not permission for an interview.
    elif re.search(r"换个话题|聊点别的|这个聊完了", text) or intent == "prompt_to_speak" or shared_scene.get("topicExhausted"):
        move = "pivot"
    elif re.search(r"哈哈|开心|太棒|好消息|终于", text) and permissions.get("allowPlayful"):
        move, hook = "play", "playful_hook"
    elif re.search(r"还记得|上次|之前", text) and permissions.get("allowSharedMemory"):
        move, hook = "bridge", "memory_callback"
    elif not shared_scene.get("recentQuestionCount", 0):
        move, hook = "deepen", "specific_curiosity"
    return {
        "primaryAction": action,
        "conversationMove": move,
        "replyHook": hook,
        "hookAnchor": "" if hook == "none" else shared_scene.get("topicAnchor", ""),
        "maxSentences": 2 if move in {"close", "hold"} else 4,
        "askQuestion": hook == "specific_curiosity",
        "allowSilenceAfter": move in {"close", "hold"},
        "answerFirst": intent in {"question", "memory_check", "repair"},
        "contributeContent": hook != "none",
        "forbidServiceClosure": True,
    }
