"use strict";

const SETTINGS_KEY = "echo-debug-settings-v1";
const $ = (selector) => document.querySelector(selector);
const state = { desktop: null, service: null, opening: null };

function localDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString("sv-SE");
}

function selectedDate() { return $("#episode-date").value || new Date().toLocaleDateString("sv-SE"); }

function memoryDate(memory, desktop) {
  if (memory.localDate) return memory.localDate;
  const raw = memory.occurredAt || memory.endedAt || memory.startedAt || memory.createdAt;
  if (raw) return localDate(raw);
  const facts = desktop?.collector?.facts || [];
  const linked = (memory.factIds || []).map((id) => facts.find((fact) => fact.id === id)).find(Boolean);
  return linked ? memoryDate(linked, desktop) : "";
}

function settings() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}"); } catch (_) {}
  return {
    userId: saved.userId || "local-user",
    token: saved.token || "local-dev-token",
    desktopUrl: String(saved.desktopUrl || "http://127.0.0.1:47890").replace(/\/$/, ""),
  };
}

function applySettings() {
  const value = settings();
  $("#user-id").value = value.userId;
  $("#token").value = value.token;
  $("#desktop-url").value = value.desktopUrl;
}

function saveSettings() {
  localStorage.setItem(SETTINGS_KEY, JSON.stringify({
    userId: $("#user-id").value.trim() || "local-user",
    token: $("#token").value,
    desktopUrl: $("#desktop-url").value.trim().replace(/\/$/, ""),
  }));
}

async function jsonFetch(url, options = {}, auth = false) {
  const headers = { "Accept": "application/json", "Content-Type": "application/json", ...(options.headers || {}) };
  if (auth) headers.Authorization = `Bearer ${settings().token}`;
  const response = await fetch(url, { ...options, headers, cache: "no-store" });
  let payload;
  try { payload = await response.json(); } catch (_) { throw new Error(`无法读取响应 (HTTP ${response.status})`); }
  if (!response.ok) throw new Error(payload.error || `请求失败 (HTTP ${response.status})`);
  return payload;
}

const serviceApi = (path, options) => jsonFetch(path, options, true);
const desktopApi = (path, options) => jsonFetch(`${settings().desktopUrl}${path}`, options, false);

function setStatus(text, kind = "") {
  const node = $("#operation-status");
  node.textContent = text;
  node.dataset.kind = kind;
}

function formatDuration(seconds) {
  const minutes = Math.round(Number(seconds || 0) / 60);
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  return `${hours} 小时${minutes % 60 ? ` ${minutes % 60} 分钟` : ""}`;
}

function memoryTitle(memory) {
  if (memory.layer === "L3") return memory.label || "用户表达的情绪";
  if (memory.layer === "L2") return memory.statement || memory.kind || "行为线索";
  return memory.project?.name || memory.app || memory.title || memory.kind || "活动事实";
}

function memoryDetail(memory) {
  const parts = [];
  if (memory.context) parts.push(memory.context);
  if (memory.durationSeconds) parts.push(formatDuration(memory.durationSeconds));
  if (memory.lifecycle?.status) parts.push(`生命周期 ${memory.lifecycle.status}`);
  if (memory.server?.serverSeq) parts.push(`服务序号 ${memory.server.serverSeq}`);
  return parts.join(" / ") || "暂无补充信息";
}

function renderMemories(selector, memories) {
  const root = $(selector);
  root.replaceChildren();
  const rows = [...(memories || [])].reverse().slice(0, 100);
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "还没有记忆。";
    root.append(empty);
    return;
  }
  for (const memory of rows) {
    const fragment = $("#memory-template").content.cloneNode(true);
    fragment.querySelector(".layer").textContent = `${memory.layer || "?"} ${memory.kind || "memory"}`;
    const rawTime = memory.endedAt || memory.occurredAt || memory.createdAt || memory.server?.receivedAt;
    fragment.querySelector("time").textContent = rawTime ? new Date(rawTime).toLocaleString("zh-CN") : "时间未知";
    fragment.querySelector(".memory-title").textContent = memoryTitle(memory);
    fragment.querySelector(".memory-detail").textContent = memoryDetail(memory);
    fragment.querySelector(".memory-id").textContent = memory.id || "无 ID";
    root.append(fragment);
  }
}

