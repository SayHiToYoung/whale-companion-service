"""Incremental adapters over existing domain algorithms; no writes or model calls."""
from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timedelta, timezone

from ..dialogue_state import emotional_dialogue_state
from ..memory_activation import memory_activation, memory_source_confidence
from ..memory_lifecycle import default_lifecycle_status, memory_is_speakable
from ..memory_protocol import memory_time
from ..memory_retrieval import memory_topic_relevance, select_companion_memories
from ..persona_card import DEFAULT_PERSONA_CARD, compile_persona_card
from ..profile_memory import extract_profile_memories
from ..standing_knowledge import derive_standing_knowledge
from .companion_emotion import companion_emotion_context
from .context_assembler import safe_value
from .context_contract import CompanionFrame, ContextBudgetError, ContextFact, ContextFragment, Priority, canonical, timestamp
from .daily_life import advance_agenda, daily_life_context, daily_life_fragments
from .emotion_state import build_emotion_state
from .expression_learning import expression_context, recall_rules, scene_of, is_learnable
from .inner_reaction import build_inner_reaction
from .open_threads import build_open_threads
from .affinity import normalize_affinity_state, relationship_constraints
from .relationship import build_relationship
from .proactive import proactive_policy_fragment
from .safety import build_safety
from .scene import build_scene
from .self_timeline import asks_about_self_timeline, recall_self_timeline, self_timeline_context
from .shared_scene import build_shared_scene
from .speech_style import build_speech_style
from .turn_decision import build_turn_decision
from .story import story_fragments


def _sources(row, fallback):
    ids = [row.get("id"), row.get("threadId"), row.get("sourceMessageId"), row.get("messageId")]
    ids.extend(row.get("source_event_ids") or row.get("evidenceMemoryIds") or [])
    ids.extend(e.get("messageId") for e in row.get("evidence", []) if isinstance(e, dict))
    return tuple(sorted({str(v) for v in ids if v})) or (fallback,)


# 每层记忆有资格声称的字段，以及这层必须拿得出的证据。白名单之外的一切
# ——原始活动样本、审计字段、客户端塞进来的任意 payload——都不进上下文。
_LAYER_SOURCE = {"L1": "observed", "L2": "derived", "L3": "user_stated"}
_LAYER_FIELDS = {"L1": ("app", "context", "durationSeconds"), "L2": ("statement",), "L3": ("label", "quote")}
_LAYER_REQUIRED = {"L1": ("app",), "L2": ("statement",), "L3": ("label",)}
_MEMORY_TEXT_LIMIT = 240


def validated_memory_value(row):
    """把一条原始记忆投影成可进上下文的最小事实；投影不过关就整条丢弃。

    除了字段白名单，这里还校验 layer 与来源自述是否一致。一条桌面观察如果自称
    `user_stated`，进上下文后就会被仲裁当成用户亲口说过的话——证据强度凭空升了一级。
    宁可丢掉这条记忆，也不让它冒充更强的证据。
    """
    layer = str(row.get("layer") or "")
    fields = _LAYER_FIELDS.get(layer)
    if fields is None or not str(row.get("id") or "").strip():
        return None, ""
    source = _LAYER_SOURCE[layer]
    declared = str(row.get("sourceType") or "")
    if declared and declared != source:
        return None, ""
    value = {}
    for key in fields:
        raw = row.get(key)
        if raw is None:
            continue
        if key == "durationSeconds":
            try:
                seconds = float(raw)
            except (TypeError, ValueError):
                return None, ""
            if not math.isfinite(seconds) or seconds < 0:
                return None, ""
            value[key] = int(seconds)
        else:
            text = " ".join(str(raw).split())[:_MEMORY_TEXT_LIMIT]
            if text:
                value[key] = text
    if any(key not in value for key in _LAYER_REQUIRED[layer]):
        return None, ""
    if layer == "L1" and isinstance(row.get("project"), dict):
        name = " ".join(str(row["project"].get("name") or "").split())[:160]
        if name:
            value["projectName"] = name
    value["layer"] = layer
    return value, source


