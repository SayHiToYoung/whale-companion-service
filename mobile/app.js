"use strict";

const STORAGE_KEY = "whale-mobile-settings-v1";
const OUTBOX_KEY = "whale-mobile-outbox-v1";

const elements = {
  conversation: document.querySelector("#conversation"),
  loading: document.querySelector("#loading-state"),
  empty: document.querySelector("#empty-state"),
  handoff: document.querySelector("#handoff-line"),
  handoffText: document.querySelector("#handoff-text"),
  receipt: document.querySelector("#memory-receipt"),
  receiptText: document.querySelector("#receipt-text"),
  status: document.querySelector("#inline-status"),
  composer: document.querySelector("#composer"),
  input: document.querySelector("#message-input"),
  send: document.querySelector("#send-button"),
  greeting: document.querySelector("#greeting"),
  dialog: document.querySelector("#settings-dialog"),
  openSettings: document.querySelector("#open-settings"),
  saveSettings: document.querySelector("#save-settings"),
  settingsError: document.querySelector("#settings-error"),
  modelStatus: document.querySelector("#model-status"),
  serviceUrl: document.querySelector("#service-url"),
  token: document.querySelector("#pairing-token"),
  userId: document.querySelector("#user-id"),
  template: document.querySelector("#message-template"),
};

const state = {
  settings: loadSettings(),
  messageIds: new Set(),
  statusTimer: null,
  modelEnabled: false,
  nextMessageSeq: 0,
  syncTimer: null,
  syncing: false,
  sending: false,
  ready: false,
  lastDeliveryCheck: 0,
  lastInputAt: 0,
};

