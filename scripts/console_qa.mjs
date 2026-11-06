/* RoutePilot 控制台 QA 基座（Task 10 拆分后）
   ------------------------------------------------------------------
   本文件只保留：服务启动（Node mock + `python -m router` 子进程）、浏览器、
   管理面 API、用例注册器/执行器、共享断言助手与 teardown。页面用例分别位于
   scripts/console_qa/{canvas,inspector,tryrun,logs,models}.mjs，各模块只调用
   本文件的 test(name, fn) 注册用例，不复制启动逻辑、不持有页面状态。

   console_qa.mjs 顶部保留的 8 条用例是基座自检（健康检查 / 原生 ESM / mock wire
   shape / bootstrap / 真实 /admin/try），不属于任何页面模块。

   Usage: node scripts/console_qa.mjs            # 全量（= npm run check:console）
          QA_ONLY=<regex> node scripts/console_qa.mjs
          node scripts/console_qa/canvas.mjs      # 只跑单个页面模块
   Exports: test(name, fn) / runChecks() / runIfMain(metaUrl) / api(...) / page
   ------------------------------------------------------------------ */
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import http from 'node:http';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright-core';

/* 控制台源码是原生 ES module：Node 侧可直接导入纯模块常量，
   页面内则用动态 import() 读取同一份模块实例（不暴露 window 全局）。 */
import { HISTORY_LIMIT } from '../router/console/routing/draft.js';
import { MIN_K } from '../router/console/routing/canvas.js';

export { HISTORY_LIMIT, MIN_K };

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const EXAMPLE_PATH = path.join(ROOT, 'router/console/routing/example.json');
const SCENARIO_PATH = path.join(ROOT, 'tests/fixtures/graph_scenarios.json');
const SHOT_DIR = path.join(ROOT, 'docs/od-design/routing-canvas-2026-11-02/implementation-qa');
export const TREE_ID = 'power-priority';
export const JEV_MODEL = 'd1-2B';
export const S1_TEXT = '10kV 线路跳闸，请安排抢修工单并做故障定位。';
export const S1_NOW = '2026-11-02T12:00:00+08:00';
const API_TIMEOUT_MS = 15000;
/* Screenshot writes are opt-in: a plain `npm run check:console` must not dirty
   the committed evidence PNGs. Regenerate with QA_SCREENSHOTS=1. */
const WRITE_SHOTS = process.env.QA_SCREENSHOTS === '1';

/* 示例图是 15 节点图的唯一来源：节点/边数、start 坐标、兜底目标都从 fixture 派生，
   避免 QA 里散落 15 / 20,150 / standard-target 这类字面量。 */
export const EXAMPLE = JSON.parse(fs.readFileSync(EXAMPLE_PATH, 'utf8'));
export const EXAMPLE_NODES = EXAMPLE.nodes.length;
export const EXAMPLE_EDGES = EXAMPLE.edges.length;
export const EXAMPLE_START = EXAMPLE.nodes.find(node => node.id === 'start').position;
export const EXAMPLE_TARGETS = { fallback: EXAMPLE.error_fallback };
/* 示例图只有 root；envelope 元数据是 QA 基座约定，集中一处供 bootstrap / 基线 / 断言复用。 */
export const BASELINE = {
  name: '电力优先决策组',
  timezone: 'Asia/Shanghai',
  sticky: { enabled: true, ttl_seconds: 1800 }
};

/* ── 用例注册器 ─────────────────────────────────────────────────────── */

const checks = [];

export function test(name, fn) {
  checks.push({ name, fn });
}

export async function runChecks() {
  const failures = [];
  /* QA_ONLY 只用于本地定位单条用例；标准命令不设置它，仍跑完整矩阵。 */
  const filter = process.env.QA_ONLY ? new RegExp(process.env.QA_ONLY) : null;
  let ran = 0;
  console.log(WRITE_SHOTS
    ? 'screenshots: enabled (QA_SCREENSHOTS=1)'
    : 'screenshots: skipped (set QA_SCREENSHOTS=1)');
  for (const check of checks) {
    if (filter && !filter.test(check.name)) continue;
    ran += 1;
    try {
      await check.fn();
      console.log(`PASS ${check.name}`);
} catch (error) {
      failures.push(`${check.name}: ${error.stack || error}`);
}
}
  try {
    assertRepoDbUnchanged();
} catch (error) {
    failures.push(String(error && error.message ? error.message : error));
}
  if (pageErrors.length) {
    failures.push('browser pageerror/console error:\n' + pageErrors.join('\n'));
}
  if (externalRequests.length) {
    failures.push('external requests:\n' + externalRequests.join('\n'));
}
  if (failures.length) throw new Error(failures.join('\n\n'));
  console.log(`ALL PASS (${ran})`);
}

/* ── 运行期状态（导出给用例使用） ───────────────────────────────────── */

export let page = null;
export let mockBase = '';
export let routerBase = '';

let browser = null;
let routerChild = null;
let mockServer = null;
export let tempDir = '';
let routerLog = '';
let repoDbFingerprint = null;

const pageErrors = [];
const externalRequests = [];
/* 故意制造的 4xx 会让 Chromium 打印 "Failed to load resource" console error。
   用例必须同时声明精确的请求 URL 与状态码；监听器只吞掉完全匹配的那一条，
   其他 4xx（哪怕同一次点击里的）仍然记为 console error。 */
export const expectedHttpStatusErrors = [];
export function expectHttpStatusError(matcher) {
  expectedHttpStatusErrors.push(matcher);
}
export function resetExpectedHttpStatusErrors() {
  expectedHttpStatusErrors.length = 0;
}
export const mockState = { systemoneCalls: [], chatCalls: [], scenarioCalls: {} };

export async function screenshot(name) {
  if (!WRITE_SHOTS) return;
  await fsp.mkdir(SHOT_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SHOT_DIR, name) });
}

/* 仓库里的 router.db 必须与运行前完全一致：QA 只能写临时库。 */
function repoDbSignature() {
  const repoDb = path.join(ROOT, 'router.db');
  if (!fs.existsSync(repoDb)) return null;
  const stat = fs.statSync(repoDb);
  return stat.size + ':' + stat.mtimeMs;
}

export function assertRepoDbUnchanged() {
  assert.equal(repoDbSignature(), repoDbFingerprint,
    'the QA run modified the repository router.db (expected the temp database only)');
}

/* ── 通用工具 ───────────────────────────────────────────────────────── */

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      server.close(() => resolve(port));
});
});
}

function readJsonBody(request) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    request.on('data', chunk => {
      size += chunk.length;
      if (size > 5 * 1024 * 1024) {
        reject(new Error('mock request body too large'));
        request.destroy();
        return;
}
      chunks.push(chunk);
});
    request.on('end', () => {
      try {
        const text = Buffer.concat(chunks).toString('utf8');
        resolve(text ? JSON.parse(text) : {});
} catch (error) {
        reject(error);
}
});
    request.on('error', reject);
});
}

