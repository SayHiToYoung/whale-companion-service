from __future__ import annotations


def build_turn_decision(shared_scene: dict, emotion: dict) -> dict:
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
    return {
        "primaryAction": action,
        "maxSentences": 1 if intent in {"reaction_to_companion", "prompt_to_speak", "continuation"} else 2,
        "askQuestion": intent == "question" and action == "answer_directly",
        "allowSilenceAfter": True,
        "forbidServiceClosure": True,
    }
