from __future__ import annotations

from whale_companion_service.companion_runtime.expression_learning import (
    expression_context,
    is_learnable,
    learn_candidates,
    recall_rules,
    review_rule,
    scene_of,
)


def test_scene_detection():
    assert scene_of("谢谢你真好") == "praise"
    assert scene_of("好烦啊") == "complaint"
    assert scene_of("我最近有点焦虑") == "concern"
    assert scene_of("哈哈笑死") == "play"
    assert scene_of("今天天气不错") == "casual"


def test_learnable_short_expression():
    assert is_learnable("开摆") is True
    assert is_learnable("麻了") is True


def test_not_learnable_long_or_fact():
    assert is_learnable("我最近在写一个很长的项目文档") is False   # 太长
    assert is_learnable("我叫张三") is False                        # 事实句
    assert is_learnable("我的密码是 abc") is False                  # 敏感
    assert is_learnable("你吃饭了吗") is False                      # 提问


def test_learn_candidates_returns_pending():
    candidates = learn_candidates("开摆", "complaint")
    assert len(candidates) == 1
    assert candidates[0]["status"] == "pending"
    assert candidates[0]["pattern"] == "开摆"


def test_learn_candidates_dedup_known():
    candidates = learn_candidates("开摆", "complaint", known_patterns={"开摆"})
    assert candidates == []


def test_review_gates_recall():
    pending = {"pattern": "开摆", "instruction": "学着用", "scene": "complaint", "kind": "style", "status": "pending", "usageCount": 0}
    approved = review_rule(pending, "approved")
    assert approved["status"] == "approved"


def test_recall_only_approved_and_scene_first():
    rules = [
        {"pattern": "a", "scene": "casual", "status": "approved", "usageCount": 1},
        {"pattern": "b", "scene": "complaint", "status": "approved", "usageCount": 0},
        {"pattern": "c", "scene": "complaint", "status": "pending", "usageCount": 99},  # 未审核，不可召回
        {"pattern": "d", "scene": "complaint", "status": "approved", "usageCount": 5},
    ]
    recalled = recall_rules("complaint", rules)
    patterns = [r["pattern"] for r in recalled]
    assert "c" not in patterns                       # pending 不进召回
    assert patterns[0] == "d"                        # 场景命中 + 使用次数高优先
    assert patterns[1] == "b"


def test_review_rejects_invalid_decision():
    import pytest
    rule = {"pattern": "x", "status": "pending"}
    with pytest.raises(ValueError):
        review_rule(rule, "nonsense")


def test_context_marks_empty():
    ctx = expression_context([])
    assert ctx["empty"] is True