function sendJson(response, status, body) {
  response.statusCode = status;
  response.setHeader('content-type', 'application/json; charset=utf-8');
  response.end(JSON.stringify(body));
}

/* ── mock：/v1/systemone + /v1/chat/completions ─────────────────────── */

function loadScenarioFixture() {
  const fixture = JSON.parse(fs.readFileSync(SCENARIO_PATH, 'utf8'));
  const optionKeys = {};
  (EXAMPLE.nodes || []).forEach(node => {
    if (node.type === 'question' && node.question_type === 'choice') {
      optionKeys[node.id] = (node.options || []).map(option => option.key);
}
});
  return { periods: fixture.periods || [], scenarios: fixture.scenarios || [], optionKeys };
}

export const FIXTURE = loadScenarioFixture();

function pickScenario(state) {
  const matched = FIXTURE.scenarios.find(scenario => state.indexOf(scenario.text) >= 0);
  return matched || { id: 'manual', text: '', answers: {}, fail: null };
}

function normalizeProbabilities(values) {
  const total = values.reduce((sum, value) => sum + value, 0);
  if (!(total > 0)) return values.map(() => 1 / values.length);
  return values.map(value => value / total);
}

function choiceAnswer(nodeId, question, fixture) {
  const labels = Object.keys(question.criteria || {});
  const keys = FIXTURE.optionKeys[nodeId];
  const probsByKey = (fixture && fixture.probs) || {};
  let values = labels.map((label, index) => {
    const key = Array.isArray(keys) && keys.length === labels.length ? keys[index] : null;
    const value = key && typeof probsByKey[key] === 'number' ? probsByKey[key] : null;
    return value;
});
  if (values.every(value => value === null)) values = labels.map(() => 1 / Math.max(1, labels.length));
  values = normalizeProbabilities(values.map(value => (value === null ? 0 : Math.max(0, value))));
  let best = 0;
  values.forEach((value, index) => { if (value > values[best]) best = index; });
  const probabilities = {};
  labels.forEach((label, index) => { probabilities[label] = values[index]; });
  return {
    type: 'choice',
    choice: labels[best],
    probabilities,
    confidence: values[best]
};
}

function noulAnswer(fixture) {
  const yes = fixture && typeof fixture.yes === 'number' ? fixture.yes : 0.5;
  return { type: 'noul', noul: yes };
}

function scoreAnswer(question, fixture) {
  const criteria = Array.isArray(question.criteria) ? question.criteria : [];
  const legend = {};
  const probabilities = {};
  let selected = 0;
  if (criteria.length) {
    criteria.forEach((level, index) => { legend[String(index)] = level; });
    if (fixture && typeof fixture.value === 'number') {
      let bestDistance = Infinity;
      criteria.forEach((level, index) => {
        const distance = Math.abs(level - fixture.value);
        if (distance < bestDistance) {
          bestDistance = distance;
          selected = index;
}
});
} else if (criteria.length > 1) {
      selected = 1;
}
    criteria.forEach((level, index) => {
      probabilities[String(index)] = index === selected ? 0.82 : 0.18 / Math.max(1, criteria.length - 1);
});
}
  return {
    type: 'score',
    score: selected,
    legend,
    probabilities,
    confidence: 0.82
};
}

function answerFor(nodeId, question, scenario) {
  const type = question && question.type;
  const fixture = (scenario.answers || {})[nodeId] || null;
  if (type === 'choice') return choiceAnswer(nodeId, question, fixture);
  if (type === 'noul') return noulAnswer(fixture);
  if (type === 'score') return scoreAnswer(question, fixture);
  throw new Error(`mock does not support question type ${type} (${nodeId})`);
}

function startMock() {
  return new Promise((resolve, reject) => {
    const server = http.createServer(async (request, response) => {
      try {
        const url = new URL(request.url || '/', 'http://127.0.0.1');
        if (request.method === 'POST' && url.pathname === '/v1/systemone') {
          const body = await readJsonBody(request);
          const state = typeof body.state === 'string' ? body.state : '';
          const scenario = pickScenario(state);
          const questionKeys = Object.keys(body.questions || {});
          mockState.scenarioCalls[scenario.id] = (mockState.scenarioCalls[scenario.id] || 0) + 1;
          mockState.systemoneCalls.push({ scenario: scenario.id, keys: questionKeys });

          /* s5「决策服务超时」：第 1 波整波失败（504）；后续波（例如只问 clarify）
             不在场景 answers 里，仍可正常应答，便于验证「整波作废」后的兜底路径。 */
          const firstWave = scenario.fail && questionKeys.length > 0 &&
            questionKeys.every(key => Object.prototype.hasOwnProperty.call(scenario.answers || {}, key));
          if (firstWave) {
            sendJson(response, 504, {
              detail: 'mock decision timeout',
              scenario: scenario.id,
              message: scenario.fail.message || ''
});
            return;
}
          const answers = {};
          questionKeys.forEach(nodeId => {
            answers[nodeId] = answerFor(nodeId, body.questions[nodeId], scenario);
});
          sendJson(response, 200, {
            model: body.model || JEV_MODEL,
            answers,
            usage: { input_tokens: 172, output_tokens: 0 }
});
          return;
}
        if (request.method === 'POST' && url.pathname === '/v1/chat/completions') {
          const body = await readJsonBody(request);
          mockState.chatCalls.push(body);
          const model = body.model || 'mock-upstream';
          const created = Math.floor(Date.now() / 1000);
          if (body.stream) {
            response.writeHead(200, {
              'content-type': 'text/event-stream; charset=utf-8',
              'cache-control': 'no-cache',
              connection: 'keep-alive'
});
            response.write('data: ' + JSON.stringify({
              id: 'chatcmpl-qa', object: 'chat.completion.chunk', created, model,
              choices: [{ index: 0, delta: { role: 'assistant', content: '固定回复' }, finish_reason: null }]
}) + '\n\n');
            response.write('data: ' + JSON.stringify({
              id: 'chatcmpl-qa', object: 'chat.completion.chunk', created, model,
              choices: [{ index: 0, delta: {}, finish_reason: 'stop' }]
}) + '\n\n');
            response.write('data: [DONE]\n\n');
            response.end();
            return;
}
          sendJson(response, 200, {
            id: 'chatcmpl-qa',
            object: 'chat.completion',
            created,
            model,
            choices: [{
              index: 0,
              message: { role: 'assistant', content: '这是 console QA 的固定模型回复。' },
              finish_reason: 'stop'
}],
            usage: { prompt_tokens: 12, completion_tokens: 8, total_tokens: 20 }
});
          return;
}
        if (request.method === 'GET' && (url.pathname === '/healthz' || url.pathname === '/')) {
          sendJson(response, 200, { ok: true, mock: true });
          return;
}
        sendJson(response, 404, { error: 'not found' });
} catch (error) {
        sendJson(response, 500, { error: String(error && error.message ? error.message : error) });
}
});
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      mockBase = `http://127.0.0.1:${server.address().port}`;
      resolve(server);
});
});
}

