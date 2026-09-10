from __future__ import annotations


def build_speech_style(persona: dict, decision: dict) -> dict:
    return {
        "voice": list(persona.get("traits", []))[:4],
        "maxSentences": decision["maxSentences"],
        "defaultToStatement": not decision["askQuestion"],
        "conversationMove": decision["conversationMove"],
        "replyHook": decision["replyHook"],
        "allowAcknowledgementPrefix": False,
        "allowForcedChoiceEnding": False,
        "rules": list(persona.get("speechRules", [])),
        "badPatterns": list(persona.get("badPatterns", [])),
    }