function renderSummary(desktop) {
  const summaries = desktop?.collector?.dailySummaries || {};
  const date = selectedDate();
  const summary = summaries[date];
  const node = $("#daily-summary");
  if (!summary) { node.className = "summary empty"; node.textContent = `${date} 没有可展示的汇总。`; return; }
  const topApp = summary.byApp?.[0];
  const text = [
    `活跃 ${formatDuration(summary.totalActiveSeconds)}`,
    summary.meetingSeconds ? `会议 ${formatDuration(summary.meetingSeconds)}` : "没有会议记录",
    topApp ? `主要应用 ${topApp.name} (${formatDuration(topApp.durationSeconds)})` : "",
  ].filter(Boolean).join("。") + "。";
  node.className = "summary";
  node.textContent = text;
}

function pipelineItem(title, stateName, detail) {
  const li = document.createElement("li");
  li.dataset.state = stateName;
  const badge = document.createElement("b");
  badge.textContent = stateName === "ok" ? "已有证据" : stateName === "error" ? "连接失败" : "等待数据";
  const strong = document.createElement("strong"); strong.textContent = title;
  const small = document.createElement("small"); small.textContent = detail;
  li.append(badge, strong, small);
  return li;
}

function renderPipeline() {
  const desktop = state.desktop;
  const service = state.service;
  const facts = (desktop?.collector?.facts || []).filter((item) => memoryDate(item, desktop) === selectedDate());
  const pending = desktop?.episode?.pendingCount || 0;
  const batches = service?.batches || [];
  const memories = service?.memories || [];
  const refs = service?.conversationMemoryRefs || [];
  const items = [
    ["桌面采集", desktop ? (facts.length ? "ok" : "empty") : "error", desktop ? `${selectedDate()} · ${facts.length} 条 L1 事实` : "桌宠端口不可用"],
    ["整理 outbox", desktop ? (pending ? "ok" : "empty") : "error", desktop ? `${pending} 个待确认批次` : "无法读取本地 outbox"],
    ["服务 ACK", service ? (batches.length ? "ok" : "empty") : "error", service ? `${batches.length} 个已接收批次` : "共享服务不可用"],
    ["记忆入库", service ? (memories.length ? "ok" : "empty") : "error", service ? `${memories.length} 条服务端记忆` : "无法读取 SQLite"],
    ["开场引用", service ? (refs.length || state.opening?.focusMemoryId ? "ok" : "empty") : "error", state.opening?.focusMemoryId || refs[0]?.memory_id || "尚未引用记忆"],
  ];
  const root = $("#pipeline"); root.replaceChildren(...items.map((item) => pipelineItem(...item)));
}

function renderPersona(service) {
  const persona = service?.persona || {};
  const companion = service?.companion || {};
  $("#persona-prompt").textContent = persona.systemPrompt || "未读取到人格规则。";
  let context = persona.modelContext || "";
  try { context = JSON.stringify(JSON.parse(context), null, 2); } catch (_) {}
  $("#model-context").textContent = context || "当前没有可提供给模型的记忆。";
  const values = [
    ["人格版本", persona.promptVersion || "未知"],
    ["回复模式", companion.modelEnabled ? companion.model : "严格事实型兜底"],
    ["最近来源", companion.lastReplySource || "尚未回复"],
    ["数据库", service?.database || "未知"],
  ];
  const root = $("#persona-meta"); root.replaceChildren();
  for (const [label, value] of values) {
    const item = document.createElement("div"); item.className = "meta-item";
    const span = document.createElement("span"); span.textContent = label;
    const strong = document.createElement("strong"); strong.textContent = value;
    item.append(span, strong); root.append(item);
  }
}

function renderPersonaCard(service) {
  const state = service?.persona?.card || {};
  const card = state.draft || state.active || {};
  const editor = $("#persona-card-editor");
  if (document.activeElement !== editor) editor.value = JSON.stringify(card, null, 2);
  $("#persona-card-version").textContent = state.draftVersion
    ? `草稿 v${state.draftVersion} · 已发布 v${state.activeVersion}`
    : `已发布 v${state.activeVersion || 1}`;
  $("#persona-compiled-prompt").textContent = state.compiledPrompt || "尚未编译。";
}

function personaCardFromEditor() {
  try { return JSON.parse($("#persona-card-editor").value); }
  catch (_) { throw new Error("PersonaCard 不是有效 JSON。"); }
}