def memory_fragments(memories, query, now):
    """记忆进上下文的唯一通道：筛选 → 事实校验 → 去重 → ContextFragment。

    筛选不再是"有没有过期"，而是激活度：类型决定衰退速度，话题相关、重要性、情绪强度、
    再次提及、未完成状态和来源置信度共同决定它此刻还剩多少分量。
    """
    speakable = [r for r in memories if isinstance(r, dict) and memory_is_speakable(r)]
    selected, _trace = select_companion_memories(speakable, query)
    live = []
    for row in selected:
        value, source = validated_memory_value(row)
        if value is None:
            continue
        lifecycle = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
        reading = memory_activation(
            row, lifecycle, now=now, relevance=memory_topic_relevance(row, query),
            status=str(lifecycle.get("storedStatus") or lifecycle.get("status") or ""),
        )
        if not reading["speakable"]:
            continue
        live.append((reading, row, value, source))

    # 去重：同一件事被记成两条（同一应用同一情境、同一句陈述）只进上下文一次。
    # 合并的是证据不是内容——时长这类可加量求和，其余字段必须逐字相同才算同一条。
    merged: dict[str, dict] = {}
    for reading, row, value, source in live:
        signature = canonical({k: v for k, v in value.items() if k != "durationSeconds"})
        entry = merged.get(signature)
        if entry is None:
            merged[signature] = entry = {
                "reading": reading, "row": row, "value": dict(value), "source": source,
                "sources": set(_sources(row, "memory:" + str(row["id"]))),
            }
            continue
        entry["sources"].update(_sources(row, "memory:" + str(row["id"])))
        if "durationSeconds" in value:
            entry["value"]["durationSeconds"] = entry["value"].get("durationSeconds", 0) + value["durationSeconds"]
        better = (reading["activation"], str(row["id"])) > (
            entry["reading"]["activation"], str(entry["row"]["id"]))
        if better:
            entry["reading"], entry["row"], entry["source"] = reading, row, source

    result = []
    for entry in merged.values():
        reading, row, value = entry["reading"], entry["row"], entry["value"]
        lifecycle = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
        result.append(ContextFragment(module="memory",
            # 激活度只在记忆这一档内部排先后，装不下时先丢激活度低的，
            # 但仍然压在未完成对话之下——它决定顺序，不决定真假。
            priority=int(Priority.MEMORY) + int(round(reading["activation"] * 50)),
            facts=(ContextFact("memory:" + str(row["id"]), value, source=entry["source"],
                lifecycle=str(lifecycle.get("status") or default_lifecycle_status(row))),),
            source_event_ids=tuple(sorted(entry["sources"])),
            confidence=memory_source_confidence(row),
            created_at=memory_time(row) or now.isoformat(), expires_at=row.get("expiresAt"),
            sensitivity=row.get("sensitivity", "personal"), token_budget=12000,
            metadata={"activation": reading["activation"], "memoryClass": reading["memoryClass"]}))
    return result


