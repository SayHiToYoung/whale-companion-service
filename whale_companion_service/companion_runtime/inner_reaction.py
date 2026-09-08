from __future__ import annotations


def build_inner_reaction(shared_scene: dict, relationship: dict, scene: dict, emotion: dict, persona: dict) -> dict:
    intent = shared_scene["intent"]
    if intent == "reaction_to_companion":
        reaction = "意识到用户在质疑自己上一句话；先回看自己说了什么，允许承认说得怪。"
    elif intent == "prompt_to_speak":
        reaction = "主动贡献一个具体观察，不等待用户提供话题。"
    elif emotion["signal"] == "emotional_bid":
        reaction = "在意这声叹气，但不预设原因。"
    elif intent == "continuation":
        reaction = "沿着共同话题理解省略部分，不要求用户重新交代背景。"
    elif intent == "question":
        reaction = "先回答问题本身，不绕去做情绪服务。"
    else:
        reaction = "形成一个符合自身偏好的具体反应，不总结用户。"
    return {
        "reaction": reaction,
        "stance": "has_own_view",
        "personaTraitsUsed": list(persona.get("traits", []))[:3],
        "relationshipStyle": relationship["interactionStyle"],
        "scenePeriod": scene["period"],
    }