/* ── router 子进程 ──────────────────────────────────────────────────── */

function pythonExecutable() {
  const venv = path.join(ROOT, '.venv/bin/python');
  return fs.existsSync(venv) ? venv : 'python3';
}

function capture(child) {
  const append = chunk => { routerLog = (routerLog + chunk.toString()).slice(-20000); };
  if (child.stdout) child.stdout.on('data', append);
  if (child.stderr) child.stderr.on('data', append);
}

async function startRouter(port) {
  const child = spawn(pythonExecutable(), ['-m', 'router'], {
    cwd: ROOT,
    env: {
      ...process.env,
      ROUTER_DB: path.join(tempDir, 'router.db'),
      ROUTER_GRAPH_DIR: path.join(tempDir, 'routing-graph'),
      ROUTER_HOST: '127.0.0.1',
      ROUTER_PORT: String(port),
      ROUTER_USER_AUTH: '0',
      ROUTER_JEV_URL: mockBase,
      ROUTER_JEV_MODEL: JEV_MODEL,
      ROUTER_JEV_TIMEOUT: '5',
      PYTHONUNBUFFERED: '1'
},
    stdio: ['ignore', 'pipe', 'pipe']
});
  routerChild = child;
  capture(child);
  child.on('exit', code => {
    if (routerChild === child) {
      routerChild = null;
      routerLog += `\n[router exited with code ${code}]`;
}
});

  const deadline = Date.now() + 45000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error('router exited during startup (code ' + child.exitCode + ')\n' + routerLog);
}
    try {
      const response = await fetch(`http://127.0.0.1:${port}/healthz`, { signal: AbortSignal.timeout(1500) });
      if (response.ok) {
        const body = await response.json();
        if (body && body.ok) return child;
}
} catch {
      /* 还没起来，继续轮询 */
}
    await new Promise(resolve => setTimeout(resolve, 200));
}
  throw new Error('router did not become ready in 45s\n' + routerLog);
}

async function stopRouter() {
  const child = routerChild;
  if (!child) return;
  routerChild = null;
  if (child.exitCode !== null) return;
  await new Promise(resolve => {
    const timer = setTimeout(() => {
      try { child.kill('SIGKILL'); } catch { /* ignore */ }
}, 5000);
    child.once('exit', () => {
      clearTimeout(timer);
      resolve();
});
    try { child.kill('SIGTERM'); } catch { clearTimeout(timer); resolve(); }
});
}

/* ── 管理面 API + bootstrap ─────────────────────────────────────────── */

export async function api(method, apiPath, body) {
  if (!routerBase) throw new Error('router is not ready');
  const init = { method, headers: {} };
  if (body !== undefined) {
    init.headers['content-type'] = 'application/json';
    init.body = JSON.stringify(body);
}
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), API_TIMEOUT_MS);
  init.signal = controller.signal;
  try {
    const response = await fetch(routerBase + apiPath, init);
    const text = await response.text();
    let data = text;
    try { data = text ? JSON.parse(text) : null; } catch { /* 保留文本 */ }
    if (!response.ok) {
      const detail = typeof data === 'string' ? data : JSON.stringify(data);
      throw new Error(`${method} ${apiPath} → HTTP ${response.status}: ${detail}`);
}
    return data;
} catch (error) {
    if (controller.signal.aborted) {
      throw new Error(`${method} ${apiPath} → request timed out after ${API_TIMEOUT_MS}ms`);
}
    throw error;
} finally {
    clearTimeout(timer);
}
}

const MODEL_PROFILE = {
  domains: [],
  tier: 'large',
  reasoning: {
    format: 'openai',
    efforts: { low: 'low', medium: 'medium', high: 'high' },
    default: 'medium'
}
};

async function bootstrap() {
  /* 1. local provider → mock（/v1/chat/completions 由 mock 承接）。 */
  await api('PUT', '/admin/providers/local', {
    base_url: mockBase + '/v1',
    key_env: '',
    reasoning_format: 'openai'
});

  /* 2. 示例图引用的四个模型，均声明 low/medium/high 推理档位。 */
  for (const modelId of ['deepseek-v3', 'qwen3-8b', 'qwen3-32b']) {
    await api('PUT', '/admin/models/' + encodeURIComponent(modelId), {
      upstream: modelId,
      provider: 'local',
      profile: MODEL_PROFILE
});
}
  await api('POST', '/admin/models', {
    id: 'qwen3.8-flash-sft',
    upstream: 'qwen3.8-flash-sft',
    provider: 'local',
    profile: MODEL_PROFILE
});

  /* 3. peak / normal / offpeak 三个固定时段（与 graph_scenarios.json 一致）。 */
  await api('PUT', '/admin/time-periods', { periods: FIXTURE.periods });

  /* 4. 决策服务指向 mock。 */
  await api('PUT', '/admin/jev', {
    base_url: mockBase,
    model: JEV_MODEL,
    timeout_seconds: 5
});

  /* 5. 建图并启用（root 原样使用 Task 3 的 graph_power 示例）。 */
  await api('POST', '/admin/trees', {
    id: TREE_ID,
    name: BASELINE.name,
    timezone: BASELINE.timezone,
    sticky: BASELINE.sticky,
    root: EXAMPLE
});
  await api('POST', '/admin/trees/' + TREE_ID + '/enable');
}

/* ── 浏览器 ─────────────────────────────────────────────────────────── */

function chromiumExecutable() {
  if (process.env.CHROME) return process.env.CHROME;
  const caches = [
    path.join(os.homedir(), 'Library/Caches/ms-playwright'),
    path.join(os.homedir(), '.cache/ms-playwright')
  ];
  const candidates = [];
  for (const cache of caches) {
    if (!fs.existsSync(cache)) continue;
    for (const entry of fs.readdirSync(cache).sort()) {
      const base = path.join(cache, entry);
      if (entry.startsWith('chromium-')) {
        candidates.push(
          path.join(base, 'chrome-mac-arm64', 'Google Chrome for Testing.app', 'Contents', 'MacOS', 'Google Chrome for Testing'),
          path.join(base, 'chrome-mac-x64', 'Google Chrome for Testing.app', 'Contents', 'MacOS', 'Google Chrome for Testing'),
          path.join(base, 'chrome-mac', 'Google Chrome for Testing.app', 'Contents', 'MacOS', 'Google Chrome for Testing'),
          path.join(base, 'chrome-linux', 'chrome'),
          path.join(base, 'chrome-linux64', 'chrome'),
          path.join(base, 'chrome-win', 'chrome.exe'),
          path.join(base, 'chrome-win64', 'chrome.exe')
        );
} else if (entry.startsWith('chromium_headless_shell-')) {
        candidates.push(
          path.join(base, 'chrome-headless-shell-mac-arm64', 'chrome-headless-shell'),
          path.join(base, 'chrome-headless-shell-mac-x64', 'chrome-headless-shell'),
          path.join(base, 'chrome-headless-shell-linux64', 'chrome-headless-shell'),
          path.join(base, 'chrome-headless-shell-win64', 'chrome-headless-shell.exe')
        );
}
}
}
  return candidates.find(file => fs.existsSync(file));
}