function randomId(prefix) {
  const value = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}_${value}`;
}

function loadSettings() {
  let saved = {};
  try {
    saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
  } catch (_) {
    saved = {};
  }
  return {
    url: String(saved.url || location.origin).replace(/\/$/, ""),
    token: String(saved.token || "local-dev-token"),
    userId: String(saved.userId || "local-user"),
    deviceId: String(saved.deviceId || randomId("phone")),
  };
}

function saveSettings() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state.settings));
}

function authHeaders() {
  return {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Authorization": `Bearer ${state.settings.token}`,
  };
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(`${state.settings.url}${path}`, {
      ...options,
      headers: {...authHeaders(), ...(options.headers || {})},
      cache: "no-store",
    });
  } catch (_) {
    throw new Error("连不上记忆服务，请检查网络或服务是否已启动");
  }
  let payload = {};
  try {
    payload = await response.json();
  } catch (_) {
    throw new Error(`记忆服务返回了无法读取的内容 (HTTP ${response.status})`);
  }
  if (!response.ok) {
    throw new Error(payload.error || `连接失败 (HTTP ${response.status})`);
  }
  return payload;
}

function setGreeting() {
  const hour = new Date().getHours();
  elements.greeting.textContent = hour < 6 ? "还没睡呀" : hour < 11 ? "早上好" : hour < 18 ? "下午好" : "晚上好";
}

function setHandoff(text, kind = "connected") {
  elements.handoff.dataset.state = kind;
  elements.handoffText.textContent = text;
}

function showStatus(text, kind = "info", duration = 3600) {
  clearTimeout(state.statusTimer);
  elements.status.textContent = text;
  elements.status.dataset.kind = kind;
  elements.status.hidden = false;
  state.statusTimer = setTimeout(() => {
    elements.status.hidden = true;
  }, duration);
}

function formatTime(value) {
  const date = new Date(value || Date.now());
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {hour: "2-digit", minute: "2-digit"}).format(date);
}

function renderMessage(message, options = {}) {
  const id = String(message.messageId || options.messageId || "");
  if (id && state.messageIds.has(id)) return null;
  if (id) state.messageIds.add(id);
  const fragment = elements.template.content.cloneNode(true);
  const article = fragment.querySelector(".message");
  const role = message.role === "user" ? "user" : "assistant";
  article.dataset.role = role;
  if (options.pending) article.dataset.pending = "true";
  if (options.failed) article.dataset.failed = "true";
  if (id) article.dataset.messageId = id;
  fragment.querySelector(".message-author").textContent = role === "user" ? "你" : "大鲸";
  fragment.querySelector(".message-text").textContent = String(message.text || "");
  fragment.querySelector("time").textContent = formatTime(message.createdAt);
  elements.conversation.appendChild(fragment);
  elements.empty.hidden = true;
  return elements.conversation.lastElementChild;
}

function summarizeMemory(memory) {
  if (!memory || typeof memory !== "object") return "";
  if (memory.layer === "L3") return "你主动说出的感受已经回到同一份记忆里。";
  const project = memory.project && typeof memory.project === "object" ? memory.project.name : "";
  const app = project || memory.app || memory.title || "今天的一段活动";
  const minutes = Math.max(1, Math.round(Number(memory.durationSeconds || 0) / 60));
  const duration = minutes >= 60
    ? `${Math.floor(minutes / 60)} 小时${minutes % 60 ? ` ${minutes % 60} 分钟` : ""}`
    : `${minutes} 分钟`;
  return `${app}，大约 ${duration}`;
}

function showReceipt(memory) {
  const text = summarizeMemory(memory);
  if (!text) return;
  elements.receiptText.textContent = text;
  elements.receipt.hidden = false;
}

function outbox() {
  try {
    const value = JSON.parse(localStorage.getItem(OUTBOX_KEY) || "[]");
    return Array.isArray(value) ? value : [];
  } catch (_) {
    return [];
  }
}

function storeOutbox(items) {
  localStorage.setItem(OUTBOX_KEY, JSON.stringify(items));
}

function messageArticle(messageId) {
  return Array.from(elements.conversation.querySelectorAll(".message")).find(
    (node) => node.dataset.messageId === messageId
  ) || null;
}

function renderPendingOutbox() {
  for (const item of outbox()) {
    renderMessage({...item, role: "user"}, {pending: true});
  }
}

async function loadHistory() {
  const settings = state.settings;
  let more = true;
  while (more) {
    const query = new URLSearchParams({userId: state.settings.userId,
      afterMessageSeq: String(state.nextMessageSeq), limit: "300"});
    const result = await api(`/v1/conversation/messages?${query}`);
    if (state.settings !== settings) return;
    for (const message of result.messages || []) renderMessage(message);
    const next = Number(result.nextMessageSeq);
    more = Boolean(result.hasMore) && next > state.nextMessageSeq;
    state.nextMessageSeq = Math.max(state.nextMessageSeq, next || 0);
  }
}

function scheduleSync() {
  clearTimeout(state.syncTimer);
  if (!document.hidden && state.ready) state.syncTimer = setTimeout(syncConversation, 30000);
}

async function syncConversation() {
  if (document.hidden || !state.ready || state.syncing) return;
  state.syncing = true;
  const settings = state.settings;
  try {
    await loadHistory();
    if (!state.ready || state.settings !== settings) return;
    const typing = elements.input.value.trim() || Date.now() - state.lastInputAt < 30000;
    if (!document.hidden && !state.sending && !typing && !outbox().length && Date.now() - state.lastDeliveryCheck >= 60000) {
      const key = `${STORAGE_KEY}:delivery:${state.settings.url}:${state.settings.userId}:${state.settings.deviceId}`;
      const deliveryId = localStorage.getItem(key) || randomId("delivery");
      localStorage.setItem(key, deliveryId);
      const result = await api("/v1/companion/proactive/deliveries", {
        method: "POST", body: JSON.stringify({userId: state.settings.userId,
          deviceId: state.settings.deviceId, deliveryId, activity: "active"}),
      });
      if (state.settings !== settings) return;
      localStorage.removeItem(key);
      state.lastDeliveryCheck = Date.now();
      if (result.assistantMessage) renderMessage(result.assistantMessage);
      await loadHistory();
    }
  } catch (_) {
    // Cursor and pending delivery ID survive failures; the next visible poll recovers.
  } finally {
    state.syncing = false;
    scheduleSync();
  }
}

document.addEventListener("visibilitychange", () => {
  clearTimeout(state.syncTimer);
  if (!document.hidden) void syncConversation();
});

async function loadCompanionStatus() {
  const result = await api("/health");
  const companion = result.companion || {};
  state.modelEnabled = Boolean(companion.modelEnabled);
  if (!state.modelEnabled) {
    elements.modelStatus.textContent = "对话模型尚未配置，当前使用严格事实型回复";
  } else if (companion.lastReplySource === "model") {
    elements.modelStatus.textContent = `模型已配置：${companion.model}。上一轮由模型回复`;
  } else if (companion.lastReplySource === "fallback") {
    elements.modelStatus.textContent = `模型已配置：${companion.model}。上一轮使用了事实型兜底`;
  } else {
    elements.modelStatus.textContent = `模型配置已加载：${companion.model}。发送消息后可确认实际来源`;
  }
}

async function sendMessage(item, article = null) {
  state.sending = true;
  try {
    showStatus(
      state.modelEnabled ? "大鲸正在想怎么回应你。" : "大鲸正在整理记忆里的线索。",
      "info",
      30000
    );
    const result = await api("/v1/conversation/messages", {
      method: "POST",
      body: JSON.stringify({
        userId: state.settings.userId,
        deviceId: state.settings.deviceId,
        messageId: item.messageId,
        role: "user",
        text: item.text,
      }),
    });
    const remaining = outbox().filter((row) => row.messageId !== item.messageId);
    storeOutbox(remaining);
    if (article) {
      delete article.dataset.pending;
      delete article.dataset.failed;
    }
    const updates = Array.isArray(result.lifecycleUpdates) ? result.lifecycleUpdates : [];
    const lifecycle = updates.length ? updates[updates.length - 1] : null;
    if (lifecycle?.status === "suppressed") {
      showStatus("记住了。大鲸以后不会主动提这件事。", "info", 5200);
    } else if (lifecycle?.status === "resolved") {
      showStatus("这件事已经更新为过去状态，不会再当成你现在的感受。", "info", 5200);
    } else if (lifecycle?.status === "dormant") {
      showStatus("先放下。大鲸不会继续追着问。", "info", 5200);
    } else if (lifecycle?.status === "active") {
      showStatus("大鲸知道这件事还在继续。", "info", 5200);
    } else if (result.replySource === "fallback" && state.modelEnabled) {
      showStatus("模型暂时没有回应，大鲸先按记忆里的事实回你。", "info", 5200);
    } else if (result.emotionRecorded) {
      showStatus("你明确说出的感受，大鲸已经记住了。", "info");
    } else {
      showStatus("大鲸听见了。没有说清的情绪，她不会替你定义。", "info");
    }
    if (result.assistantMessage) {
      renderMessage(result.assistantMessage);
    }
    setHandoff("你的话已经回到同一份记忆里");
    try {
      await loadCompanionStatus();
    } catch (_) {
      // 消息已经成功写入；状态刷新失败不应把已发送消息重新放回待发箱。
    }
    return true;
  } catch (error) {
    if (article) {
      delete article.dataset.pending;
      article.dataset.failed = "true";
    }
    showStatus(`暂时没送出去，联网后会再试。${error.message}`, "error", 6000);
    return false;
  } finally {
    state.sending = false;
  }
}

async function flushOutbox() {
  for (const item of outbox()) {
    const article = messageArticle(item.messageId)
      || renderMessage({...item, role: "user"}, {pending: true});
    const ok = await sendMessage(item, article);
    if (!ok) break;
  }
}

async function boot() {
  setGreeting();
  saveSettings();
  elements.loading.hidden = false;
  elements.empty.hidden = true;
  try {
    try {
      await loadCompanionStatus();
    } catch (_) {
      state.modelEnabled = false;
      elements.modelStatus.textContent = "暂时无法确认对话模型状态";
    }
    let historyError = null;
    try {
      await loadHistory();
    } catch (error) {
      historyError = error;
    }
    renderPendingOutbox();
    await flushOutbox();
    if (historyError) throw historyError;
    setHandoff(elements.handoffText.textContent, "connected");
    elements.empty.hidden = state.messageIds.size > 0;
  } catch (error) {
    setHandoff("暂时没有连上共享记忆", "error");
    showStatus(error.message, "error", 7000);
    elements.empty.hidden = state.messageIds.size > 0;
  } finally {
    elements.loading.hidden = true;
    state.ready = true;
    void syncConversation();
  }
}

elements.composer.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = elements.input.value.trim();
  if (!text) return;
  const item = {
    messageId: randomId("user"),
    text,
    createdAt: new Date().toISOString(),
  };
  storeOutbox([...outbox(), item]);
  const article = renderMessage({...item, role: "user"}, {pending: true});
  elements.input.value = "";
  elements.input.style.height = "auto";
  elements.send.disabled = true;
  await sendMessage(item, article);
  elements.send.disabled = false;
  elements.input.focus();
});

elements.input.addEventListener("input", () => {
  state.lastInputAt = Date.now();
  elements.input.style.height = "auto";
  const maxHeight = parseFloat(getComputedStyle(elements.input).maxHeight) || 132;
  elements.input.style.height = `${Math.min(elements.input.scrollHeight, maxHeight)}px`;
});

elements.openSettings.addEventListener("click", () => {
  elements.serviceUrl.value = state.settings.url;
  elements.token.value = state.settings.token;
  elements.userId.value = state.settings.userId;
  elements.settingsError.hidden = true;
  if (typeof elements.dialog.showModal === "function") {
    elements.dialog.showModal();
  } else {
    // iOS Safari < 15.4 不支持 <dialog>.showModal()，降级为 open 属性
    elements.dialog.setAttribute("open", "");
  }
});

elements.saveSettings.addEventListener("click", async () => {
  if (!elements.serviceUrl.reportValidity() || !elements.token.reportValidity() || !elements.userId.reportValidity()) return;
  const previousSettings = state.settings;
  state.ready = false;
  clearTimeout(state.syncTimer);
  state.settings = {...previousSettings, url: elements.serviceUrl.value.trim().replace(/\/$/, ""),
    token: elements.token.value, userId: elements.userId.value.trim()};
  elements.saveSettings.disabled = true;
  try {
    await api("/v1/conversation/messages?" + new URLSearchParams({
      userId: state.settings.userId,
      afterMessageSeq: "0",
      limit: "1",
    }));
    saveSettings();
    elements.dialog.close();
    location.reload();
  } catch (error) {
    state.settings = previousSettings;
    state.ready = true;
    scheduleSync();
    elements.settingsError.textContent = error.message;
    elements.settingsError.hidden = false;
  } finally {
    elements.saveSettings.disabled = false;
  }
});

window.addEventListener("online", flushOutbox);

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

boot();
