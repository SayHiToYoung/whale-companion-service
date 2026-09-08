from __future__ import annotations

from .emotion_state import build_emotion_state
from .inner_reaction import build_inner_reaction
from .open_threads import build_open_threads
from .relationship import build_relationship
from .safety import build_safety
from .scene import build_scene
from .shared_scene import build_shared_scene
from .speech_style import build_speech_style
from .turn_decision import build_turn_decision


def build_companion_frame(*, user_text: str, conversation: list[dict], memories: list[dict], user_facts: list[dict], boundaries: list[dict], persona: dict, threads: list[dict] | None = None) -> dict:
    open_threads = build_open_threads(threads or [])
    shared = build_shared_scene(user_text, conversation)
    relationship = build_relationship(conversation, user_facts, open_threads)
    scene = build_scene(memories)
    emotion = build_emotion_state(user_text, shared, open_threads)
    reaction = build_inner_reaction(shared, relationship, scene, emotion, persona)
    decision = build_turn_decision(shared, emotion)
    style = build_speech_style(persona, decision)
    safety = build_safety(boundaries, emotion)
    return {
        "version": "companion-frame-v2", "sharedScene": shared, "relationship": relationship,
        "scene": scene, "emotion": emotion, "openThreads": open_threads,
        "innerReaction": reaction, "turnDecision": decision, "speechStyle": style, "safety": safety,
    }