function isLocalRequest(url) {
  if (!/^https?:/i.test(url)) return true;
  try {
    const origin = new URL(url).origin;
    return origin === new URL(routerBase).origin || origin === new URL(mockBase).origin;
} catch {
    return false;
}
}

async function startBrowser() {
  const executablePath = chromiumExecutable();
  browser = await chromium.launch(executablePath ? { headless: true, executablePath } : { headless: true });
  page = await browser.newPage({ viewport: { width: 1440, height: 960 } });
  page.on('pageerror', error => {
    pageErrors.push('pageerror: ' + String(error && error.stack ? error.stack : error));
});
  page.on('console', message => {
    if (message.type() !== 'error') return;
    const text = 'console: ' + message.text();
    const location = message.location();
    const url = (location && location.url) || '';
    const index = expectedHttpStatusErrors.findIndex(entry =>
      entry.url.test(url) && entry.status.test(message.text()));
    if (index >= 0) {
      expectedHttpStatusErrors.splice(index, 1);
      return;
}
    pageErrors.push(text);
});
  page.on('request', request => {
    const url = request.url();
    if (!isLocalRequest(url)) externalRequests.push(url);
});
}

/* ── 基座自检用例（不属于任何页面模块；页面用例见 console_qa/*.mjs） ── */

test('router service is ready on an isolated database', async () => {
  const response = await fetch(routerBase + '/healthz');
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true });
  assert.equal(fs.existsSync(path.join(tempDir, 'router.db')), true);
  assertRepoDbUnchanged();
});

test('graph.js is served as a native ES module with a single module entry', async () => {
  const response = await fetch(routerBase + '/console/routing/graph.js');
  assert.equal(response.status, 200);
  const text = await response.text();
  assert.match(text, /export \{/, 'graph.js is not an ES module');
  assert.doesNotMatch(text, /module\.exports/, 'graph.js still carries the CommonJS export');
  assert.doesNotMatch(text, /root\.RTGraph/, 'graph.js still writes a window/global bridge');
  const pageResponse = await fetch(routerBase + '/console/routing.html');
  assert.equal(pageResponse.status, 200);
  const html = await pageResponse.text();
  const tags = html.match(/<script\b[^>]*>/g) || [];
  assert.equal(tags.length, 1, 'routing.html must load exactly one module entry');
  assert.match(tags[0], /type="module"/);
  assert.match(tags[0], /routing\/app\.js/);
  assert.equal((await fetch(routerBase + '/console/routing/example.json')).status, 200);
});

test('console pages each load through one native module entry', async () => {
  const pages = ['index.html', 'jev.html', 'logs.html', 'models.html', 'users.html', 'login.html'];
  const markers = {
    'index.html': '#cards .statcard',
    'jev.html': '#jevStatus',
    'logs.html': '#countPill',
    'models.html': '#modelTable tr',
    'users.html': '#usersTable tr, #denyCard:not([hidden])',
  };
  for (const name of pages) {
    const html = await (await fetch(routerBase + '/console/' + name)).text();
    const tags = html.match(/<script\b[^>]*>/g) || [];
    assert.equal(tags.length, 1, name + ' must load exactly one script tag');
    assert.match(tags[0], /type="module"/, name + ' entry is not a native module');
    const before = pageErrors.length;
    await page.goto(routerBase + '/console/' + name);
    if (name === 'login.html') {
      /* 已登录（QA 关闭了用户鉴权）的访客直接回首页：模块必须真的跑起来。 */
      await waitFor(() => location.pathname.endsWith('/index.html'));
    } else {
      await page.waitForSelector(markers[name], { timeout: 15000 });
    }
    assert.equal(pageErrors.length, before, name + ' raised a page error: ' +
      pageErrors.slice(before).join(' | '));
  }
  /* 语言开关不再依赖 inline onclick：模块绑定的按钮必须真的切换文案。 */
  await page.goto(routerBase + '/console/index.html');
  await page.waitForSelector('#cards .statcard');
  await page.locator('.langtog button[data-lang="en"]').click();
  await waitFor(() => document.documentElement.lang === 'en');
  const english = await page.evaluate(() => document.querySelector('.top nav a').textContent.trim());
  assert.equal(english, 'Overview', 'language toggle did not repaint the nav');
  await page.locator('.langtog button[data-lang="zh"]').click();
  await waitFor(() => document.documentElement.lang === 'zh-CN');
});

test('login language toggle is module-bound and really switches', async () => {
  const html = await (await fetch(routerBase + '/console/login.html')).text();
  /* setLang 是模块导出，不是页面全局：login.html 不能再出现 inline 调用。 */
  assert.doesNotMatch(html, /onclick\s*=\s*["'][^"']*\bsetLang\b/i,
    'login.html still calls setLang from an inline handler');

  /* QA 会话已登录（ROUTER_USER_AUTH=0），login.js 会直接回首页；
     只把 /admin/me 变成 401，才能留在登录页验证按钮绑定。 */
  const meMatcher = '**/admin/me';
  expectHttpStatusError({ url: /\/admin\/me$/, status: /401/ });
  await page.route(meMatcher, route => route.fulfill({
    status: 401,
    contentType: 'application/json',
    body: JSON.stringify({ detail: 'unauthorized' })
  }));
  const before = pageErrors.length;
  try {
    await page.goto(routerBase + '/console/login.html');
    await page.waitForSelector('#loginForm:not([hidden])', { timeout: 15000 });
    await page.locator('.langtog button[data-lang="en"]').click();
    await page.waitForFunction(() => document.documentElement.lang === 'en');
    const english = await page.evaluate(async () => {
      const { t } = await import('/console/i18n.js');
      return {
        title: document.title,
        expectedTitle: t('login.title') + ' · RoutePilot',
        submit: document.getElementById('loginSubmit').textContent,
        expectedSubmit: t('login.submit'),
        active: document.querySelector('.langtog button.on').dataset.lang
      };
    });
    assert.equal(english.title, english.expectedTitle, 'the login title did not repaint in English');
    assert.equal(english.submit, english.expectedSubmit, 'the login form did not repaint in English');
    assert.equal(english.active, 'en', 'the login language toggle did not mark the active language');

    await page.locator('.langtog button[data-lang="zh"]').click();
    await page.waitForFunction(() => document.documentElement.lang === 'zh-CN');
  } finally {
    await page.unroute(meMatcher);
    resetExpectedHttpStatusErrors();
  }
  assert.equal(pageErrors.length, before, 'login language toggle raised a page error: ' +
    pageErrors.slice(before).join(' | '));
});

test('mock /v1/systemone answers use the frozen M0 wire shapes', async () => {
  const state = '【待判断的用户请求】\n' + S1_TEXT + '\n\n【任务】请选择最合适处理这条请求的模型。';
  const response = await fetch(mockBase + '/v1/systemone', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      model: JEV_MODEL,
      state,
      questions: {
        capability: {
          type: 'choice',
          instructions: '这条请求需要哪个能力档位？',
          criteria: { 轻量模型: '简单问答', 标准模型: '标准写作', 增强模型: '复杂分析' }
},
        domain: { type: 'noul', instructions: '是否需要电力行业知识？' },
        complexity: { type: 'score', instructions: '复杂度评分。', criteria: [1, 5, 7.5, 10] }
}
})
});
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.deepEqual(Object.keys(body.answers).sort(), ['capability', 'complexity', 'domain']);

  const choice = body.answers.capability;
  assert.equal(choice.type, 'choice');
  assert.equal(typeof choice.choice, 'string');
  assert.deepEqual(Object.keys(choice.probabilities).sort(), ['增强模型', '标准模型', '轻量模型'].sort());
  const total = Object.values(choice.probabilities).reduce((sum, value) => sum + value, 0);
  assert.ok(Math.abs(total - 1) < 1e-9);
  assert.ok(Math.abs(choice.probabilities['增强模型'] - 0.84) < 1e-9);

  const noul = body.answers.domain;
  assert.equal(noul.type, 'noul');
  assert.equal(typeof noul.noul, 'number');
  assert.ok(noul.noul >= 0 && noul.noul <= 1);
  assert.ok(Math.abs(noul.noul - 0.92) < 1e-9);
  assert.equal('yes' in noul, false);

  const score = body.answers.complexity;
  assert.equal(score.type, 'score');
  assert.equal(Number.isInteger(score.score), true);
  assert.equal(typeof score.legend[String(score.score)], 'number');
  assert.equal(score.legend[String(score.score)], 5);
  assert.ok(typeof score.probabilities[String(score.score)] === 'number');
});