async function savePersonaDraft() {
  return serviceApi("/v1/persona-card/actions", {
    method:"POST",
    body:JSON.stringify({userId:settings().userId, action:"saveDraft", card:personaCardFromEditor()}),
  });
}

function renderRelationship(service) {
  const threads = service?.emotionalThreads || [];
  const mentions = service?.memoryMentions || [];
  const identity = service?.identity || {};
  const desktopIdentity = state.desktop?.identity || {};
  const samePet = Boolean(identity.petId && desktopIdentity.petId && identity.petId === desktopIdentity.petId);
  $("#identity-status").textContent = samePet ? "两端一致" : "待核对";
  const identityRoot = $("#identity-details"); identityRoot.replaceChildren();
  const identityItem = document.createElement("article"); identityItem.className = "memory-row";
  const identityTitle = document.createElement("strong"); identityTitle.textContent = `${identity.productName || "回声"} · ${identity.petId || "未知 petId"}`;
  const identityDetail = document.createElement("p"); identityDetail.className = "memory-detail";
  identityDetail.textContent = `${identity.desktopName || "小鲸"} ⇄ ${identity.mobileName || "大鲸"} · ${identity.identityVersion || "版本未知"} · 桌面读取 ${desktopIdentity.petId || "失败"}`;
  identityItem.append(identityTitle, identityDetail); identityRoot.append(identityItem);
  const dialogue = service?.dialogueState || {phase:"idle", turn:0};
  const mind = service?.companionMind || {};
  const frame = service?.companionFrame || {};
  const latestTrace = service?.replyTraces?.[0] || null;
  const decision = frame.turnDecision || {};
  const shared = frame.sharedScene || {};
  $("#dialogue-phase").textContent = decision.primaryAction || mind.action || dialogue.phase || "idle";
  $("#companion-frame").textContent = Object.keys(frame).length ? JSON.stringify(frame, null, 2) : "尚未生成。";
  $("#reply-trace").textContent = latestTrace
    ? JSON.stringify(latestTrace, null, 2)
    : "还没有回复轨迹。请先在聊天页发送一句话。";
  const dialogueRoot = $("#dialogue-details"); dialogueRoot.replaceChildren();
  const dialogueItem = document.createElement("article"); dialogueItem.className = "memory-row";
  const dialogueTitle = document.createElement("strong"); dialogueTitle.textContent = shared.intent ? `共同现场：${shared.intent}` : (mind.intent ? `判断：${mind.intent}` : ({idle:"当前没有待承接情绪", invited:"已主动靠近，等待回应", exploring:"正在围绕当前话题承接", closed:"用户已结束本轮话题"}[dialogue.phase] || dialogue.phase));
  const dialogueDetail = document.createElement("p"); dialogueDetail.className = "memory-detail";
  dialogueDetail.textContent = shared.intent
    ? [`动作 ${decision.primaryAction}`, `话题 ${shared.topicAnchor || "未识别"}`, `关系 ${frame.relationship?.stage || "未知"}`, `情绪 ${frame.emotion?.userEmotion || "unknown"}`, frame.emotion?.carriedEmotion ? `延续 ${frame.emotion.carriedEmotion}（${frame.openThreads?.carriedAgeDays ?? "?"}天前·不可断言）` : "", frame.relationship?.commitments?.length ? `约定 ${frame.relationship.commitments.map((item) => item.hint).join("、")}` : "", `问句 ${decision.askQuestion ? "允许" : "禁止"}`].filter(Boolean).join(" · ")
    : mind.intent
    ? [`动作 ${mind.action}`, `话题 ${mind.topicAnchor || "未识别"}`, `场景 ${mind.scene}`, mind.previousUserTurn ? `上一句 ${mind.previousUserTurn}` : ""].filter(Boolean).join(" · ")
    : `承接轮次 ${dialogue.turn || 0}${dialogue.anchor ? ` · 起点 ${dialogue.anchor}` : ""}`;
  dialogueItem.append(dialogueTitle, dialogueDetail); dialogueRoot.append(dialogueItem);
  $("#thread-count").textContent = `${threads.length} 条`;
  $("#mention-count").textContent = `${mentions.length} 条`;
  const standing = service?.standingKnowledge || [];
  $("#standing-count").textContent = `${standing.length} 条`;
  const standingRoot = $("#standing-knowledge"); standingRoot.replaceChildren();
  if (!standing.length) standingRoot.textContent = "还没有可固化的常驻认知。";
  for (const entry of standing) {
    const item = document.createElement("article"); item.className = "memory-row";
    const title = document.createElement("strong");
    title.textContent = `${entry.plate === "user_profile" ? "TA的事" : "我们之间"} · ${entry.text}`;
    const detail = document.createElement("p"); detail.className = "memory-detail";
    detail.textContent = `印证 ${entry.sourceCount} 次 · 来源 ${entry.basedOn.join("、") || "未知"}`;
    item.append(title, detail); standingRoot.append(item);
  }
  const threadRoot = $("#emotional-threads"); threadRoot.replaceChildren();
  const mentionRoot = $("#memory-mentions"); mentionRoot.replaceChildren();
  if (!threads.length) threadRoot.textContent = "还没有用户明确表达的情绪线程。";
  for (const thread of threads) {
    const item = document.createElement("article"); item.className = "memory-row";
    const title = document.createElement("strong"); title.textContent = thread.topic || thread.label || "未命名情绪";
    const detail = document.createElement("p"); detail.className = "memory-detail";
    const needNames = {listening:"只想被陪着", vent:"想吐槽", advice:"需要建议", unknown:"需求待确认"};
    detail.textContent = [thread.status, needNames[thread.need] || thread.need, thread.episode_id, `已提起 ${thread.mention_count || 0} 次`, thread.follow_up_hint].filter(Boolean).join(" · ");
    const id = document.createElement("code"); id.textContent = thread.thread_id;
    item.append(title, detail, id); threadRoot.append(item);
  }
  if (!mentions.length) mentionRoot.textContent = "还没有主动提起记录。";
  for (const mention of mentions) {
    const item = document.createElement("article"); item.className = "memory-row";
    const title = document.createElement("strong"); title.textContent = mention.mention_type === "opening" ? "主动开场" : "对话承接";
    const detail = document.createElement("p"); detail.className = "memory-detail";
    detail.textContent = new Date(mention.created_at).toLocaleString("zh-CN");
    const id = document.createElement("code"); id.textContent = mention.memory_id;
    item.append(title, detail, id); mentionRoot.append(item);
  }
}

