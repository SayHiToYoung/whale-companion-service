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
      // 服务端可以回一个错误信封；测试要能走到真实的错误分支，而不只是 happy path。
      if (data && data.__status && data.__status >= 400) {
        const {__status: status, ...payload} = data;
        return {ok: false, status, json: async () => payload};
      }
      return {ok: true, json: async () => data};
    },
  });
  const source = fs.readFileSync(path.join(__dirname, '../mobile/app.js'), 'utf8');
  // Boot is exercised by the browser; isolate sync from unrelated onboarding here.
  vm.runInContext(source.replace(/\nboot\(\);\s*$/, ''), context);
  const api = vm.runInContext(
    '({state, syncConversation, loadHistory, renderMessage, scheduleSync, elements,'
    + ' sendMessage, flushOutbox, outbox, storeOutbox, clientIdentity, authHeaders})', context);
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

test('every request carries an optional client identity the server never requires', async () => {
  const a = app(async () => ({messages: [], nextMessageSeq: 0}));
  await a.loadHistory();
  const headers = a.requests[0].headers;
  assert.equal(headers['X-Whale-Client-Kind'], 'mobile');
  assert.equal(headers['X-Whale-Api-Version'], '1');
  assert.match(headers['X-Whale-Client-Id'], /^client_/);
  assert.match(headers['X-Whale-Client-Capabilities'], /conversation\.send/);
  // 身份在请求体里也是同一份，且 clientId 跨请求稳定。
  const identity = a.clientIdentity();
  assert.equal(identity.clientId, headers['X-Whale-Client-Id']);
  assert.equal(identity.apiVersion, 1);
  assert.equal(JSON.stringify(a.clientIdentity()), JSON.stringify(identity));
});

test('sent messages and deliveries declare the same client in the body', async () => {
  const a = app(async (url, options) => {
    if (options.method === 'POST') return {assistantMessage: null, lifecycleUpdates: []};
    return {messages: [], nextMessageSeq: 0};
  });
  await a.sendMessage({messageId: 'user_1', text: '你好'});
  const body = JSON.parse(a.requests.find(r => r.method === 'POST').body);
  assert.equal(body.messageId, 'user_1');
  assert.equal(body.client.kind, 'mobile');
  // clientIdentity() 来自 vm realm，跨 realm 比较用 JSON 形状。
  assert.equal(JSON.stringify(body.client), JSON.stringify(a.clientIdentity()));
});

test('a retryable failure keeps the message queued for the next attempt', async () => {
  let attempts = 0;
  const a = app(async (url, options) => {
    if (options.method !== 'POST') return {messages: [], nextMessageSeq: 0};
    if (++attempts === 1) throw new Error('network down');
    return {assistantMessage: null, lifecycleUpdates: []};
  });
  a.storeOutbox([{messageId: 'user_1', text: '第一句'}]);
  await a.flushOutbox();
  // 断线是可重试的：同一个 messageId 留在待发箱里等下一次。
  assert.equal(a.outbox().length, 1);
  assert.equal(a.outbox()[0].messageId, 'user_1');
  await a.flushOutbox();
  assert.equal(a.outbox().length, 0);
  const ids = a.requests.filter(r => r.method === 'POST').map(r => JSON.parse(r.body).messageId);
  assert.deepEqual(ids, ['user_1', 'user_1']);
});

test('a non-retryable conflict leaves the outbox instead of replaying forever', async () => {
  const a = app(async (url, options) => {
    if (options.method !== 'POST') return {messages: [], nextMessageSeq: 0};
    return {__status: 409, error: 'messageId already exists with different payload',
      code: 'conversation.message_id_conflict',
      message: '同一个 messageId 已经用于另一段内容', retryable: false};
  });
  a.storeOutbox([{messageId: 'user_1', text: '冲突的一句'}, {messageId: 'user_2', text: '后面一句'}]);
  await a.flushOutbox();
  // 两条都试过了，都是不可重试的冲突，所以待发箱清空而不是无限重放。
  assert.equal(a.requests.filter(r => r.method === 'POST').length, 2);
  assert.equal(a.outbox().length, 0);
  assert.match(a.elements.status.textContent, /重试也不会成功/);
});