test('mock fails the s5 first wave with 504 and answers later waves', async () => {
  const s5 = FIXTURE.scenarios.find(scenario => scenario.id === 's5');
  assert.ok(s5, 's5 scenario fixture missing');
  const state = '【待判断的用户请求】\n' + s5.text + '\n\n【任务】请选择最合适处理这条请求的模型。';
  const first = await fetch(mockBase + '/v1/systemone', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      model: JEV_MODEL,
      state,
      questions: {
        capability: {
          type: 'choice',
          instructions: '这条请求需要哪个能力档位？',
          criteria: { 轻量模型: '简单问答', 标准模型: '标准写作', 增强模型: '复杂分析' }
},
        domain: { type: 'noul', instructions: '是否需要电力行业知识？' }
}
})
});
  assert.equal(first.status, 504);
  const firstBody = await first.json();
  assert.equal(firstBody.scenario, 's5');

  const later = await fetch(mockBase + '/v1/systemone', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      model: JEV_MODEL,
      state,
      questions: { clarify: { type: 'noul', instructions: '是否需要进一步处理？' } }
})
});
  assert.equal(later.status, 200);
  const laterBody = await later.json();
  assert.equal(typeof laterBody.answers.clarify.noul, 'number');
});

test('mock upstream returns an OpenAI-format completion', async () => {
  const response = await fetch(mockBase + '/v1/chat/completions', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ model: 'qwen3.8-flash-sft', messages: [{ role: 'user', content: 'hi' }] })
});
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.object, 'chat.completion');
  assert.equal(body.model, 'qwen3.8-flash-sft');
  assert.equal(body.choices[0].message.role, 'assistant');
  assert.equal(typeof body.choices[0].message.content, 'string');
  assert.equal(body.usage.total_tokens > 0, true);
});


test('admin try routes the sample graph through the live decision mock', async () => {
  const result = await api('POST', '/admin/try', { tree_id: TREE_ID, text: S1_TEXT, now: S1_NOW });
  assert.equal(result.outcome.type, 'hit');
  assert.equal(result.outcome.target_id, 'power-target');
  assert.equal(result.outcome.model, 'qwen3.8-flash-sft');
  assert.equal(result.outcome.waves.length, 1);
  assert.deepEqual(result.outcome.waves[0].questions.sort(), ['capability', 'domain']);
  assert.deepEqual(result.outcome.candidates.map(candidate => candidate.target_id).sort(), ['power-target', 'strong-target']);
  assert.ok(Math.abs(result.answers.capability.probabilities.strong - 0.84) < 1e-9);
  assert.ok(Math.abs(result.answers.domain.yes - 0.92) < 1e-9);
  assert.deepEqual(result.warnings, []);

  const calls = mockState.systemoneCalls.filter(call => call.scenario === 's1');
  assert.equal(calls.length >= 1, true);
  assert.deepEqual(calls[calls.length - 1].keys.sort(), ['capability', 'domain']);
});

/* ── Task 10 · 画布骨架 / 视口 / 连线 / 快速创建 / 历史栈 ───────────── */

export const MOD = process.platform === 'darwin' ? 'Meta' : 'Control';

export async function openCanvas() {
  await page.goto(routerBase + '/console/routing.html');
  await page.waitForSelector('#cvStage [data-node-id]', { timeout: 20000 });
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app && app.booted && app.kit);
});
  await page.waitForTimeout(180);
}

export async function appSnapshot() {
  return page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (({
    viewport: app.kit.getViewport(),
    kind: app.kind(),
    nodes: app.graph.nodes.map(node => ({
      id: node.id, type: node.type, question_type: node.question_type || null,
      operator: node.operator || null, source: node.source || null,
      position: { x: node.position.x, y: node.position.y }
})),
    edges: app.graph.edges.map(edge => ({
      id: edge.id, source: edge.source, source_port: edge.source_port,
      target: edge.target, target_port: edge.target_port,
      condition: edge.condition ? { op: edge.condition.op, value: edge.condition.value } : edge.condition
})),
    selection: app.kit.getSelection(),
    history: { undo: store.canUndo, redo: store.canRedo }
}));
});
}

export async function blankPoint() {
  return page.evaluate(() => {
    const stage = document.getElementById('cvStage');
    const rect = stage.getBoundingClientRect();
    for (let y = rect.top + 14; y < rect.bottom - 130; y += 22) {
      for (let x = rect.left + 14; x < rect.right - 14; x += 22) {
        const el = document.elementFromPoint(x, y);
        if (!el) continue;
        if (el.closest('[data-node-id]') || el.closest('.rtc-overlay') || el.closest('.rtc-edge-hit')) continue;
        if (el !== stage && !stage.contains(el)) continue;
        return { x: x, y: y };
}
}
    return null;
});
}

export async function clearToasts() {
  await page.evaluate(() => { document.getElementById('cvToast').innerHTML = ''; });
}

export async function toastText() {
  return page.evaluate(() => document.getElementById('cvToast').textContent.replace(/\s+/g, ' ').trim());
}

