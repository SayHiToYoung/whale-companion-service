from __future__ import annotations

from whale_companion_service.persona_card import (
    DEFAULT_PERSONA_CARD,
    compile_persona_card,
    validate_persona_card,
)


def test_default_card_has_emotional_reactions_and_stages():
    assert isinstance(DEFAULT_PERSONA_CARD["emotionalReactions"], dict)
    assert "happy" in DEFAULT_PERSONA_CARD["emotionalReactions"]
    assert isinstance(DEFAULT_PERSONA_CARD["relationshipStages"], dict)
    assert "familiar" in DEFAULT_PERSONA_CARD["relationshipStages"]


def test_validate_keeps_new_fields():
    validated = validate_persona_card(dict(DEFAULT_PERSONA_CARD))
    assert "emotionalReactions" in validated
    assert "relationshipStages" in validated


def test_compile_emits_reactions_and_stages():
    compiled = compile_persona_card(DEFAULT_PERSONA_CARD)
    assert "不同心情下的说话方式" in compiled
    assert "开心时" in compiled
    assert "看到你熬夜时" in compiled
    assert "随关系阶段的说话方式" in compiled
    assert "刚认识" in compiled
    assert "很亲近" in compiled


def test_custom_card_can_override_reactions():
    card = dict(DEFAULT_PERSONA_CARD)
    card["emotionalReactions"] = {"happy": "会转圈圈"}
    compiled = compile_persona_card(card)
    assert "会转圈圈" in compiled