async function profileAction(memoryType, id, action) {
  const payload = {userId:settings().userId, memoryType, id, action};
  if (action === "correct") {
    const value = prompt("请输入纠正后的事实：");
    if (!value) return;
    payload.value = value;
  }
  if (action === "delete" && !confirm("确定让大鲸忘记这条记忆吗？记录会保留为 forgotten，便于审计。")) return;
  try {
    setStatus("正在更新长期记忆。");
    await serviceApi("/v1/profile-memories/actions", {method:"POST", body:JSON.stringify(payload)});
    setStatus("长期记忆已更新。", "ok");
    await refresh();
  } catch (error) { setStatus(error.message, "error"); }
}

function renderProfileMemories(service) {
  const facts = service?.userFacts || [];
  const boundaries = service?.boundaries || [];
  $("#user-fact-count").textContent = `${facts.length} 条`;
  $("#boundary-count").textContent = `${boundaries.length} 条`;
  const renderRows = (selector, rows, type) => {
    const root = $(selector); root.replaceChildren();
    if (!rows.length) root.textContent = type === "userFact" ? "还没有明确的长期用户事实。" : "还没有用户边界。";
    for (const row of rows) {
      const item = document.createElement("article"); item.className = "memory-row";
      const title = document.createElement("strong");
      title.textContent = type === "userFact" ? `${row.predicate}：${row.value}` : row.rule;
      const detail = document.createElement("p"); detail.className = "memory-detail";
      const evidence = type === "userFact" ? row.evidence?.[0]?.quote : row.sourceQuote;
      detail.textContent = [row.status, type === "userFact" ? `置信度 ${Math.round((row.confidence || 0) * 100)}%` : `优先级 ${row.priority}`, evidence ? `证据：“${evidence}”` : "无证据"].join(" · ");
      const id = document.createElement("code"); id.className = "memory-id"; id.textContent = row.id;
      const actions = document.createElement("div"); actions.className = "memory-actions";
      const specs = row.status === "paused" ? [["resume","恢复"]] : [["pause","暂停"]];
      if (type === "userFact") specs.unshift(["correct","纠正"]);
      specs.push(["delete","忘记"]);
      for (const [action, label] of specs) {
        const button = document.createElement("button"); button.type = "button"; button.textContent = label;
        button.addEventListener("click", () => profileAction(type, row.id, action)); actions.append(button);
      }
      item.append(title, detail, id, actions); root.append(item);
    }
  };
  renderRows("#user-facts", facts, "userFact");
  renderRows("#boundaries", boundaries, "boundary");
}