export async function portPoint(nodeId, portId, dir) {
  const locator = page.locator(`[data-node-id="${nodeId}"] [data-port-id="${portId}"][data-port-dir="${dir}"]`).first();
  const box = await locator.boundingBox();
  assert.ok(box, `port not visible: ${nodeId}.${portId} (${dir})`);
  return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
}

export async function dragPort(sourceNode, sourcePort, target, options) {
  const from = await portPoint(sourceNode, sourcePort, 'out');
  const opts = options || {};
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  /* 空槽位只在拖线期间显示：目标坐标必须在 mousedown 之后再取。 */
  await page.mouse.move(from.x + 26, from.y + 18, { steps: 4 });
  const point = target && typeof target.node === 'string' && typeof target.port === 'string'
    ? await portPoint(target.node, target.port, 'in')
    : target;
  await page.mouse.move(point.x, point.y, { steps: 10 });
  if (opts.beforeUp) await opts.beforeUp();
  await page.mouse.up();
}

export async function clickNode(nodeId) {
  const box = await page.locator(`[data-node-id="${nodeId}"]`).first().boundingBox();
  assert.ok(box, `node not visible: ${nodeId}`);
  await page.mouse.move(box.x + 24, box.y + 12);
  await page.mouse.down();
  await page.mouse.up();
}

export async function clickEdge(edgeId) {
  const point = await page.evaluate(id => {
    const hit = Array.from(document.querySelectorAll('.rtc-edge-hit'))
      .find(path => path.dataset.edgeId === id);
    if (!hit) return null;
    const length = hit.getTotalLength();
    const matrix = hit.getScreenCTM();
    for (const ratio of [0.5, 0.4, 0.6, 0.3, 0.7, 0.2, 0.8, 0.1, 0.9]) {
      const local = hit.getPointAtLength(length * ratio);
      const screen = new DOMPoint(local.x, local.y).matrixTransform(matrix);
      if (document.elementFromPoint(screen.x, screen.y) === hit) return { x: screen.x, y: screen.y };
}
    return null;
}, edgeId);
  if (point) await page.mouse.click(point.x, point.y);
  else await page.evaluate(async id => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.kit.select('edge', id));
}, edgeId);
}

/* 负向断言：给异步副作用一个有限的窗口；窗口内出现坏状态即失败。 */
export async function expectNever(predicate, message, timeout) {
  let seen = false;
  try {
    await waitFor(predicate, null, { timeout: timeout || 400 });
    seen = true;
} catch (error) {
    if (error.name !== 'TimeoutError') throw error;
}
  assert.equal(seen, false, message);
}

/* page.waitForFunction 的 predicate 不支持 async（Promise 会被当成真值），
   而读取 ESM 状态必须 await import()；因此轮询放在 Node 侧，每次用
   page.evaluate 执行同一个 async predicate。超时错误保持 name='TimeoutError'，
   expectNever() 的负向断言语义不变。 */
export async function waitFor(predicate, arg, options) {
  const timeout = (options && options.timeout) || 30000;
  const interval = (options && typeof options.polling === 'number') ? options.polling : 100;
  const deadline = Date.now() + timeout;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      const value = await page.evaluate(predicate, arg);
      if (value) return value;
} catch (error) {
      lastError = error;
}
    await new Promise(resolve => setTimeout(resolve, interval));
}
  const error = new Error('waitFor timed out after ' + timeout + 'ms' +
    (lastError ? ': ' + lastError.message : ''));
  error.name = 'TimeoutError';
  throw error;
}

export async function dragLibraryItem(kind, target) {
  const item = page.locator(`[data-lib-kind="${kind}"]`);
  const box = await item.boundingBox();
  assert.ok(box, `node library item not visible: ${kind}`);
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(target.x, target.y, { steps: 12 });
  await page.mouse.up();
}


/* ── Task 11 · 检查器 / 条件双入口 / 级联删除 ─────────────────────── */

export async function waitCondPop(edgeId) {
  await page.waitForFunction(id => {
    const pop = document.getElementById('cvCondPop');
    return pop && !pop.hidden && pop.dataset.edgeId === id;
}, edgeId, { timeout: 4000 });
}

export async function waitInspectorBranch(branch, id) {
  await page.waitForFunction(([kind, value]) => {
    const root = document.querySelector('#cvInspectorBody [data-ins-branch]');
    return root && root.dataset.insBranch === kind && (!value || root.dataset.insId === value);
}, [branch, id], { timeout: 4000 });
}

export async function waitEdgeCondition(edgeId, check) {
  await waitFor(async ([id, expected]) => {
    const { app, store } = await import('/console/routing/app.js');

    const edge = app.graph.edges.find(item => item.id === id);
    if (!edge) return false;
    if (expected === null) return edge.condition === null;
    return edge.condition && Object.keys(expected).every(key => edge.condition[key] === expected[key]);

}, [edgeId, check], { timeout: 4000 });
}

export async function edgeOf(edgeId) {
  return page.evaluate(async id => {
    const { app, store } = await import('/console/routing/app.js');

    const edge = app.graph.edges.find(item => item.id === id);
    return edge ? JSON.parse(JSON.stringify(edge)) : null;

}, edgeId);
}

export async function danglingEdgeIds() {
  return page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
const RTGraph = await import('/console/routing/graph.js');

    const graph = app.graph;
    return graph.edges.filter(edge => {
      const source = RTGraph.findNode(graph, edge.source);
      const target = RTGraph.findNode(graph, edge.target);
      return !source || !target ||
        !RTGraph.portOf(source, edge.source_port, graph) ||
        !RTGraph.portOf(target, edge.target_port, graph);
}).map(edge => edge.id);

});
}

export async function issueCodes() {
  return page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.issues.filter(issue => issue.level !== 'hint').map(issue => issue.code));
});
}

export async function openIssuesPanel() {
  await page.locator('#cvIssuesBtn').click();
  await page.waitForFunction(() => !document.getElementById('cvIssues').hidden);
}


/* ── Task 12 · 保存 / 启用 / dirty / 导入导出 / 校验定位 ────────────── */

/* 每个 Task 12 用例都从同一份服务端基线开始：power-priority 恢复为示例图并启用。
   管理面没有 DELETE /admin/trees/{id}，用例新建的组留在临时库里但保持未启用。 */
export async function putTree(id, payload) {
  const current = await serverTree(id);
  return api('PUT', '/admin/trees/' + encodeURIComponent(id), {
    ...payload,
    base_document_sha256: current.document_sha256,
});
}

export async function ensureBaselineTree() {
  const restored = await putTree(TREE_ID, {
    id: TREE_ID,
    name: BASELINE.name,
    timezone: BASELINE.timezone,
    sticky: BASELINE.sticky,
    root: EXAMPLE
});
  if (!restored.tree.enabled) await api('POST', '/admin/trees/' + TREE_ID + '/enable');
  return (await api('GET', '/admin/trees/' + TREE_ID)).tree;
}