def collect_context_fragments(*, user_text, conversation, memories, user_facts, boundaries, persona,
                              threads=None, companion_emotion=None, affinity_state=None, daily_state=None,
                              chronotype=None, timeline_events=None, expression_rules=None,
                              story_provider=None, now=None, **_compat):
    moment = now or datetime.now(timezone.utc)
    at = moment.isoformat()
    expiry = (moment + timedelta(minutes=10)).isoformat()
    recent = [dict(role=r["role"], text=str(r.get("text") or "")[-4000:]) for r in conversation
              if r.get("role") in {"user", "assistant"}][-8:]
    if not recent or recent[-1]["role"] != "user" or recent[-1]["text"] != user_text:
        recent.append({"role": "user", "text": user_text})
    message_source = "current-message:" + hashlib.sha256(user_text.encode()).hexdigest()[:16]
    current = next((r for r in reversed(conversation) if r.get("role") == "user" and r.get("text") == user_text), {})
    message_source = str(current.get("messageId") or current.get("id") or message_source)
    boundaries = [r for r in boundaries if not r.get("expiresAt") or timestamp(r["expiresAt"]) > moment]
    # Direct legacy responder calls must respect boundaries in the current utterance too.
    existing_rules = {r.get("rule") for r in boundaries if r.get("status", "active") == "active"}
    boundaries = boundaries + [{**r, "status": "active", "sourceMessageId": message_source}
        for r in extract_profile_memories(user_text)["boundaries"] if r["rule"] not in existing_rules]
    timeline_events = [r for r in timeline_events or [] if not r.get("expiresAt") or timestamp(r["expiresAt"]) > moment]
    expression_rules = [r for r in expression_rules or [] if not r.get("expiresAt") or timestamp(r["expiresAt"]) > moment]
    if daily_state and daily_state.get("date") and daily_state["date"] != moment.date().isoformat():
        daily_state = None  # A prior day's snapshot is not the current virtual life state.
    fragments = []

    def emit(module, value, priority, *, key=None, sources=None, confidence=1.0, source="derived", instructions=(), created_at=None):
        fragments.append(ContextFragment(module=module, priority=priority,
            facts=(ContextFact(key or module, safe_value(value), source=source),), instructions=tuple(instructions),
            source_event_ids=tuple(sources or ("projection:" + module,)), confidence=confidence,
            created_at=created_at or at, expires_at=expiry, token_budget=12000))

    for row in boundaries:
        if row.get("status", "active") != "active":
            continue
        rule = str(row.get("rule") or "")
        terms = re.findall(r"[“「](.*?)[”」]", rule) if row.get("kind") in {"topic_suppression", "addressing"} else []
        if row.get("kind") == "identity_correction" and "学生" in rule:
            terms.extend(["早课", "考试", "作业", "学校", "寒暑假", "学生党"])
        fragments.append(ContextFragment(module="boundaries", priority=Priority.BOUNDARY,
            instructions=(rule,), source_event_ids=_sources(row, "boundary:" + hashlib.sha256(rule.encode()).hexdigest()[:16]),
            created_at=row.get("createdAt") or at, expires_at=row.get("expiresAt"),
            metadata={"blocked_terms": terms + list(row.get("blockedTerms") or []),
                      "blocked_modules": row.get("blockedModules") or [], "blocked_keys": row.get("blockedKeys") or [],
                      "deny_sensitivities": row.get("denySensitivities") or []}))
    fragments.extend(memory_fragments(memories, user_text, moment))
    facts = [r for r in user_facts if r.get("status") == "confirmed"
             and (not r.get("expiresAt") or timestamp(r["expiresAt"]) > moment)]
    for row in facts:
        fragments.append(ContextFragment(module="user_facts", priority=Priority.MEMORY,
            facts=(ContextFact("user:" + str(row.get("predicate")), row.get("value"), source="user_stated", lifecycle="confirmed"),),
            source_event_ids=_sources(row, "profile:" + str(row.get("predicate"))), confidence=float(row.get("confidence", 1)),
            created_at=row.get("updatedAt") or row.get("createdAt") or at, expires_at=row.get("expiresAt")))

    open_state = build_open_threads(threads or [], now=moment)
    shared = build_shared_scene(user_text, recent)
    relationship = build_relationship(conversation, facts, open_state, affinity_state, boundaries)
    scene = build_scene([])  # Observations have one provenance-preserving path: memory adapter.
    scene["period"] = daily_life_context(daily_state, chronotype, now=moment)["period"]
    emotion = build_emotion_state(user_text, shared, open_state)
    reaction = build_inner_reaction(shared, relationship, scene, emotion, persona)
    decision = build_turn_decision(shared, emotion)
    style = build_speech_style(persona, decision)
    safety = build_safety(boundaries, emotion)
    daily = daily_life_context(daily_state, chronotype, now=moment)
    # 日程推进出的状态；进模型的只有这一层处理过的投影，完整日程留在日程模块里。
    agenda_state = advance_agenda(now=moment, chronotype=chronotype, daily_state=daily_state)
    daily = {**daily, **{k: agenda_state[k] for k in
             ("currentActivity", "location", "movedFrom", "shareableEvents", "stateKind")}}
    recalled = recall_self_timeline(timeline_events or [], user_text, now=moment) if asks_about_self_timeline(user_text) else None
    timeline = self_timeline_context(recalled) if recalled is not None else None
    rules = recall_rules(scene_of(user_text), [r for r in expression_rules or [] if is_learnable(str(r.get("pattern") or ""))])
    expressions = expression_context(rules)
    companion = companion_emotion_context(companion_emotion) if companion_emotion is not None else None
    legacy = dict(sharedScene=shared, relationship=relationship, scene=scene, emotion=emotion, openThreads=open_state,
                  innerReaction=reaction, turnDecision=decision, speechStyle=style, safety=safety,
                  dailyLife=daily, selfTimeline=timeline, learnedExpressions=expressions, companionEmotion=companion)
    for module, value in (("sharedScene", shared), ("emotion", {**emotion, "carriedEmotion": "", "carriedFromThread": ""}),
                          ("scene", scene), ("turnDecision", decision), ("safety", {**safety, "activeBoundaries": []}),
                          ("dialogueState", emotional_dialogue_state(recent))):
        emit(module, value, Priority.CURRENT, sources=(message_source,), source="user_stated" if module == "emotion" else "derived")
    by_thread = {r.get("threadId"): r for r in threads or []}
    for view in open_state["threads"]:
        if view["carryWeight"] < 0.25:
            continue
        raw = by_thread.get(view["threadId"], {})
        emit("openThreads", {k: view[k] for k in ("label", "topic", "need", "status", "daysSinceLastTouch")},
             Priority.OPEN_THREAD, key="thread:" + view["threadId"], sources=_sources(raw, "thread:" + view["threadId"]),
             confidence=min(0.49, view["carryWeight"]),
             created_at=raw.get("lastMentionedAt") or raw.get("updatedAt") or raw.get("openedAt"),
             instructions=("过去的情绪只是背景，不得断言是用户此刻的情绪，也不以此开场。",))
    for followup in open_state["pendingFollowUps"]:
        emit("openThreads", {k: followup[k] for k in ("hint", "topic")}, Priority.OPEN_THREAD,
             key="commitment:" + followup["threadId"],
             sources=_sources(by_thread.get(followup["threadId"], {}), followup["threadId"]), source="user_stated",
             created_at=by_thread.get(followup["threadId"], {}).get("updatedAt"))
    relationship_source = "relationship_state:" + str((affinity_state or {}).get("updatedAt") or "fallback")
    emit("relationship", {k: v for k, v in relationship.items()
                          if k not in {"commitments", "score", "userTurnCount", "knownFactCount",
                                       "permissions", "boundaryState", "allowPlayful", "allowFollowup",
                                       "allowMemory", "allowDailyCare"}},
         Priority.RELATIONSHIP, sources=(relationship_source,), created_at=(affinity_state or {}).get("updatedAt"))
    # 关系权限：模型只看到"能做什么"，看不到任何内部评分。
    # 同一份权限还以控制数据的形式交给组装器，用户划了线时真的把对应模块挡在上下文外，
    # 而不是多给模型一句"请不要"。
    internal = normalize_affinity_state(affinity_state or {})
    permissions = relationship["permissions"]
    constraints = relationship_constraints(internal, permissions, boundaries=boundaries)
    fragments.append(ContextFragment(module="relationshipPolicy", priority=Priority.RELATIONSHIP,
        facts=(ContextFact("relationshipPermissions", permissions, source="derived"),),
        instructions=("这些权限是关系此刻允许的分寸，不是建议。没有开的就不要做，也不要解释为什么不做。",),
        source_event_ids=(relationship_source,), created_at=(affinity_state or {}).get("updatedAt") or at,
        metadata={"blocked_modules": constraints["blocked_modules"],
                  "deny_sensitivities": constraints["deny_sensitivities"]}))
    daily_source = "daily_state:" + str((daily_state or {}).get("date") or "default")
    fragments.extend(daily_life_fragments(
        agenda_state if daily_state else {}, now=moment, source=daily_source))
    if recalled is not None:
        emit("selfTimelinePolicy", {"empty": not recalled}, Priority.DAILY_LIFE, instructions=(timeline["usage"],))
        for event in recalled:
            fragments.append(ContextFragment(module="self_timeline", priority=Priority.DAILY_LIFE,
                facts=(ContextFact("timeline:" + str(event.get("id")), {k: event.get(k, "") for k in ("when", "type", "summary")},
                                   source="confirmed", lifecycle="confirmed"),),
                source_event_ids=_sources(event, "timeline:unknown"), created_at=event["ts"], expires_at=event.get("expiresAt")))
    for entry in derive_standing_knowledge(facts, [], threads or []):
        if entry["plate"] == "user_profile":
            continue  # Already represented under the canonical user predicate.
        emit("standingKnowledge", entry["text"], Priority.MEMORY, key="standing:" + entry["entryId"],
             sources=entry["basedOn"], source="user_stated", instructions=("只用于理解背景，不主动复述。",))
    fragments.append(ContextFragment(module="standingKnowledge", priority=Priority.MEMORY,
        instructions=("user_facts 是常驻认知，依据已确认来源理解用户，不重复询问，也不主动复述表功。",),
        source_event_ids=("policy:standing_knowledge",), created_at=at))
    for rule in rules:
        emit("learnedExpressions", {"pattern": rule["pattern"], "scene": rule.get("scene", "casual")}, Priority.STYLE,
             key="expression:" + str(rule.get("id") or rule["pattern"]), sources=_sources(rule, "expression:" + rule["pattern"]),
             instructions=(expressions["usage"],))
    emit("innerReaction", reaction, Priority.STYLE)
    emit("speechStyle", style, Priority.STYLE)
    if companion is not None:
        emit("companionEmotion", {"mood": companion.get("mood", ""), "usage": companion.get("usage", "")}, Priority.STYLE)
    fragments.append(ContextFragment(module="persona", priority=Priority.STYLE,
        instructions=(compile_persona_card({**DEFAULT_PERSONA_CARD, **persona}),),
        source_event_ids=("persona:" + hashlib.sha256(canonical(persona).encode()).hexdigest()[:16],), created_at=at, token_budget=18000))
    fragments.extend(story_fragments(story_provider, now=moment))
    fragments.append(proactive_policy_fragment(relationship, now=moment))
    return fragments, recent, legacy