function renderInsightDrafts(service) {
  const drafts = service?.insightDrafts || [];
  $("#insight-draft-count").textContent = `${drafts.length} 条`;
  const root = $("#insight-drafts"); root.replaceChildren();
  if (!drafts.length) root.textContent = "至少需要跨两天的重复活动，或两次相同的明确情绪表达。";
  for (const draft of drafts) {
    const item = document.createElement("article"); item.className = "memory-row";
    const title = document.createElement("strong"); title.textContent = draft.headline;
    const detail = document.createElement("p"); detail.className = "memory-detail";
    detail.textContent = `${draft.body} · 置信度 ${Math.round((draft.confidence || 0) * 100)}%`;
    const evidence = document.createElement("code"); evidence.textContent = `证据：${(draft.evidenceMemoryIds || []).join(", ")}`;
    const actions = document.createElement("div"); actions.className = "memory-actions";
    for (const [action, label] of [["confirm", "确认认识"], ["reject", "否决"]]) {
      const button = document.createElement("button"); button.type = "button"; button.textContent = label;
      button.disabled = (action === "confirm" && draft.status === "confirmed") || (action === "reject" && draft.status === "rejected");
      button.addEventListener("click", async () => {
        try {
          setStatus(`正在${label}。`);
          await serviceApi("/v1/insight-drafts/actions", {method:"POST", body:JSON.stringify({userId:settings().userId, insightId:draft.id, action})});
          setStatus(`${label}完成。`, "ok"); await refresh();
        } catch (error) { setStatus(error.message, "error"); }
      });
      actions.append(button);
    }
    item.append(title, detail, evidence, actions); root.append(item);
  }
}

const STAGE_LABELS = {
  deeply_distant: "极度疏离", strongly_distant: "强烈疏离", distant: "疏离",
  acquaintance: "初识", familiar: "熟悉", close: "亲近", intimate: "亲密", deeply_bonded: "深度联结",
};
const STAGE_RANGES = [
  ["deeply_distant", -1200, -801], ["strongly_distant", -800, -401], ["distant", -400, -1],
  ["acquaintance", 0, 199], ["familiar", 200, 599], ["close", 600, 899], ["intimate", 900, 1199], ["deeply_bonded", 1200, 1200],
];