export async function serverTree(id) {
  return (await api('GET', '/admin/trees/' + encodeURIComponent(id))).tree;
}

export async function dirtyBadge() {
  return page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');

    const badge = document.getElementById('cvDirty');
    return {
      hidden: badge.hidden,
      kind: badge.dataset.kind,
      text: badge.textContent.trim(),
      appKind: app.kind(),
      cleanHidden: document.getElementById('cvClean').hidden
};

});
}

export async function dragNodeBy(nodeId, dx, dy) {
  const before = await page.evaluate(async id => {
    const { app, store } = await import('/console/routing/app.js');

    const node = app.graph.nodes.find(item => item.id === id);
    return { x: node.position.x, y: node.position.y };

}, nodeId);
  const box = await page.locator(`[data-node-id="${nodeId}"]`).first().boundingBox();
  assert.ok(box, `node not visible: ${nodeId}`);
  const start = { x: box.x + box.width / 2, y: box.y + 11 };
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await page.mouse.move(start.x + dx, start.y + dy, { steps: 8 });
  await page.mouse.up();
  await waitFor(async ([id, previous]) => {
    const { app, store } = await import('/console/routing/app.js');

    const node = app.graph.nodes.find(item => item.id === id);
    return node && (node.position.x !== previous.x || node.position.y !== previous.y);

}, [nodeId, before], { timeout: 4000 });
  return page.evaluate(async id => {
    const { app, store } = await import('/console/routing/app.js');

    const node = app.graph.nodes.find(item => item.id === id);
    return { x: node.position.x, y: node.position.y };

}, nodeId);
}

export async function saveNow() {
  await page.locator('#cvSave').click();
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.changeKind === 'clean' && !!app.saved);
},
    null, { timeout: 8000 });
}

export async function beforeUnloadBlocked() {
  return page.evaluate(() => {
    const event = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(event);
    return event.defaultPrevented;
});
}

export async function exportViaUi() {
  const downloadPromise = page.waitForEvent('download', { timeout: 10000 });
  await page.locator('#cvExport').click();
  const download = await downloadPromise;
  const file = await download.path();
  assert.ok(file, 'browser download has no local path');
  return { name: download.suggestedFilename(), text: fs.readFileSync(file, 'utf8') };
}

export async function setImportPayload(payload, name) {
  await page.locator('#cvImportFile').setInputFiles({
    name: name || 'graph.sr-routing-graph-v3.json',
    mimeType: 'application/json',
    buffer: Buffer.from(typeof payload === 'string' ? payload : JSON.stringify(payload))
});
}

export async function waitConfirmDialog() {
  await page.waitForSelector('#cvConfirmModal:not([hidden])', { timeout: 4000 });
  return page.locator('#cvConfirmBody').innerText();
}

export async function waitToastIncludes(fragment, timeout) {
  await page.waitForFunction(value => {
    const host = document.getElementById('cvToast');
    return !!host && host.textContent.indexOf(value) >= 0;
}, fragment, { timeout: timeout || 6000 });
}


/* ── Task 13 · 真实试算 / trace-only 回放 / 轨迹抽屉 / 波次布局 ─────── */

/* 样例输入是「文本 + 固定 now」的请求预设：断言用 fixture 的文本/时间，
   但答案永远从实时 /admin/try 读，QA 不把 fixture answers 传给页面。 */
export const TRY_SAMPLES = FIXTURE.scenarios.map(scenario => ({
  id: scenario.id,
  name: scenario.name,
  text: scenario.text,
  now: scenario.now
}));

export async function prepareTryCanvas() {
  await ensureBaselineTree();
  await openCanvas();
  const treeId = await page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.treeId);
});
  if (treeId !== TREE_ID) {
    await page.evaluate(async id => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.selectTree(id));
}, TREE_ID);
    await waitFor(async id => {
    const { app, store } = await import('/console/routing/app.js');
    return (app.treeId === id && app.booted);
},
      TREE_ID, { timeout: 10000 });
}
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app && !app.tryRunning);
});
}

export async function waitTryResult(expected) {
  await waitFor(async want => {
    const { app, store } = await import('/console/routing/app.js');

    const replay = app.replay;
    if (!replay || app.tryRunning) return false;
    if (!want) return true;
    if (want.text != null && replay.text !== want.text) return false;
    if (want.now !== undefined && (replay.now || null) !== want.now) return false;
    return true;

}, expected || null, { timeout: 15000 });
}

export async function runTrySample(index) {
  const sample = TRY_SAMPLES[index];
  assert.ok(sample, `missing scenario fixture at index ${index}`);
  await page.locator(`[data-try-sample="${index}"]`).click();
  await waitTryResult({ text: sample.text, now: sample.now });
  return sample;
}

export async function runTryManual(text) {
  await page.locator('#cvTryInput').fill(text);
  await page.locator('#cvTryRun').click();
  await waitTryResult({ text: text });
}

/* 一次性读取回放数据 + 卡片/边/结论/抽屉的 DOM 状态；所有断言都基于
   页面公开状态（App.replay / data-* / class），不猜内部实现。 */