def ensure_frame(memories, conversation=None, profile=None):
    """Legacy signature adapter. Model context always passes through the assembler."""
    from .orchestrator import build_companion_frame
    profile = profile or {}
    frame = profile.get("companionFrame")
    if isinstance(frame, CompanionFrame):
        return frame
    if isinstance(frame, dict) and frame.get("version") == "companion-frame-v5":
        # Trusted persisted frames re-enter arbitration, including expiry and source validation.
        from .context_assembler import ContextAssembler
        if frame.get("internalMetadata", {}).get("generationBlocked"):
            return CompanionFrame(frame)
        priorities = {"boundaries": Priority.BOUNDARY, "sharedScene": Priority.CURRENT,
                      "emotion": Priority.CURRENT, "scene": Priority.CURRENT, "turnDecision": Priority.CURRENT,
                      "safety": Priority.CURRENT, "dialogueState": Priority.CURRENT, "openThreads": Priority.OPEN_THREAD,
                      "memory": Priority.MEMORY, "user_facts": Priority.MEMORY, "standingKnowledge": Priority.MEMORY,
                      "relationship": Priority.RELATIONSHIP, "proactivePolicy": Priority.RELATIONSHIP,
                      "dailyLife": Priority.DAILY_LIFE, "self_timeline": Priority.DAILY_LIFE, "story": Priority.STORY}
        fragments = []
        for row in frame.get("processedFacts", []):
            fragments.append(ContextFragment(module=row["module"], priority=priorities.get(row["module"], Priority.STYLE),
                facts=(ContextFact(row["key"], row["value"], source=row["source"], lifecycle=row["lifecycle"]),),
                source_event_ids=tuple(row["source_event_ids"]), confidence=row["confidence"],
                created_at=row["created_at"], expires_at=row["expires_at"], token_budget=18000))
        for row in frame.get("moduleInstructions", []):
            fragments.append(ContextFragment(module=row["module"], priority=priorities.get(row["module"], Priority.STYLE),
                instructions=(row["text"],), source_event_ids=tuple(row["source_event_ids"]), confidence=row["confidence"],
                created_at=row["created_at"], expires_at=row["expires_at"], token_budget=18000))
        return ContextAssembler().assemble(fragments, recent_conversation=[
            {"role": r["role"], "text": r["content"]} for r in frame.get("recentConversation", [])])
    history = conversation or []
    text = next((r.get("text", "") for r in reversed(history) if r.get("role") == "user"), "")
    return build_companion_frame(user_text=text, conversation=history, memories=memories,
        user_facts=profile.get("userFacts", []), boundaries=profile.get("boundaries", []),
        persona=profile.get("personaCard") or DEFAULT_PERSONA_CARD)


def build_reply_frame(**inputs):
    """Repository boundary: an oversized mandatory context selects local fallback."""
    from .orchestrator import build_companion_frame
    try:
        return build_companion_frame(**inputs)
    except ContextBudgetError:
        return CompanionFrame(version="companion-frame-v5", recentConversation=[], processedFacts=[],
            moduleInstructions=[], sharedScene={}, internalMetadata={"generationBlocked": "context_budget_exceeded"})