function renderLiving(service) {
  const living = service?.living || {};
  const affinity = living.affinity || {};
  const daily = living.daily || {};
  const chronotype = living.chronotype || {};
  const timeline = living.timeline || [];
  const expressions = living.expressions || [];
  const config = living.config || {};

  $("#affinity-stage").textContent = `${STAGE_LABELS[affinity.stage] || affinity.stage || "?"} · ${affinity.score ?? 0} 分 · ${affinity.interaction || "?"}`;
  const stageRoot = $("#affinity-stages"); stageRoot.replaceChildren();
  for (const [key, min, max] of STAGE_RANGES) {
    const item = document.createElement("article"); item.className = "memory-row";
    if (affinity.stage === key) item.style.outline = "1px solid var(--accent, #5b8def)";
    const title = document.createElement("strong"); title.textContent = `${STAGE_LABELS[key]}`;
    const detail = document.createElement("p"); detail.className = "memory-detail";
    detail.textContent = `${key} · ${min} ~ ${max} 分`;
    item.append(title, detail); stageRoot.append(item);
  }

  $("#daily-period").textContent = daily.period || "-";
  const dailyNode = $("#living-daily-summary");
  dailyNode.className = "summary";
  dailyNode.textContent = daily.energy != null
    ? `精力 ${daily.energy}/100${daily.moodBias ? ` · 心情底色「${daily.moodBias}」` : ""} · 活跃窗 ${daily.activeWindow ? "是" : "否"}`
    : "尚未读取。";
  const condRoot = $("#daily-conditions"); condRoot.replaceChildren();
  if (!(daily.conditions || []).length) condRoot.textContent = "今天还没有状态条件。";
  for (const c of (daily.conditions || [])) {
    const item = document.createElement("article"); item.className = "memory-row";
    const title = document.createElement("strong"); title.textContent = `${c.title || c.kind}：${c.label || ""}`;
    const detail = document.createElement("p"); detail.className = "memory-detail";
    detail.textContent = `精力 ${Number(c.energyDelta) > 0 ? "+" : ""}${c.energyDelta} · ${c.mood || "无情绪"} · ${c.cause || ""}`;
    item.append(title, detail); condRoot.append(item);
  }
  if (document.activeElement !== $("#chrono-wake")) $("#chrono-wake").value = chronotype.wakeMinute ?? 450;
  if (document.activeElement !== $("#chrono-sleep")) $("#chrono-sleep").value = chronotype.sleepMinute ?? 1350;

  $("#expr-count").textContent = `${expressions.length} 条`;
  const exprRoot = $("#expression-rules"); exprRoot.replaceChildren();
  if (!expressions.length) exprRoot.textContent = "还没有学到的表达。";
  for (const rule of expressions) {
    const item = document.createElement("article"); item.className = "memory-row";
    const title = document.createElement("strong"); title.textContent = `「${rule.pattern}」 · ${rule.scene}`;
    const detail = document.createElement("p"); detail.className = "memory-detail";
    detail.textContent = `状态 ${rule.status} · 已用 ${rule.usageCount || 0} 次`;
    const actions = document.createElement("div"); actions.className = "memory-actions";
    if (rule.status === "pending") {
      for (const [decision, label] of [["approved", "通过"], ["rejected", "拒绝"]]) {
        const b = document.createElement("button"); b.type = "button"; b.textContent = label;
        b.addEventListener("click", () => expressionAction(rule.id, decision)); actions.append(b);
      }
    }
    const del = document.createElement("button"); del.type = "button"; del.textContent = "删除";
    del.addEventListener("click", () => expressionAction(rule.id, "delete")); actions.append(del);
    item.append(title, detail, actions); exprRoot.append(item);
  }

  $("#timeline-count").textContent = `${timeline.length} 条`;
  const tlRoot = $("#self-timeline"); tlRoot.replaceChildren();
  if (!timeline.length) tlRoot.textContent = "还没有自我时间线事件。";
  for (const ev of timeline) {
    const item = document.createElement("article"); item.className = "memory-row";
    const title = document.createElement("strong"); title.textContent = `${ev.when || ""} · ${ev.summary || ev.type}`;
    const detail = document.createElement("p"); detail.className = "memory-detail";
    detail.textContent = `${ev.type} · ${ev.status}`;
    const actions = document.createElement("div"); actions.className = "memory-actions";
    const del = document.createElement("button"); del.type = "button"; del.textContent = "删除";
    del.addEventListener("click", () => timelineAction(ev.id)); actions.append(del);
    item.append(title, detail, actions); tlRoot.append(item);
  }

  if (document.activeElement !== $("#cfg-daily-limit")) $("#cfg-daily-limit").value = config?.proactive?.dailyLimit ?? 8;
  if (document.activeElement !== $("#cfg-min-interval")) $("#cfg-min-interval").value = config?.proactive?.minIntervalMinutes ?? 15;
  if (document.activeElement !== $("#cfg-expr-enabled")) $("#cfg-expr-enabled").checked = config?.expressionLearning?.enabled !== false;
}

async function expressionAction(ruleId, decision) {
  const isDelete = decision === "delete";
  const body = isDelete ? { userId: settings().userId, ruleId } : { userId: settings().userId, ruleId, decision };
  const path = isDelete ? "/v1/companion/expressions/delete" : "/v1/companion/expressions/review";
  try { setStatus("正在更新表达。"); await serviceApi(path, { method: "POST", body: JSON.stringify(body) }); setStatus("表达已更新。", "ok"); await refresh(); }
  catch (error) { setStatus(error.message, "error"); }
}

async function timelineAction(eventId) {
  try { setStatus("正在删除时间线事件。"); await serviceApi("/v1/companion/self-timeline/delete", { method: "POST", body: JSON.stringify({ userId: settings().userId, eventId }) }); setStatus("已删除。", "ok"); await refresh(); }
  catch (error) { setStatus(error.message, "error"); }
}