export async function trySnapshot() {
  return page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');

    const replay = app.replay;
    if (!replay) return null;
    const nodeEl = id => document.querySelector(`#cvNodes [data-node-id="${id}"]`);
    const textOf = (el, sel) => {
      if (!el) return '';
      const target = el.querySelector(sel);
      return target ? target.textContent.replace(/\s+/g, ' ').trim() : '';
};
    const nodeInfo = id => {
      const el = nodeEl(id);
      if (!el) return null;
      const live = {};
      Array.prototype.forEach.call(el.querySelectorAll('[data-live]'), item => {
        live[item.dataset.live] = item.textContent.trim();
});
      return {
        replay: el.dataset.replay || '',
        replayKind: el.dataset.replayKind || '',
        classes: el.className,
        state: textOf(el, '.rtc-card-h [data-live="state"]'),
        foot: textOf(el, '.rtc-tfoot [data-live="state"]'),
        gate: textOf(el, '.rtc-gate'),
        selectedOptions: Array.prototype.map.call(el.querySelectorAll('.rtc-opt.is-sel'),
          row => row.dataset.optkey),
        keywordClasses: Array.prototype.map.call(el.querySelectorAll('.rtc-kw'),
          chip => chip.className),
        live: live
};
};
    const slotInfo = (nodeId, port) => {
      const row = document.querySelector(
        `#cvNodes [data-node-id="${nodeId}"] .rtc-slot[data-slot="${port}"]`);
      if (!row) return null;
      const live = row.querySelector('.rtc-live');
      return {
        classes: row.className,
        status: row.dataset.status || '',
        edge: row.dataset.edge || '',
        live: live ? live.textContent.trim() : ''
};
};
    const edges = {};
    Array.prototype.forEach.call(document.querySelectorAll('#cvEdges path.rtc-edge'), path => {
      edges[path.dataset.edgeId] = path.className.baseVal;
});
    return {
      id: replay.id,
      text: replay.text,
      now: replay.now,
      final: replay.final ? {
        target_id: replay.final.target_id,
        kind: replay.final.kind,
        selection_reason: replay.final.selection_reason,
        model: replay.final.model,
        priority: replay.final.priority,
        candidates: replay.final.candidates
} : null,
      waves: replay.waves.map(wave => ({
        wave: wave.wave,
        questions: wave.questions,
        duration_ms: wave.duration_ms,
        failed: wave.failed,
        error: wave.error
})),
      nodeStates: replay.nodeStates,
      edgeStates: replay.edgeStates,
      candidates: replay.candidates,
      nodes: {
        start: nodeInfo('start'),
        capability: nodeInfo('capability'),
        domain: nodeInfo('domain'),
        clarify: nodeInfo('clarify'),
        'and-power': nodeInfo('and-power'),
        'or-ctx': nodeInfo('or-ctx'),
        'not-cap': nodeInfo('not-cap'),
        'and-follow': nodeInfo('and-follow'),
        'cond-text': nodeInfo('cond-text'),
        'cond-peak': nodeInfo('cond-peak'),
        'power-target': nodeInfo('power-target'),
        'strong-target': nodeInfo('strong-target'),
        'deep-target': nodeInfo('deep-target'),
        'standard-target': nodeInfo('standard-target'),
        'light-target': nodeInfo('light-target')
},
      slots: {
        andPowerIn1: slotInfo('and-power', 'in_1'),
        andPowerIn2: slotInfo('and-power', 'in_2'),
        deepIn1: slotInfo('deep-target', 'in_1'),
        clarifyIn1: slotInfo('clarify', 'in_1'),
        powerIn1: slotInfo('power-target', 'in_1'),
        lightIn1: slotInfo('light-target', 'in_1')
},
      edges: edges,
      svgReplay: document.getElementById('cvEdges').classList.contains('is-replay'),
      resultHidden: document.getElementById('cvTryResult').hidden,
      resultText: document.getElementById('cvTryResult').textContent.replace(/\s+/g, ' ').trim(),
      resultFinal: document.getElementById('cvTryResult').dataset.final || '',
      resultKind: document.getElementById('cvTryResult').dataset.kind || '',
      hudHidden: document.getElementById('cvReplayHud').hidden,
      hudText: document.getElementById('cvReplayHud').textContent.replace(/\s+/g, ' ').trim(),
      hudTitles: Array.prototype.map.call(
        document.querySelectorAll('#cvReplayHud [data-hud-wave]'),
        el => el.getAttribute('title') || ''),
      drawerHidden: document.getElementById('cvTraceDrawer').hidden,
      drawerWaveCount: document.querySelectorAll('#cvTraceDrawer .rtc-dw-wave').length,
      drawerWaveTexts: Array.prototype.map.call(
        document.querySelectorAll('#cvTraceDrawer .rtc-dw-wave'),
        el => el.textContent.replace(/\s+/g, ' ').trim()),
      drawerText: document.getElementById('cvTraceDrawer').textContent.replace(/\s+/g, ' ').trim(),
      running: app.tryRunning,
      runDisabled: document.getElementById('cvTryRun').disabled,
      topRunDisabled: document.getElementById('cvTryBtn').disabled,
      palCollapsed: document.getElementById('cvPalette').classList.contains('is-collapsed'),
      inspCollapsed: document.getElementById('cvInspector').classList.contains('is-collapsed')
};

});
}

export function assertIncludes(haystack, needle, message) {
  assert.ok(String(haystack).indexOf(needle) >= 0,
    `${message || 'missing text'} (wanted ${JSON.stringify(needle)} in ${JSON.stringify(String(haystack).slice(0, 400))})`);
}


/* ── Task 14 · 最终验收矩阵（§7.4 补充项 / 极限图 / 响应式 / 中文） ── */


/* ── Task 14 · final acceptance hardening ───────────────────────────── */


/* loadExample/applyImport 用 change() 换图，不走 loadTree：过去没有 bump loadSeq，
   在途的 selectTree 回包落地后会把示例/导入的新草稿整份盖回去。 */


/* ── 计算日志：分阶段计算视图（Task 5） ─────────────────────────────── */


/* ── 启动 / 清理 ────────────────────────────────────────────────────── */

let teardownPromise = null;

async function teardown() {
  if (!teardownPromise) {
    teardownPromise = (async () => {
      if (browser) {
        try { await browser.close(); } catch { /* ignore */ }
        browser = null;
}
      await stopRouter();
      if (mockServer) {
        await new Promise(resolve => mockServer.close(resolve));
        mockServer = null;
}
      if (tempDir) {
        await fsp.rm(tempDir, { recursive: true, force: true });
        tempDir = '';
}
      /* runChecks 已断言一次；teardown 再验一次，覆盖中途抛错的路径。 */
      assertRepoDbUnchanged();
})();
}
  return teardownPromise;
}

function installSignalHandlers() {
  let shuttingDown = false;
  const handle = signal => {
    if (shuttingDown) return;
    shuttingDown = true;
    console.error(`\n[console-qa] ${signal} received, tearing down ...`);
    teardown()
      .catch(error => console.error(error && error.stack ? error.stack : error))
      .finally(() => process.exit(1));
};
  process.once('SIGINT', () => handle('SIGINT'));
  process.once('SIGTERM', () => handle('SIGTERM'));
}

let started = false;

/* 页面模块可单独执行：node scripts/console_qa/<page>.mjs 会走到这里；
   全量入口 main() 也复用同一函数，保证只有一套启动/清理路径。 */
export async function runRegisteredCases() {
  if (started) return;
  started = true;
  installSignalHandlers();
  try {
    repoDbFingerprint = repoDbSignature();
    mockServer = await startMock();
    tempDir = await fsp.mkdtemp(path.join(os.tmpdir(), 'rt-console-qa-'));
    const port = await freePort();
    routerBase = `http://127.0.0.1:${port}`;
    await startRouter(port);
    await bootstrap();
    await startBrowser();
    await runChecks();
} finally {
    await teardown();
}
}

export async function runIfMain(metaUrl) {
  const invoked = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(metaUrl);
  if (!invoked) return;
  try {
    await runRegisteredCases();
} catch (error) {
    console.error(error && error.stack ? error.stack : error);
    process.exitCode = 1;
}
}

async function main() {
  /* 静态 import 会先于本文件执行（页面模块又反向 import 本文件的注册器），
     因此页面模块在 main 里动态加载，确保基座初始化完成后再注册用例。 */
  await import('./console_qa/canvas.mjs');
  await import('./console_qa/inspector.mjs');
  await import('./console_qa/tryrun.mjs');
  await import('./console_qa/logs.mjs');
  await import('./console_qa/models.mjs');
  await runRegisteredCases();
}

const invokedDirectly = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedDirectly) {
  main().catch(error => {
    console.error(error && error.stack ? error.stack : error);
    process.exitCode = 1;
});
}