test('a retryable 4xx keeps the message queued instead of being dropped', async () => {
  // "4xx 一律不可重试"曾经是客户端的硬规则。429 说的是"此刻不行，等一下再来"，
  // 按状态码区间判断的话，这条消息会被当成永久失败就地丢掉。
  let attempts = 0;
  const a = app(async (url, options) => {
    if (options.method !== 'POST') return {messages: [], nextMessageSeq: 0};
    if (++attempts === 1) {
      return {__status: 429, error: '请求太频繁', code: 'request.rate_limited',
        message: '请求太频繁，按 Retry-After 退避后重试', retryable: true,
        status: 429, apiVersion: 1, retryAfterSeconds: 5};
    }
    return {assistantMessage: null, lifecycleUpdates: []};
  });
  a.storeOutbox([{messageId: 'user_1', text: '被限流的一句'}]);
  await a.flushOutbox();
  assert.equal(a.outbox().length, 1, '可重试的 429 不该把消息丢掉');
  assert.doesNotMatch(a.elements.status.textContent, /重试也不会成功/);
  // 退避之后原样重放同一个幂等键，服务端因此不会多出第二条。
  await a.flushOutbox();
  assert.equal(a.outbox().length, 0);
  const ids = a.requests.filter(r => r.method === 'POST').map(r => JSON.parse(r.body).messageId);
  assert.deepEqual(ids, ['user_1', 'user_1']);
});

test('the server can suggest how long to back off', async () => {
  const a = app(async (url, options) => {
    if (options.method !== 'POST') return {messages: [], nextMessageSeq: 0};
    return {__status: 408, error: '请求超时', code: 'request.timeout',
      message: '服务端等待请求体超时', retryable: true, status: 408,
      apiVersion: 1, retryAfterSeconds: 1};
  });
  a.storeOutbox([{messageId: 'user_1', text: '超时的一句'}]);
  await a.flushOutbox();
  assert.equal(a.outbox().length, 1);
});

test('an old-style error body with no envelope still fails safely', async () => {
  const a = app(async (url, options) => {
    if (options.method !== 'POST') return {messages: [], nextMessageSeq: 0};
    return {__status: 503, error: '记忆服务正在重启'};
  });
  a.storeOutbox([{messageId: 'user_1', text: '一句话'}]);
  await a.flushOutbox();
  // 没有 retryable 字段时按 HTTP 状态推断，503 视为可重试，消息留在待发箱。
  assert.equal(a.outbox().length, 1);
  assert.match(a.elements.status.textContent, /记忆服务正在重启/);
});

test('paging prefers the exact signal and stops without an extra empty page', async () => {
  const pages = [
    {messages: [{...message, messageSeq: 5, messageId: 'a'}], nextMessageSeq: 5,
      hasMore: true, hasMoreExact: true},
    // 最后一页刚好取满：保守的 hasMore 仍是 true，精确的 hasMoreExact 是 false。
    {messages: [{...message, messageSeq: 7, messageId: 'b'}], nextMessageSeq: 7,
      hasMore: true, hasMoreExact: false},
  ];
  let index = 0;
  const a = app(async () => pages[Math.min(index++, pages.length - 1)]);
  await a.loadHistory();
  assert.equal(index, 2, '精确信号应当在第二页就停下，而不是再翻一页空的');
  assert.equal(a.state.nextMessageSeq, 7);
  assert.equal(a.elements.conversation.children.length, 2);
});

test('an old server without hasMoreExact still pages exactly as before', async () => {
  let pages = 0;
  const a = app(async () => ++pages === 1
    ? {messages: [message], nextMessageSeq: 7, hasMore: true}
    : {messages: [], nextMessageSeq: 7, hasMore: true});
  await a.loadHistory();
  // 没有 hasMoreExact 时退回"游标必须前进"的老规则，行为与升级前逐字相同。
  assert.equal(pages, 2);
  assert.equal(a.state.nextMessageSeq, 7);
});