async function adjustAffinity(payload) {
  try { setStatus("正在调整好感度。"); await serviceApi("/v1/companion/affinity/adjust", { method: "POST", body: JSON.stringify({ userId: settings().userId, ...payload }) }); setStatus("好感度已更新。", "ok"); await refresh(); }
  catch (error) { setStatus(error.message, "error"); }
}

async function saveChronotype() {
  try {
    const wake = parseInt($("#chrono-wake").value, 10); const sleep = parseInt($("#chrono-sleep").value, 10);
    setStatus("正在保存作息。");
    await serviceApi("/v1/companion/chronotype", { method: "POST", body: JSON.stringify({ userId: settings().userId, wakeMinute: wake, sleepMinute: sleep }) });
    setStatus("作息已保存。", "ok"); await refresh();
  } catch (error) { setStatus(error.message, "error"); }
}

async function saveLivingConfig() {
  try {
    const config = {
      proactive: { dailyLimit: parseInt($("#cfg-daily-limit").value, 10), minIntervalMinutes: parseInt($("#cfg-min-interval").value, 10) },
      expressionLearning: { enabled: $("#cfg-expr-enabled").checked },
    };
    setStatus("正在保存配置。");
    await serviceApi("/v1/companion/living-config", { method: "POST", body: JSON.stringify({ userId: settings().userId, config }) });
    setStatus("配置已保存。", "ok"); await refresh();
  } catch (error) { setStatus(error.message, "error"); }
}

function render() {
  const desktopMemories = [
    ...(state.desktop?.collector?.facts || []),
    ...(state.desktop?.collector?.clues || []),
    ...(state.desktop?.collector?.emotions || []),
  ].filter((item) => memoryDate(item, state.desktop) === selectedDate());
  renderSummary(state.desktop);
  renderMemories("#desktop-memories", desktopMemories);
  renderMemories("#server-memories", state.service?.memories || []);
  $("#desktop-count").textContent = `${desktopMemories.length} 条`;
  $("#server-count").textContent = `${state.service?.memories?.length || 0} 条`;
  renderPersona(state.service);
  renderPersonaCard(state.service);
  renderRelationship(state.service);
  renderProfileMemories(state.service);
  renderInsightDrafts(state.service);
  renderLiving(state.service);
  renderPipeline();
  const health = $("#health");
  const both = Boolean(state.desktop && state.service);
  health.dataset.state = both ? "ok" : "error";
  health.querySelector("strong").textContent = both ? "链路可以验证" : "链路尚未连通";
  $("#health-detail").textContent = both ? "桌宠与共享服务均在线" : `${state.desktop ? "桌宠在线" : "桌宠离线"} / ${state.service ? "服务在线" : "服务离线"}`;
}

async function refresh() {
  setStatus("正在读取两端真实状态。");
  const userId = encodeURIComponent(settings().userId);
  const date = encodeURIComponent(selectedDate());
  const [desktop, service] = await Promise.allSettled([
    desktopApi("/memory/debug"),
    serviceApi(`/v1/debug/state?userId=${userId}&date=${date}`),
  ]);
  state.desktop = desktop.status === "fulfilled" ? desktop.value : null;
  state.service = service.status === "fulfilled" ? service.value : null;
  render();
  if (!state.desktop || !state.service) {
    const messages = [];
    if (!state.desktop) messages.push(`桌宠：${desktop.reason?.message || "连接失败"}`);
    if (!state.service) messages.push(`服务：${service.reason?.message || "连接失败"}`);
    setStatus(messages.join("；"), "error");
  } else setStatus("已刷新。所有数字都来自当前真实状态。", "ok");
}

async function runAction(button, task, success) {
  button.disabled = true;
  setStatus(`${button.querySelector("strong")?.textContent || button.textContent}进行中。`);
  try { const result = await task(); setStatus(success(result), "ok"); await refresh(); return result; }
  catch (error) { setStatus(error.message, "error"); throw error; }
  finally { button.disabled = false; }
}

