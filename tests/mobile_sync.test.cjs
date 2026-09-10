// Browser-independent execution of the real PWA sync/rendering functions.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

function app(handler) {
  const nodes = new Map(), events = {}, timers = new Map(), storage = new Map(), requests = [];
  let timerId = 0;
  const node = () => ({value: '', style: {}, dataset: {}, hidden: false, children: [],
    addEventListener() {}, querySelectorAll() { return this.children; },
    appendChild(fragment) { this.children.push(fragment.querySelector('.message')); this.lastElementChild = this.children.at(-1); }});
  const document = {hidden: false,
    querySelector(selector) { if (!nodes.has(selector)) nodes.set(selector, node()); return nodes.get(selector); },
    addEventListener(name, fn) { events[name] = fn; },
  };
  document.querySelector('#message-template').content = {cloneNode() {
    const parts = new Map();
    return {querySelector(selector) { if (!parts.has(selector)) parts.set(selector, node()); return parts.get(selector); }};
  }};
  const context = vm.createContext({document, console, URLSearchParams, Date, Intl,
    location: {origin: 'http://localhost'}, navigator: {}, window: {addEventListener() {}},
    localStorage: {getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
    setTimeout(fn, delay) { timers.set(++timerId, {fn, delay}); return timerId; },
    clearTimeout(id) { timers.delete(id); },
    async fetch(url, options) {
      requests.push({url, ...options});
      const data = await handler(url, options);
      return {ok: true, json: async () => data};
    },
  });
  const source = fs.readFileSync(path.join(__dirname, '../mobile/app.js'), 'utf8');
  // Boot is exercised by the browser; isolate sync from unrelated onboarding here.
  vm.runInContext(source.replace(/\nboot\(\);\s*$/, ''), context);
  const api = vm.runInContext('({state, syncConversation, loadHistory, renderMessage, scheduleSync, elements})', context);
  api.state.ready = true;
  return {...api, document, events, timers, storage, requests};
}

const message = {messageId: 'proactive-stable', deviceId: 'proactive', messageSeq: 7,
  role: 'assistant', text: '虚拟日常：在窗边听雨。我偏爱雨声胜过完全的安静。', createdAt: '2026-09-09T09:00:00Z'};

test('POST result and incremental history render the same assistant only once', async () => {
  let committed = false;
  const a = app(async (url, options) => {
    if (options.method === 'POST') { committed = true; return {assistantMessage: message}; }
    return {messages: committed ? [message] : [], nextMessageSeq: committed ? 7 : 0};
  });
  await a.syncConversation();
  assert.equal(a.elements.conversation.children.length, 1);
  assert.equal(a.elements.conversation.children[0].dataset.messageId, message.messageId);
  assert.equal(a.state.nextMessageSeq, 7);
  await a.syncConversation();
  assert.match(a.requests.at(-1).url, /afterMessageSeq=7/);
  assert.equal(a.elements.conversation.children.length, 1);
  assert.equal(a.requests.filter(r => r.method === 'POST').length, 1);
  assert.equal([...a.timers.values()][0].delay, 30000);
});

test('hidden pages stop polling and returning to foreground immediately syncs', async () => {
  const a = app(async () => ({messages: [message], nextMessageSeq: 7}));
  a.state.lastDeliveryCheck = Date.now();
  a.scheduleSync();
  a.document.hidden = true;
  a.events.visibilitychange();
  await a.syncConversation();
  assert.equal(a.requests.length, 0);
  assert.equal(a.timers.size, 0);
  a.document.hidden = false;
  a.events.visibilitychange();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(a.requests.length, 1);
  assert.equal(a.elements.conversation.children.length, 1);
});

test('failed delivery reuses its id and recovers a committed message', async () => {
  let attempts = 0;
  const ids = [];
  const a = app(async (url, options) => {
    if (options.method === 'POST') {
      ids.push(JSON.parse(options.body).deliveryId);
      if (++attempts === 1) throw new Error('response lost after commit');
      return {assistantMessage: message};
    }
    return {messages: attempts ? [message] : [], nextMessageSeq: attempts ? 7 : 0};
  });
  await a.syncConversation();
  await a.syncConversation();
  assert.equal(ids.length, 2);
  assert.equal(ids[0], ids[1]);
  assert.equal(a.elements.conversation.children.length, 1);
});

test('typing/sending inhibits delivery but not receiving desktop messages', async () => {
  const a = app(async () => ({messages: [message], nextMessageSeq: 7}));
  a.elements.input.value = '正在输入';
  await a.syncConversation();
  a.elements.input.value = '';
  a.state.sending = true;
  await a.syncConversation();
  assert.equal(a.requests.filter(r => r.method === 'POST').length, 0);
  assert.equal(a.elements.conversation.children.length, 1);
});

test('incremental paging follows nextMessageSeq and does not loop on an empty page', async () => {
  let pages = 0;
  const a = app(async () => ++pages === 1
    ? {messages: [message], nextMessageSeq: 7, hasMore: true}
    : {messages: [], nextMessageSeq: 7, hasMore: true});
  await a.loadHistory();
  assert.equal(pages, 2);
  assert.match(a.requests[1].url, /afterMessageSeq=7/);
});

test('in-flight old-account response cannot contaminate a new conversation', async () => {
  let release;
  const a = app(() => new Promise(resolve => { release = resolve; }));
  const pending = a.loadHistory();
  a.state.settings = {...a.state.settings, userId: 'another-user'};
  release({messages: [message], nextMessageSeq: 7});
  await pending;
  assert.equal(a.elements.conversation.children.length, 0);
  assert.equal(a.state.nextMessageSeq, 0);
});