$("#refresh").addEventListener("click", refresh);
$("#report").addEventListener("click", () => runAction($("#report"), () => desktopApi("/memory/report", {method:"POST", body:JSON.stringify({date:selectedDate()})}), (r) => `已整理 ${r.date}，${r.pendingCount} 个批次等待或正在同步。`).catch(()=>{}));
$("#sync").addEventListener("click", () => runAction($("#sync"), () => desktopApi("/memory/sync", {method:"POST", body:JSON.stringify({date:selectedDate()})}), (r) => r.syncStarted ? `已开始同步 ${r.date} 的 ${r.pendingCount} 个批次。` : `${r.date} 没有待同步批次。`).catch(()=>{}));
$("#opening").addEventListener("click", () => runAction($("#opening"), () => serviceApi("/v1/debug/openings/regenerate", {method:"POST", body:JSON.stringify({userId:settings().userId,date:selectedDate()})}), (r) => {
  state.opening = r;
  $("#opening-preview").textContent = r.text || "当前没有可用于开场的记忆。";
  $("#opening-evidence").textContent = `陪伴日：${r.episodeDate || selectedDate()}；引用记忆：${r.focusMemoryId || "无"}。这是预览，不推进手机消费游标。`;
  renderPipeline();
  return r.text ? "已基于当前服务端记忆重新生成开场。" : "没有可生成开场的有效记忆。";
}).catch(()=>{}));
$("#delete-tests").addEventListener("click", async () => {
  const ids = (state.service?.memories || []).map((m) => String(m.id || "")).filter((id) => id.startsWith("debug_"));
  if (!ids.length) { setStatus("当前没有 debug_ 开头的测试记忆。", "error"); return; }
  if (!confirm(`确定删除 ${ids.length} 条测试记忆吗？真实记忆不会被删除。`)) return;
  await runAction($("#delete-tests"), () => serviceApi("/v1/debug/memories", {method:"DELETE", body:JSON.stringify({userId:settings().userId,memoryIds:ids})}), (r) => `已删除 ${r.deleted} 条测试记忆。`).catch(()=>{});
});
$("#save-persona-draft").addEventListener("click", () => runAction($("#save-persona-draft"), savePersonaDraft, (r) => `草稿 v${r.draftVersion} 已保存，尚未影响正式对话。`).catch(()=>{}));
$("#publish-persona").addEventListener("click", () => runAction($("#publish-persona"), () => serviceApi("/v1/persona-card/actions", {method:"POST",body:JSON.stringify({userId:settings().userId,action:"publish"})}), (r) => `人设 v${r.activeVersion} 已发布。`).catch(()=>{}));
$("#rollback-persona").addEventListener("click", async () => {
  const version = Number(prompt("回滚到哪个历史版本？"));
  if (!version) return;
  await runAction($("#rollback-persona"), () => serviceApi("/v1/persona-card/actions", {method:"POST",body:JSON.stringify({userId:settings().userId,action:"rollback",version})}), (r) => `已从历史版本生成并发布 v${r.activeVersion}。`).catch(()=>{});
});
$("#preview-persona").addEventListener("click", async () => {
  const button = $("#preview-persona"); button.disabled = true;
  try {
    await savePersonaDraft();
    const result = await serviceApi("/v1/persona-card/preview", {method:"POST",body:JSON.stringify({userId:settings().userId,text:$("#persona-preview-input").value,useDraft:true})});
    $("#persona-preview-output").textContent = result.text;
    $("#persona-preview-meta").textContent = `草稿 v${result.version} · ${result.source} · 不写入正式对话`;
    $("#persona-compiled-prompt").textContent = result.compiledPrompt;
    setStatus("人设试演完成。", "ok"); await refresh();
  } catch (error) { setStatus(error.message, "error"); }
  finally { button.disabled = false; }
});
$("#save-settings").addEventListener("click", () => { saveSettings(); refresh(); });
$("#episode-date").addEventListener("change", () => { state.opening = null; refresh(); });

// 活着的陪伴控制台
$("#affinity-set").addEventListener("click", () => {
  const score = parseInt($("#affinity-score").value, 10);
  if (Number.isNaN(score)) { setStatus("先填一个 -1200~1200 的分数。", "error"); return; }
  adjustAffinity({ score });
});
$("#affinity-plus").addEventListener("click", () => adjustAffinity({ delta: 20 }));
$("#affinity-minus").addEventListener("click", () => adjustAffinity({ delta: -20 }));
$("#affinity-reset").addEventListener("click", async () => {
  if (!confirm("确定把好感度重置回「初识 0 分」吗？")) return;
  await adjustAffinity({ reset: true });
});
$("#chrono-save").addEventListener("click", saveChronotype);
$("#cfg-save").addEventListener("click", saveLivingConfig);

applySettings();
$("#episode-date").value = new Date().toLocaleDateString("sv-SE");
refresh();
