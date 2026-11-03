/* 控制台 QA 基座（Task 9；后续 Task 10–14 在此注册画布/检查器/试算用例）
   ------------------------------------------------------------------
   启动两个服务：
   1. Node HTTP mock（127.0.0.1 随机端口）：决策服务 /v1/systemone（M0 冻结 wire 契约）
      + 模型上游 /v1/chat/completions；
   2. `.venv/bin/python -m router` 子进程：临时目录 SQLite、随机端口、ROUTER_USER_AUTH=0、
      ROUTER_JEV_URL/MODEL 指向 mock。
   然后经真实管理面 API 完成 bootstrap（provider → 模型 → 时段 → 决策服务 → 建图 → 启用），
   最后注册/执行用例；teardown 关闭 browser、router、mock 并删除临时目录。

   Usage: node scripts/console_qa.mjs
   Exports: test(name, fn) / runChecks() / api(method, path, body) / page / mockBase
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

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const EXAMPLE_PATH = path.join(ROOT, 'router/console/routing/example.json');
const SCENARIO_PATH = path.join(ROOT, 'tests/fixtures/graph_scenarios.json');
const SHOT_DIR = path.join(ROOT, 'docs/od-design/routing-canvas-2026-11-02/implementation-qa');
const TREE_ID = 'power-priority';
const JEV_MODEL = 'd1-2B';
const S1_TEXT = '10kV 线路跳闸，请安排抢修工单并做故障定位。';
const S1_NOW = '2026-11-02T12:00:00+08:00';
const API_TIMEOUT_MS = 15000;
/* Screenshot writes are opt-in: a plain `npm run check:console` must not dirty
   the committed evidence PNGs. Regenerate with QA_SCREENSHOTS=1. */
const WRITE_SHOTS = process.env.QA_SCREENSHOTS === '1';

/* 示例图是 15 节点图的唯一来源：节点/边数、start 坐标、兜底目标都从 fixture 派生，
   避免 QA 里散落 15 / 20,150 / standard-target 这类字面量。 */
const EXAMPLE = JSON.parse(fs.readFileSync(EXAMPLE_PATH, 'utf8'));
const EXAMPLE_NODES = EXAMPLE.nodes.length;
const EXAMPLE_EDGES = EXAMPLE.edges.length;
const EXAMPLE_START = EXAMPLE.nodes.find(node => node.id === 'start').position;
const EXAMPLE_TARGETS = { otherwise: EXAMPLE.otherwise_target, error: EXAMPLE.error_fallback };
/* 示例图只有 root；envelope 元数据是 QA 基座约定，集中一处供 bootstrap / 基线 / 断言复用。 */
const BASELINE = {
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
let tempDir = '';
let routerLog = '';
let repoDbFingerprint = null;

const pageErrors = [];
const externalRequests = [];
/* 故意制造的 4xx 会让 Chromium 打印 "Failed to load resource" console error。
   用例必须同时声明精确的请求 URL 与状态码；监听器只吞掉完全匹配的那一条，
   其他 4xx（哪怕同一次点击里的）仍然记为 console error。 */
const expectedHttpStatusErrors = [];
function expectHttpStatusError(matcher) {
  expectedHttpStatusErrors.push(matcher);
}
function resetExpectedHttpStatusErrors() {
  expectedHttpStatusErrors.length = 0;
}
const mockState = { systemoneCalls: [], chatCalls: [], scenarioCalls: {} };

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

function assertRepoDbUnchanged() {
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

const FIXTURE = loadScenarioFixture();

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

/* ── 用例（Task 9 冒烟；Task 10–14 在此文件追加画布/编辑/试算用例） ── */

test('router service is ready on an isolated database', async () => {
  const response = await fetch(routerBase + '/healthz');
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true });
  assert.equal(fs.existsSync(path.join(tempDir, 'router.db')), true);
  assertRepoDbUnchanged();
});

test('graph.js is served as a browser script', async () => {
  const response = await fetch(routerBase + '/console/routing/graph.js');
  assert.equal(response.status, 200);
  const text = await response.text();
  assert.match(text, /RTGraph/);
  assert.match(text, /module\.exports/);
  assert.equal((await fetch(routerBase + '/console/routing/example.json')).status, 200);
  assert.equal((await fetch(routerBase + '/console/routing.html')).status, 200);
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

test('bootstrap registers catalog, periods, decision service and the enabled power graph', async () => {
  const models = await api('GET', '/admin/models');
  const modelIds = models.models.map(model => model.id).sort();
  for (const required of ['deepseek-v3', 'qwen3-8b', 'qwen3-32b', 'qwen3.8-flash-sft']) {
    assert.ok(modelIds.includes(required), 'missing model ' + required);
  }
  assert.ok(models.models.every(model => model.provider === 'local'));

  const periods = await api('GET', '/admin/time-periods');
  assert.deepEqual(periods.periods.map(period => period.name), ['peak', 'normal', 'offpeak']);
  assert.deepEqual(periods.periods.map(period => [period.start, period.end]), [
    ['08:00', '11:00'], ['11:00', '18:00'], ['18:00', '08:00']
  ]);

  const jev = await api('GET', '/admin/jev');
  assert.equal(jev.config.base_url, mockBase);
  assert.equal(jev.config.model, JEV_MODEL);

  const trees = await api('GET', '/admin/trees');
  const tree = trees.trees.find(item => item.id === TREE_ID);
  assert.ok(tree, 'power graph tree missing');
  assert.equal(tree.enabled, true);
  assert.equal(tree.root.nodes.length, 15);
  assert.equal(tree.root.edges.length, 15);
  assert.equal(tree.root.otherwise_target, 'standard-target');
  assert.equal(tree.root.error_fallback, 'light-target');
  assert.match(tree.tree_revision, /^[0-9a-f]{64}$/);
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

const MOD = process.platform === 'darwin' ? 'Meta' : 'Control';

async function openCanvas() {
  await page.goto(routerBase + '/console/routing.html');
  await page.waitForSelector('#cvStage [data-node-id]', { timeout: 20000 });
  await page.waitForFunction(() => window.App && window.App.booted && window.App.kit);
  await page.waitForTimeout(180);
}

async function appSnapshot() {
  return page.evaluate(() => ({
    viewport: window.App.kit.getViewport(),
    kind: window.App.kind(),
    nodes: window.App.graph.nodes.map(node => ({
      id: node.id, type: node.type, question_type: node.question_type || null,
      operator: node.operator || null, source: node.source || null,
      position: { x: node.position.x, y: node.position.y }
    })),
    edges: window.App.graph.edges.map(edge => ({
      id: edge.id, source: edge.source, source_port: edge.source_port,
      target: edge.target, target_port: edge.target_port,
      condition: edge.condition ? { op: edge.condition.op, value: edge.condition.value } : edge.condition
    })),
    selection: window.App.kit.getSelection(),
    history: { undo: window.App.kit.canUndo(), redo: window.App.kit.canRedo() }
  }));
}

async function blankPoint() {
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

async function clearToasts() {
  await page.evaluate(() => { document.getElementById('cvToast').innerHTML = ''; });
}

async function toastText() {
  return page.evaluate(() => document.getElementById('cvToast').textContent.replace(/\s+/g, ' ').trim());
}

async function portPoint(nodeId, portId, dir) {
  const locator = page.locator(`[data-node-id="${nodeId}"] [data-port-id="${portId}"][data-port-dir="${dir}"]`).first();
  const box = await locator.boundingBox();
  assert.ok(box, `port not visible: ${nodeId}.${portId} (${dir})`);
  return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
}

async function dragPort(sourceNode, sourcePort, target, options) {
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

async function clickNode(nodeId) {
  const box = await page.locator(`[data-node-id="${nodeId}"]`).first().boundingBox();
  assert.ok(box, `node not visible: ${nodeId}`);
  await page.mouse.move(box.x + 24, box.y + 12);
  await page.mouse.down();
  await page.mouse.up();
}

async function clickEdge(edgeId) {
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
  else await page.evaluate(id => window.App.kit.select('edge', id), edgeId);
}

/* 负向断言：给异步副作用一个有限的窗口；窗口内出现坏状态即失败。 */
async function expectNever(predicate, message, timeout) {
  let seen = false;
  try {
    await page.waitForFunction(predicate, null, { timeout: timeout || 400 });
    seen = true;
  } catch (error) {
    if (error.name !== 'TimeoutError') throw error;
  }
  assert.equal(seen, false, message);
}

async function dragLibraryItem(kind, target) {
  const item = page.locator(`[data-lib-kind="${kind}"]`);
  const box = await item.boundingBox();
  assert.ok(box, `node library item not visible: ${kind}`);
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(target.x, target.y, { steps: 12 });
  await page.mouse.up();
}

test('Task 10 · canvas skeleton renders the graph with no page overflow', async () => {
  await openCanvas();
  const metrics = await page.evaluate(() => ({
    nodes: document.querySelectorAll('#cvStage [data-node-id]').length,
    edges: document.querySelectorAll('#cvEdges path.rtc-edge').length,
    groups: document.querySelectorAll('#cvPalBody .rtc-pgroup').length,
    inspector: !!document.getElementById('cvInspector'),
    minimap: !!document.getElementById('cvMinimap'),
    trybar: !!document.getElementById('cvTryBar'),
    zoom: document.getElementById('cvZoomLabel').textContent,
    miniNodes: document.querySelectorAll('#cvMinimapSvg .rtc-mini-node').length,
    miniView: document.querySelectorAll('#cvMinimapSvg .rtc-mini-view').length,
    historyLimit: window.RTCanvas.HISTORY_LIMIT,
    scrollW: document.documentElement.scrollWidth,
    clientW: document.documentElement.clientWidth,
    scrollH: document.documentElement.scrollHeight,
    innerH: window.innerHeight
  }));
  assert.equal(metrics.nodes, 15);
  assert.equal(metrics.edges, 15);
  assert.equal(metrics.groups, 4);
  assert.ok(metrics.inspector && metrics.minimap && metrics.trybar);
  assert.match(metrics.zoom, /^\d+%$/);
  assert.equal(metrics.miniNodes, 15);
  assert.equal(metrics.miniView, 1);
  assert.equal(metrics.historyLimit, 100);
  assert.ok(metrics.scrollW <= metrics.clientW + 1, `horizontal overflow: ${metrics.scrollW} > ${metrics.clientW}`);
  assert.ok(metrics.scrollH <= metrics.innerH + 1, `vertical page scroll: ${metrics.scrollH} > ${metrics.innerH}`);
  const emptySlot = await page.evaluate(() => {
    const row = document.querySelector('#cvNodes .rtc-slot.is-empty');
    if (!row) return null;
    const id = row.querySelector('.rtc-slot-id');
    return {
      id: id ? id.textContent.trim() : '',
      addChips: document.querySelectorAll('#cvNodes .rtc-slot-add').length,
      display: getComputedStyle(row).display,
      border: getComputedStyle(row).borderTopStyle,
      title: row.getAttribute('title') || ''
    };
  });
  assert.ok(emptySlot, 'empty slot placeholder missing');
  assert.match(emptySlot.id, /^in_\d+$/);
  assert.equal(emptySlot.addChips, 0, 'canvas must not offer slot-add chips (inspector owns adding)');
  assert.equal(emptySlot.display, 'none', 'empty slots must stay hidden at rest');
  assert.equal(emptySlot.border, 'dashed');
  assert.match(emptySlot.title, /检查器|inspector/);
  const english = await page.evaluate(() => {
    window.setLang('en');
    const node = document.querySelector('[data-node-id="and-power"]');
    const chip = node.querySelector('[data-cond-edge]');
    const out = {
      badge: node.querySelector('.rtc-badge').textContent.trim(),
      chip: chip ? chip.textContent.trim() : ''
    };
    window.setLang('zh');
    return out;
  });
  assert.equal(english.badge, 'AND', 'logic operator name leaked graph.js Chinese into the EN card');
  assert.equal(english.chip, '> 0.65', 'numeric condition chip is not rendered from the port type');
  await screenshot('task10-canvas-1440.png');

  await page.setViewportSize({ width: 1280, height: 800 });
  await page.waitForFunction(() => window.innerWidth === 1280 &&
    document.getElementById('cvPalette').getBoundingClientRect().width > 0 &&
    document.getElementById('cvInspector').getBoundingClientRect().width > 0);
  const narrow = await page.evaluate(() => ({
    scrollW: document.documentElement.scrollWidth,
    clientW: document.documentElement.clientWidth,
    scrollH: document.documentElement.scrollHeight,
    innerH: window.innerHeight,
    paletteVisible: document.getElementById('cvPalette').getBoundingClientRect().width > 0,
    inspectorVisible: document.getElementById('cvInspector').getBoundingClientRect().width > 0
  }));
  assert.ok(narrow.scrollW <= narrow.clientW + 1, `1280 horizontal overflow: ${narrow.scrollW} > ${narrow.clientW}`);
  assert.ok(narrow.scrollH <= narrow.innerH + 1, `1280 page scroll: ${narrow.scrollH} > ${narrow.innerH}`);
  assert.ok(narrow.paletteVisible && narrow.inspectorVisible);
  await screenshot('task10-canvas-1280.png');
  await page.locator('[data-act="zoom-reset"]').click();
  await page.waitForFunction(() => document.querySelector('#cvZoomLabel').textContent === '100%' &&
    /scale\(1\)/.test(document.getElementById('cvWorld').style.transform));
  await screenshot('task10-canvas-100.png');
  await page.setViewportSize({ width: 1440, height: 960 });
});

test('Task 10 · blank drag, space+drag and middle-button drag all pan the viewport', async () => {
  await openCanvas();
  const start = await page.evaluate(() => window.App.kit.getViewport());
  let point = await blankPoint();
  assert.ok(point, 'no blank canvas point found');
  await page.mouse.move(point.x, point.y);
  await page.mouse.down();
  await page.mouse.move(point.x + 60, point.y + 30, { steps: 6 });
  await page.mouse.up();
  let viewport = await page.evaluate(() => window.App.kit.getViewport());
  assert.ok(Math.abs((viewport.tx - start.tx) - 60) < 2, `blank pan tx ${viewport.tx - start.tx} != 60`);
  assert.ok(Math.abs((viewport.ty - start.ty) - 30) < 2, `blank pan ty ${viewport.ty - start.ty} != 30`);
  assert.equal(Math.round(viewport.k * 1000), Math.round(start.k * 1000));
  assert.equal(await page.evaluate(() => window.App.kind()), 'clean');

  const beforeSpace = viewport;
  point = await blankPoint();
  await page.keyboard.down('Space');
  await page.mouse.move(point.x, point.y);
  await page.mouse.down();
  await page.mouse.move(point.x - 40, point.y + 25, { steps: 6 });
  await page.mouse.up();
  await page.keyboard.up('Space');
  viewport = await page.evaluate(() => window.App.kit.getViewport());
  assert.ok(Math.abs((viewport.tx - beforeSpace.tx) + 40) < 2, `space pan tx ${viewport.tx - beforeSpace.tx} != -40`);
  assert.ok(Math.abs((viewport.ty - beforeSpace.ty) - 25) < 2, `space pan ty ${viewport.ty - beforeSpace.ty} != 25`);

  const beforeMiddle = viewport;
  point = await blankPoint();
  await page.mouse.move(point.x, point.y);
  await page.mouse.down({ button: 'middle' });
  await page.mouse.move(point.x + 35, point.y - 20, { steps: 6 });
  await page.mouse.up({ button: 'middle' });
  viewport = await page.evaluate(() => window.App.kit.getViewport());
  assert.ok(Math.abs((viewport.tx - beforeMiddle.tx) - 35) < 2, `middle pan tx ${viewport.tx - beforeMiddle.tx} != 35`);
  assert.ok(Math.abs((viewport.ty - beforeMiddle.ty) + 20) < 2, `middle pan ty ${viewport.ty - beforeMiddle.ty} != -20`);

  /* 空格仍是按钮的原生激活键：焦点在顶栏按钮上时不能开始平移。 */
  await page.focus('#cvIssuesBtn');
  await page.keyboard.press('Space');
  await page.waitForFunction(() => !document.getElementById('cvIssues').hidden, null, { timeout: 2000 });
  assert.deepEqual(await page.evaluate(() => window.App.kit.getViewport()), viewport,
    'Space on a focused toolbar button started space-pan instead of activating the button');
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#cvIssues').isHidden(), true);
});

test('7.4-01-pan-zoom-fit-reset', async () => {
  await openCanvas();
  /* 平移（空白拖拽）也属于本条验收。 */
  const panStart = await page.evaluate(() => window.App.kit.getViewport());
  const blank = await blankPoint();
  assert.ok(blank, 'no blank canvas point for pan');
  await page.mouse.move(blank.x, blank.y);
  await page.mouse.down();
  await page.mouse.move(blank.x + 30, blank.y + 20, { steps: 5 });
  await page.mouse.up();
  const panned = await page.evaluate(() => window.App.kit.getViewport());
  assert.ok(Math.abs((panned.tx - panStart.tx) - 30) < 2, 'blank pan did not move X');
  assert.ok(Math.abs((panned.ty - panStart.ty) - 20) < 2, 'blank pan did not move Y');
  assert.equal(Math.round(panned.k * 1000), Math.round(panStart.k * 1000));

  const box = await page.locator('#cvStage').boundingBox();
  const point = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  const labelBefore = await page.locator('#cvZoomLabel').textContent();
  const worldBefore = await page.evaluate(([x, y]) => window.App.kit.screenToWorld(x, y), [point.x, point.y]);
  await page.mouse.move(point.x, point.y);
  await page.mouse.wheel(0, -240);
  await page.waitForFunction(previous => document.querySelector('#cvZoomLabel').textContent !== previous, labelBefore);
  const worldAfter = await page.evaluate(([x, y]) => window.App.kit.screenToWorld(x, y), [point.x, point.y]);
  assert.ok(Math.abs(worldAfter.x - worldBefore.x) < 1e-6, 'wheel zoom X anchor moved');
  assert.ok(Math.abs(worldAfter.y - worldBefore.y) < 1e-6, 'wheel zoom Y anchor moved');
  const labelZoomed = await page.locator('#cvZoomLabel').textContent();
  assert.notEqual(labelZoomed, labelBefore);

  await page.locator('[data-act="zoom-out"]').click();
  await page.waitForFunction(previous => document.querySelector('#cvZoomLabel').textContent !== previous, labelZoomed);
  const labelOut = await page.locator('#cvZoomLabel').textContent();
  await page.locator('[data-act="zoom-in"]').click();
  await page.waitForFunction(previous => document.querySelector('#cvZoomLabel').textContent !== previous, labelOut);

  await page.locator('[data-act="zoom-reset"]').click();
  await page.waitForFunction(() => document.querySelector('#cvZoomLabel').textContent === '100%');
  await page.locator('[data-act="zoom-fit"]').click();
  const inside = await page.waitForFunction(() => {
    const stage = document.getElementById('cvStage').getBoundingClientRect();
    const nodes = Array.from(document.querySelectorAll('#cvStage [data-node-id]'));
    return nodes.length > 0 && nodes.every(el => {
      const rect = el.getBoundingClientRect();
      return rect.left >= stage.left - 1 && rect.right <= stage.right + 1 &&
        rect.top >= stage.top - 1 && rect.bottom <= stage.bottom + 1;
    });
  }, null, { timeout: 3000 });
  assert.equal(await inside.jsonValue(), true, 'fit left some nodes outside the stage');
});

test('Task 10 · Esc closes the quick-create overlay before clearing the selection', async () => {
  await openCanvas();
  await clickNode('and-power');
  const selected = await page.evaluate(() => window.App.kit.getSelection());
  assert.deepEqual(selected, { kind: 'node', id: 'and-power' });
  const point = await blankPoint();
  await dragPort('capability', 'prob.easy', point);
  await page.waitForFunction(() => !document.getElementById('cvQuick').hidden, null, { timeout: 3000 });
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#cvQuick').isHidden(), true);
  assert.deepEqual(await page.evaluate(() => window.App.kit.getSelection()), selected);
  await page.keyboard.press('Escape');
  assert.deepEqual(await page.evaluate(() => window.App.kit.getSelection()), { kind: null, id: null });
});

test('7.4-02-node-drag-live-layout-only', async () => {
  await openCanvas();
  const path = page.locator('#cvEdges path.rtc-edge[data-edge-id="e-start-capability"]');
  const originalD = await path.getAttribute('d');
  const originalPosition = await page.evaluate(() => {
    const node = window.App.graph.nodes.find(item => item.id === 'start');
    return { x: node.position.x, y: node.position.y };
  });
  const box = await page.locator('[data-node-id="start"]').boundingBox();
  const start = { x: box.x + box.width / 2, y: box.y + 11 };
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await page.mouse.move(start.x + 70, start.y + 50, { steps: 6 });
  const duringD = await path.getAttribute('d');
  assert.notEqual(duringD, originalD, 'edge path did not change during node drag');
  await page.mouse.up();
  const movedD = await path.getAttribute('d');
  assert.notEqual(movedD, originalD, 'edge path did not change after node drag');
  const movedPosition = await page.evaluate(() => {
    const node = window.App.graph.nodes.find(item => item.id === 'start');
    return { x: node.position.x, y: node.position.y };
  });
  assert.notDeepEqual(movedPosition, originalPosition);
  const dirty = await page.evaluate(() => ({
    kind: document.getElementById('cvDirty').dataset.kind,
    hidden: document.getElementById('cvDirty').hidden,
    appKind: window.App.kind()
  }));
  assert.equal(dirty.hidden, false);
  assert.equal(dirty.kind, 'layout');
  assert.equal(dirty.appKind, 'layout');
  let snapshot = await appSnapshot();
  assert.deepEqual(snapshot.history, { undo: true, redo: false });

  const expectPosition = async expected => {
    await page.waitForFunction(value => {
      const node = window.App.graph.nodes.find(item => item.id === 'start');
      return node && node.position.x === value.x && node.position.y === value.y;
    }, expected);
  };

  await page.locator('#cvUndo').click();
  await expectPosition(originalPosition);
  snapshot = await appSnapshot();
  assert.equal(snapshot.kind, 'clean');
  assert.deepEqual(snapshot.history, { undo: false, redo: true });

  await page.locator('#cvRedo').click();
  await expectPosition(movedPosition);

  await page.keyboard.press(`${MOD}+z`);
  await expectPosition(originalPosition);
  await page.keyboard.press(`${MOD}+Shift+z`);
  await expectPosition(movedPosition);
});

test('7.4-03-node-library-four-groups', async () => {
  await openCanvas();
  const actions = [
    { kind: 'question-choice', how: 'click', match: node => node.type === 'question' && node.question_type === 'choice' },
    { kind: 'question-noul', how: 'drag', match: node => node.type === 'question' && node.question_type === 'noul' },
    { kind: 'logic-and', how: 'click', match: node => node.type === 'logic' && node.operator === 'and' },
    { kind: 'logic-not', how: 'drag', match: node => node.type === 'logic' && node.operator === 'not' },
    { kind: 'condition-text', how: 'click', match: node => node.type === 'condition' && node.source === 'text' },
    { kind: 'condition-periods', how: 'drag', match: node => node.type === 'condition' && node.source === 'periods' },
    { kind: 'target', how: 'click', match: node => node.type === 'target' },
    { kind: 'target', how: 'drag', match: node => node.type === 'target' }
  ];
  for (const action of actions) {
    const before = await appSnapshot();
    if (action.how === 'click') {
      await page.locator(`[data-lib-kind="${action.kind}"]`).click();
    } else {
      const point = await blankPoint();
      assert.ok(point, 'no blank drop point for node library drag');
      await dragLibraryItem(action.kind, point);
    }
    const after = await appSnapshot();
    const known = new Set(before.nodes.map(node => node.id));
    const added = after.nodes.filter(node => !known.has(node.id));
    assert.equal(added.length, 1, `${action.kind} (${action.how}) added ${added.length} nodes`);
    assert.ok(action.match(added[0]), `${action.kind} (${action.how}) created ${JSON.stringify(added[0])}`);
  }
});

test('7.4-05-numeric-default-and-boolean-true', async () => {
  await openCanvas();
  const before = await appSnapshot();
  assert.equal(await page.locator('#cvCondPop').isHidden(), true);
  const target = { node: 'and-power', port: 'in_3' };
  await dragPort('capability', 'prob.strong', target, {
    beforeUp: async () => {
      const classes = await page.evaluate(() => ({
        empty: document.querySelector('[data-node-id="and-power"] [data-port-id="in_3"]').classList.contains('is-compatible'),
        occupied: document.querySelector('[data-node-id="and-power"] [data-port-id="in_1"]').classList.contains('is-compatible')
      }));
      assert.equal(classes.empty, true, 'empty target slot was not highlighted as compatible');
      assert.equal(classes.occupied, false, 'occupied slot was highlighted as compatible');
    }
  });
  const after = await appSnapshot();
  assert.equal(after.edges.length, before.edges.length + 1);
  const edge = after.edges.find(item => item.target === 'and-power' && item.target_port === 'in_3');
  assert.ok(edge, 'new edge not found on and-power.in_3');
  assert.equal(edge.source, 'capability');
  assert.equal(edge.source_port, 'prob.strong');
  assert.deepEqual(edge.condition, { op: 'gt', value: 0.5 });
  const chipText = await page.locator(`#cvNodes [data-cond-edge="${edge.id}"]`).textContent();
  assert.equal(chipText.trim(), '> 0.5');
  const pop = await page.evaluate(() => ({
    hidden: document.getElementById('cvCondPop').hidden,
    edgeId: document.getElementById('cvCondPop').dataset.edgeId
  }));
  assert.equal(pop.hidden, false, 'condition editor did not open for a numeric edge');
  assert.equal(pop.edgeId, edge.id);

  await page.locator('#cvUndo').click();
  let undone = await appSnapshot();
  assert.equal(undone.edges.length, before.edges.length, 'one undo did not remove the numeric edge');
  await page.locator('#cvRedo').click();
  const redone = await appSnapshot();
  assert.equal(redone.edges.length, before.edges.length + 1, 'redo did not restore the numeric edge');

  /* 布尔线的默认条件：null（= true），且不打开条件浮层。 */
  await openCanvas();
  await dragPort('start', 'out', { node: 'capability', port: 'in_2' });
  const booleanEdge = (await appSnapshot()).edges.find(item =>
    item.source === 'start' && item.target === 'capability' && item.target_port === 'in_2');
  assert.ok(booleanEdge, 'boolean edge was not created');
  assert.equal(booleanEdge.condition, null, 'boolean edge did not default to null (= true)');
  assert.equal((await page.locator(`#cvNodes [data-cond-edge="${booleanEdge.id}"]`).textContent()).trim(), '= true');
  assert.equal(await page.locator('#cvCondPop').isHidden(), true,
    'boolean edge opened the comparison editor');
});

test('Task 10 · a boolean edge defaults to condition null and does not open the condition editor', async () => {
  await openCanvas();
  const before = await appSnapshot();
  await dragPort('start', 'out', { node: 'and-power', port: 'in_3' });
  const after = await appSnapshot();
  assert.equal(after.edges.length, before.edges.length + 1);
  const edge = after.edges.find(item => item.target === 'and-power' && item.target_port === 'in_3');
  assert.ok(edge, 'boolean edge not created');
  assert.equal(edge.condition, null);
  const chipText = await page.locator(`#cvNodes [data-cond-edge="${edge.id}"]`).textContent();
  assert.equal(chipText.trim(), '= true');
  assert.equal(await page.locator('#cvCondPop').isHidden(), true);
});

test('Task 10 · a noul question output connects to an AND gate slot', async () => {
  await openCanvas();
  /* 复现用户反馈：noul 的 yes/no 输出端口拖到与门空槽位必须能建立数值连线。 */
  const domainBox = await page.locator('[data-node-id="domain"]').boundingBox();
  assert.ok(domainBox, 'domain node not visible');
  const idsBefore = await page.evaluate(() => window.App.graph.nodes.map(node => node.id));
  await dragLibraryItem('logic-and', {
    x: domainBox.x + domainBox.width + 220,
    y: domainBox.y + 120,
  });
  const andId = await page.evaluate(ids => {
    const created = window.App.graph.nodes.filter(node =>
      node.type === 'logic' && node.operator === 'and' && !ids.includes(node.id));
    return created.length ? created[created.length - 1].id : null;
  }, idsBefore);
  assert.ok(andId, 'AND gate was not created from the library');

  await dragPort('domain', 'yes', { node: andId, port: 'in_1' });
  const edge = (await appSnapshot()).edges.find(item =>
    item.source === 'domain' && item.source_port === 'yes' &&
    item.target === andId && item.target_port === 'in_1');
  assert.ok(edge, 'noul yes → AND in_1 edge was not created');
  assert.deepEqual(edge.condition, { op: 'gt', value: 0.5 }, 'numeric edge did not default to gt 0.5');
  const chipText = await page.locator(`#cvNodes [data-cond-edge="${edge.id}"]`).textContent();
  assert.equal(chipText.replace(/\s+/g, ''), '>0.5');

  /* 几何回归：汇总行输出端口必须吸附卡片右缘（不能回到行内左侧被误读成开关）。 */
  const portGeo = await page.evaluate(() => {
    const card = document.querySelector('[data-node-id="domain"]').getBoundingClientRect();
    const port = document.querySelector(
      '[data-node-id="domain"] [data-port-id="yes"][data-port-dir="out"]').getBoundingClientRect();
    return { cardRight: card.right, portCx: port.x + port.width / 2 };
  });
  assert.ok(Math.abs(portGeo.cardRight - portGeo.portCx) <= 9,
    `noul yes port is not on the card right edge (card ${portGeo.cardRight}, port ${portGeo.portCx})`);
  const optPortGeo = await page.evaluate(() => {
    const card = document.querySelector('[data-node-id="capability"]').getBoundingClientRect();
    const port = document.querySelector(
      '[data-node-id="capability"] [data-port-id="selected.easy"][data-port-dir="out"]').getBoundingClientRect();
    const cardCx = card.x + card.width / 2;
    return { cardRight: card.right, portCx: port.x + port.width / 2, cardCx };
  });
  assert.ok(optPortGeo.portCx > optPortGeo.cardCx,
    'choice option output port is not in the right-edge zone');
});

test('Task 10 · a condition chip opens the condition overlay and Esc closes it', async () => {
  await openCanvas();
  const chip = page.locator('#cvNodes [data-cond-edge]').first();
  const edgeId = await chip.getAttribute('data-cond-edge');
  await chip.click();
  const pop = await page.evaluate(() => ({
    hidden: document.getElementById('cvCondPop').hidden,
    edgeId: document.getElementById('cvCondPop').dataset.edgeId
  }));
  assert.equal(pop.hidden, false);
  assert.equal(pop.edgeId, edgeId);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#cvCondPop').isHidden(), true);
});

test('7.4-04-port-type-and-reject-reasons', async () => {
  await openCanvas();
  const before = await appSnapshot();
  const cases = [
    { source: 'capability', port: 'prob.easy', node: 'and-power', slot: 'in_1', pattern: /占用/ },
    { source: 'capability', port: 'prob.easy', node: 'capability', slot: 'in_2', pattern: /自身/ },
    { source: 'and-power', port: 'out', node: 'capability', slot: 'in_2', pattern: /环/ },
    { source: 'start', port: 'out', node: 'capability', slot: 'in_1', pattern: /已存在/ }
  ];
  for (const item of cases) {
    await clearToasts();
    await dragPort(item.source, item.port, { node: item.node, port: item.slot }, {
      beforeUp: async () => {
        const compatible = await page.evaluate(([node, slot]) =>
          document.querySelector(`[data-node-id="${node}"] [data-port-id="${slot}"]`).classList.contains('is-compatible'),
        [item.node, item.slot]);
        assert.equal(compatible, false, `${item.node}.${item.slot} should not be compatible`);
      }
    });
    await page.waitForFunction(() => document.getElementById('cvToast').textContent.trim().length > 0, null, { timeout: 3000 });
    const text = await toastText();
    assert.match(text, item.pattern, `wrong toast for ${item.source}.${item.port} → ${item.node}.${item.slot}: ${text}`);
    const now = await appSnapshot();
    assert.equal(now.edges.length, before.edges.length, 'invalid drop changed the edge count');
  }

  /* 类型/结构不兼容：条件节点没有输入槽位，拖到它上面不得高亮任何兼容端口。 */
  const condBox = await page.locator('[data-node-id="cond-text"]').boundingBox();
  assert.ok(condBox, 'condition node not visible');
  await dragPort('capability', 'prob.easy',
    { x: condBox.x + condBox.width / 2, y: condBox.y + 14 },
    {
      beforeUp: async () => {
        assert.equal(await page.evaluate(() =>
          document.querySelector('[data-node-id="cond-text"]')
            .querySelectorAll('.is-compatible, .is-incompatible').length), 0,
        'a node without input slots was highlighted as a drop target');
      }
    });
  assert.equal(await page.locator('#cvQuick').isHidden(), true,
    'dropping on an incompatible node opened quick-create');
  assert.equal((await appSnapshot()).edges.length, before.edges.length,
    'dropping on an incompatible node created an edge');
});

test('Task 10 · output dropped on blank opens quick-create; dropping on an illegal node only cancels', async () => {
  await openCanvas();
  const before = await appSnapshot();
  const point = await blankPoint();
  assert.ok(point);
  await dragPort('capability', 'prob.easy', point);
  await page.waitForFunction(() => !document.getElementById('cvQuick').hidden, null, { timeout: 3000 });
  assert.equal(await page.locator('#cvQuick [data-qkind="logic-and"]').count(), 1);
  await page.locator('#cvQuick [data-qkind="logic-and"]').click();
  let after = await appSnapshot();
  assert.equal(after.nodes.length, before.nodes.length + 1);
  assert.equal(after.edges.length, before.edges.length + 1);
  const node = after.nodes.find(item => !before.nodes.some(previous => previous.id === item.id));
  assert.ok(node && node.type === 'logic' && node.operator === 'and');
  const edge = after.edges.find(item => item.target === node.id);
  assert.ok(edge, 'quick-created node has no edge');
  assert.equal(edge.source, 'capability');
  assert.equal(edge.source_port, 'prob.easy');
  assert.equal(edge.target_port, 'in_1');
  assert.deepEqual(edge.condition, { op: 'gt', value: 0.5 });

  await openCanvas();
  const beforeCancel = await appSnapshot();
  const nodeBox = await page.locator('[data-node-id="and-power"]').boundingBox();
  await dragPort('capability', 'prob.easy', { x: nodeBox.x + nodeBox.width / 2, y: nodeBox.y + 12 });
  await expectNever(() => !document.getElementById('cvQuick').hidden,
    'quick-create opened for a drop on an illegal node');
  assert.equal(await page.locator('#cvQuick').isHidden(), true);
  const afterCancel = await appSnapshot();
  assert.equal(afterCancel.nodes.length, beforeCancel.nodes.length);
  assert.equal(afterCancel.edges.length, beforeCancel.edges.length);
});

test('7.4-10-delete-confirm-and-slot-reuse', async () => {
  await openCanvas();
  await clickNode('and-power');
  let selection = await page.evaluate(() => window.App.kit.getSelection());
  assert.deepEqual(selection, { kind: 'node', id: 'and-power' });
  await page.keyboard.press('Delete');
  await page.waitForFunction(() => !document.getElementById('cvDeleteModal').hidden, null, { timeout: 3000 });
  assert.equal(await page.locator('#cvDeleteBody li').count(), 3);
  const dialogText = await page.locator('#cvDeleteBody').innerText();
  assert.match(dialogText, /单选题/);
  assert.match(dialogText, /是否问题-1/);
  assert.match(dialogText, /模型目标-2/);
  await page.locator('[data-act="delete-confirm"]').click();
  await page.waitForFunction(() => !document.querySelector('[data-node-id="and-power"]'));
  let snapshot = await appSnapshot();
  assert.equal(snapshot.nodes.some(node => node.id === 'and-power'), false);
  assert.equal(snapshot.edges.filter(edge => edge.source === 'and-power' || edge.target === 'and-power').length, 0);

  await openCanvas();
  await clickNode('cond-text');
  await page.keyboard.press('Delete');
  await page.waitForFunction(() => !document.getElementById('cvDeleteModal').hidden, null, { timeout: 3000 });
  await page.locator('[data-act="delete-confirm"]').click();
  await page.waitForFunction(() => !document.querySelector('[data-node-id="cond-text"]'));
  snapshot = await appSnapshot();
  let orEdges = snapshot.edges.filter(edge => edge.target === 'or-ctx');
  assert.equal(orEdges.length, 1);
  assert.equal(orEdges[0].target_port, 'in_2');
  assert.equal(orEdges[0].source, 'cond-peak');

  await dragPort('cond-peak', 'out', { node: 'or-ctx', port: 'in_1' });
  snapshot = await appSnapshot();
  const reused = snapshot.edges.find(edge => edge.target === 'or-ctx' && edge.target_port === 'in_1');
  assert.ok(reused, 'new edge did not reuse the freed in_1 slot');
  assert.equal(reused.source, 'cond-peak');
  orEdges = snapshot.edges.filter(edge => edge.target === 'or-ctx');
  assert.equal(orEdges.length, 2);
  assert.ok(orEdges.some(edge => edge.target_port === 'in_2' && edge.source === 'cond-peak'), 'existing in_2 edge was renumbered');
});

test('Task 10 · auto layout is layout-only and a second run adds no history entry', async () => {
  await openCanvas();
  const before = await page.evaluate(() => Object.fromEntries(window.App.graph.nodes.map(node => [node.id, { x: node.position.x, y: node.position.y }])));
  const expected = await page.evaluate(() => window.RTGraph.layoutPositions('dag', window.App.graph));
  assert.equal(Object.keys(expected).length, Object.keys(before).length);

  await page.locator('#cvAutoLayout').click();
  await page.waitForFunction(positions => {
    const nodes = window.App.graph.nodes;
    return nodes.length === Object.keys(positions).length &&
      nodes.every(node => node.position.x === positions[node.id].x && node.position.y === positions[node.id].y);
  }, expected, { timeout: 3000 });

  const laidOut = await page.evaluate(() => ({
    positions: Object.fromEntries(window.App.graph.nodes.map(node => [node.id, { x: node.position.x, y: node.position.y }])),
    edges: window.App.graph.edges.map(edge => ({ source: edge.source, target: edge.target })),
    kind: window.App.kind(),
    badge: { kind: document.getElementById('cvDirty').dataset.kind, hidden: document.getElementById('cvDirty').hidden },
    history: { undo: window.App.kit.canUndo(), redo: window.App.kit.canRedo() }
  }));
  const moved = Object.keys(laidOut.positions)
    .filter(id => laidOut.positions[id].x !== before[id].x || laidOut.positions[id].y !== before[id].y);
  assert.equal(moved.length, Object.keys(before).length, 'auto layout did not move every node');
  assert.equal(laidOut.kind, 'layout', 'auto layout must not dirty behavior');
  assert.deepEqual(laidOut.badge, { kind: 'layout', hidden: false });
  assert.deepEqual(laidOut.history, { undo: true, redo: false });
  laidOut.edges.forEach(edge => {
    assert.ok(laidOut.positions[edge.target].x > laidOut.positions[edge.source].x,
      `${edge.source} -> ${edge.target} is not laid out left-to-right`);
  });

  /* 第二次自动整理是 no-op：不得再入栈。若入了空历史，下面一次 undo 会退到同样的布局。 */
  await page.locator('#cvAutoLayout').click();
  await page.locator('#cvUndo').click();
  await page.waitForFunction(positions => {
    const nodes = window.App.graph.nodes;
    return nodes.length === Object.keys(positions).length &&
      nodes.every(node => node.position.x === positions[node.id].x && node.position.y === positions[node.id].y);
  }, before, { timeout: 3000 });
  const undone = await page.evaluate(() => ({
    kind: window.App.kind(),
    badgeHidden: document.getElementById('cvDirty').hidden,
    history: { undo: window.App.kit.canUndo(), redo: window.App.kit.canRedo() }
  }));
  assert.equal(undone.kind, 'clean', 'undo after a no-op layout did not restore the saved positions');
  assert.equal(undone.badgeHidden, true);
  assert.deepEqual(undone.history, { undo: false, redo: true });
});

test('Task 10 · edge delete confirm lists the edge, removes it and undo restores it', async () => {
  await openCanvas();
  const edgeId = 'e-selected-strong-strong';
  const before = await appSnapshot();
  assert.equal(before.edges.some(edge => edge.id === edgeId), true);
  await clickEdge(edgeId);
  assert.deepEqual(await page.evaluate(() => window.App.kit.getSelection()), { kind: 'edge', id: edgeId });
  await page.keyboard.press('Delete');
  await page.waitForFunction(() => !document.getElementById('cvDeleteModal').hidden, null, { timeout: 3000 });
  assert.equal(await page.locator('#cvDeleteBody li').count(), 1);
  const dialogText = await page.locator('#cvDeleteBody').innerText();
  assert.match(dialogText, /单选题/);
  assert.match(dialogText, /selected\.strong/);
  assert.match(dialogText, /模型目标-3/);
  assert.match(dialogText, /in_1/);

  await page.locator('[data-act="delete-confirm"]').click();
  await page.waitForFunction(id => !window.App.graph.edges.some(edge => edge.id === id), edgeId);
  assert.equal(await page.locator(`#cvEdges path.rtc-edge[data-edge-id="${edgeId}"]`).count(), 0);
  const removed = await appSnapshot();
  assert.equal(removed.edges.length, before.edges.length - 1);
  assert.equal(removed.edges.some(edge => edge.id === edgeId), false);

  await page.locator('#cvUndo').click();
  await page.waitForFunction(id => window.App.graph.edges.some(edge => edge.id === id), edgeId);
  const restored = await appSnapshot();
  const edge = restored.edges.find(item => item.id === edgeId);
  assert.ok(edge, 'undo did not restore the deleted edge');
  assert.equal(edge.source, 'capability');
  assert.equal(edge.source_port, 'selected.strong');
  assert.equal(edge.target, 'strong-target');
  assert.equal(edge.target_port, 'in_1');
  assert.equal(edge.condition, null);
  assert.equal(restored.kind, 'clean');
});

test('Task 10 · Delete/Backspace do nothing while an input is focused', async () => {
  await openCanvas();
  const before = await appSnapshot();
  await clickNode('and-power');
  await page.locator('#cvTryInput').click();
  await page.keyboard.press('Backspace');
  await page.keyboard.press('Delete');
  await expectNever(() => !document.getElementById('cvDeleteModal').hidden,
    'Delete key opened the confirm modal while an input was focused');
  assert.equal(await page.locator('#cvDeleteModal').isHidden(), true);
  const after = await appSnapshot();
  assert.equal(after.nodes.length, before.nodes.length);
});

/* ── Task 11 · 检查器 / 条件双入口 / 级联删除 ─────────────────────── */

async function waitCondPop(edgeId) {
  await page.waitForFunction(id => {
    const pop = document.getElementById('cvCondPop');
    return pop && !pop.hidden && pop.dataset.edgeId === id;
  }, edgeId, { timeout: 4000 });
}

async function waitInspectorBranch(branch, id) {
  await page.waitForFunction(([kind, value]) => {
    const root = document.querySelector('#cvInspectorBody [data-ins-branch]');
    return root && root.dataset.insBranch === kind && (!value || root.dataset.insId === value);
  }, [branch, id], { timeout: 4000 });
}

async function waitEdgeCondition(edgeId, check) {
  await page.waitForFunction(([id, expected]) => {
    const edge = window.App.graph.edges.find(item => item.id === id);
    if (!edge) return false;
    if (expected === null) return edge.condition === null;
    return edge.condition && Object.keys(expected).every(key => edge.condition[key] === expected[key]);
  }, [edgeId, check], { timeout: 4000 });
}

async function edgeOf(edgeId) {
  return page.evaluate(id => {
    const edge = window.App.graph.edges.find(item => item.id === id);
    return edge ? JSON.parse(JSON.stringify(edge)) : null;
  }, edgeId);
}

async function danglingEdgeIds() {
  return page.evaluate(() => {
    const graph = window.App.graph;
    return graph.edges.filter(edge => {
      const source = window.RTGraph.findNode(graph, edge.source);
      const target = window.RTGraph.findNode(graph, edge.target);
      return !source || !target ||
        !window.RTGraph.portOf(source, edge.source_port, graph) ||
        !window.RTGraph.portOf(target, edge.target_port, graph);
    }).map(edge => edge.id);
  });
}

async function issueCodes() {
  return page.evaluate(() => window.App.issues.filter(issue => issue.level !== 'hint').map(issue => issue.code));
}

async function openIssuesPanel() {
  await page.locator('#cvIssuesBtn').click();
  await page.waitForFunction(() => !document.getElementById('cvIssues').hidden);
}

test('7.4-06-condition-edit-dual-entry-dirty-undo', async () => {
  await openCanvas();
  const edgeId = 'e-cap-strong-and-power';
  await page.locator(`#cvNodes [data-cond-edge="${edgeId}"]`).click();
  await waitCondPop(edgeId);

  const operators = await page.evaluate(() => Array.from(
    document.querySelectorAll('#cvCondPop [data-cond-field="op"] option'))
    .map(option => [option.value, option.textContent.trim()]));
  assert.deepEqual(operators.map(item => item[0]), ['gt', 'gte', 'lt', 'lte', 'eq', 'neq'],
    'numeric operator dropdown does not expose the six canonical ops in order');
  assert.deepEqual(operators.map(item => item[1]), ['>', '≥', '<', '≤', '=', '≠']);
  assert.equal(await page.locator('#cvCondPop [data-cond-field="op"]').inputValue(), 'gt');
  assert.equal(await page.locator('#cvCondPop [data-cond-field="value"]').inputValue(), '0.65');
  await screenshot('condition-editing-1440.png');

  /* NaN / Infinity / empty / |v| > 1e9 must stay in the editor and write nothing. */
  const historyBefore = (await appSnapshot()).history;
  for (const raw of ['', '   ', 'NaN', 'Infinity', '1e10', '-1000000001', 'abc']) {
    await page.locator('#cvCondPop [data-cond-field="op"]').selectOption('gt');
    await page.locator('#cvCondPop [data-cond-field="value"]').fill(raw);
    await page.locator('#cvCondPop [data-act="cond-confirm"]').click();
    assert.equal(await page.locator('#cvCondPop').isHidden(), false, `bad threshold ${JSON.stringify(raw)} closed the editor`);
    const message = (await page.locator('#cvCondPop [data-cond-error]').textContent()).trim();
    assert.ok(message.length > 0, `bad threshold ${JSON.stringify(raw)} produced no inline error`);
    const current = (await edgeOf(edgeId)).condition;
    assert.deepEqual(current, { op: 'gt', value: 0.65 }, `bad threshold ${JSON.stringify(raw)} wrote the condition`);
    assert.deepEqual((await appSnapshot()).history, historyBefore, 'a rejected condition entered the history stack');
  }

  /* Valid path through the slot chip entry. */
  await page.locator('#cvCondPop [data-cond-field="op"]').selectOption('lte');
  await page.locator('#cvCondPop [data-cond-field="value"]').fill('0.42');
  await page.locator('#cvCondPop [data-act="cond-confirm"]').click();
  await page.waitForFunction(id => {
    const edge = window.App.graph.edges.find(item => item.id === id);
    return edge && edge.condition && edge.condition.op === 'lte' && edge.condition.value === 0.42;
  }, edgeId);
  assert.equal(await page.locator('#cvCondPop').isHidden(), true);
  const focusedEdge = await page.evaluate(() => {
    const active = document.activeElement;
    return active && active.dataset ? (active.dataset.condEdge || '') : '';
  });
  assert.equal(focusedEdge, edgeId, 'condition confirm did not return focus to the re-rendered condition chip');
  const chipText = (await page.locator(`#cvNodes [data-cond-edge="${edgeId}"]`).textContent()).trim();
  assert.equal(chipText, '≤ 0.42');
  let snapshot = await appSnapshot();
  assert.equal(snapshot.kind, 'behavior', 'condition confirm did not mark the draft behavior-dirty');
  assert.deepEqual(snapshot.history, { undo: true, redo: false });
  await page.locator('#cvUndo').click();
  await waitEdgeCondition(edgeId, { op: 'gt', value: 0.65 });
  snapshot = await appSnapshot();
  assert.equal(snapshot.kind, 'clean');
  assert.deepEqual(snapshot.history, { undo: false, redo: true }, 'one confirm must be exactly one history entry');

  /* The inspector edge branch uses the exact same form contract and commit path. */
  await openCanvas();
  await clickEdge(edgeId);
  await waitInspectorBranch('edge', edgeId);
  assert.equal(await page.locator('#cvInspectorBody [data-ins-branch="edge"]').getAttribute('data-edge-type'), 'number');
  const typeLabel = await page.evaluate(() => {
    const rows = Array.from(document.querySelectorAll('#cvInspectorBody .rtc-ins-row'));
    const row = rows.find(item => item.querySelector('span') &&
      item.querySelector('span').textContent.trim() === window.t('cv.ins.type'));
    return row && row.querySelector('b') ? row.querySelector('b').textContent.trim() : '';
  });
  assert.equal(typeLabel, await page.evaluate(() => window.t('cv.ins.typeNumber')),
    'edge inspector did not label the rendered edge type');

  /* Bad numbers must be rejected through the inspector entry too (same contract). */
  const inspectorHistoryBefore = (await appSnapshot()).history;
  for (const raw of ['', '   ', 'NaN', 'Infinity', '1e10', '-1000000001', 'abc']) {
    await page.locator('#cvInspectorBody [data-cond-field="op"]').selectOption('gt');
    await page.locator('#cvInspectorBody [data-cond-field="value"]').fill(raw);
    await page.locator('#cvInspectorBody [data-act="ins-save-edge"]').click();
    const message = (await page.locator('#cvInspectorBody [data-ins-error]').textContent()).trim();
    const expected = await page.evaluate(key => window.t(key),
      raw.trim() === '' ? 'cv.cond.needValue' : 'cv.cond.badValue');
    assert.equal(message, expected, `inspector accepted/reworded bad threshold ${JSON.stringify(raw)}`);
    assert.deepEqual(await edgeOf(edgeId), {
      id: edgeId,
      source: 'capability',
      source_port: 'prob.strong',
      target: 'and-power',
      target_port: 'in_1',
      condition: { op: 'gt', value: 0.65 }
    }, `inspector bad threshold ${JSON.stringify(raw)} wrote the condition`);
    assert.deepEqual((await appSnapshot()).history, inspectorHistoryBefore,
      'a rejected inspector condition entered the history stack');
  }

  await page.locator('#cvInspectorBody [data-cond-field="op"]').selectOption('neq');
  await page.locator('#cvInspectorBody [data-cond-field="value"]').fill('0.7');
  await page.locator('#cvInspectorBody [data-act="ins-save-edge"]').click();
  await waitEdgeCondition(edgeId, { op: 'neq', value: 0.7 });
  assert.equal((await page.locator(`#cvNodes [data-cond-edge="${edgeId}"]`).textContent()).trim(), '≠ 0.7');
  snapshot = await appSnapshot();
  assert.equal(snapshot.kind, 'behavior');
  await page.locator('#cvUndo').click();
  await waitEdgeCondition(edgeId, { op: 'gt', value: 0.65 });
  assert.deepEqual((await appSnapshot()).history, { undo: false, redo: true });
});

test('Task 11 · boolean conditions toggle = true / = false from both entries without illegal states', async () => {
  await openCanvas();
  const edgeId = 'e-selected-easy-light';
  await page.locator(`#cvNodes [data-cond-edge="${edgeId}"]`).click();
  await waitCondPop(edgeId);
  const options = await page.evaluate(() => Array.from(
    document.querySelectorAll('#cvCondPop [data-cond-field="bool"] option')).map(option => option.value));
  assert.deepEqual(options, ['true', 'false']);
  assert.equal(await page.locator('#cvCondPop [data-cond-field="bool"]').inputValue(), 'true');

  /* Cancel and Esc save the original copy and write nothing. */
  await page.locator('#cvCondPop [data-cond-field="bool"]').selectOption('false');
  await page.locator('#cvCondPop [data-act="cond-cancel"]').click();
  assert.equal(await page.locator('#cvCondPop').isHidden(), true);
  assert.equal((await edgeOf(edgeId)).condition, null);
  let snapshot = await appSnapshot();
  assert.equal(snapshot.kind, 'clean');
  assert.deepEqual(snapshot.history, { undo: false, redo: false });

  await page.locator(`#cvNodes [data-cond-edge="${edgeId}"]`).click();
  await waitCondPop(edgeId);
  await page.locator('#cvCondPop [data-cond-field="bool"]').selectOption('false');
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#cvCondPop').isHidden(), true);
  assert.equal(await page.evaluate(() => {
    const active = document.activeElement;
    return active && active.dataset ? (active.dataset.condEdge || '') : '';
  }), edgeId, 'Esc did not return focus to the condition chip');
  assert.equal((await edgeOf(edgeId)).condition, null);
  assert.equal((await appSnapshot()).kind, 'clean');

  /* Confirm false → {op:'is', value:false}; then true → canonical null. */
  await page.locator(`#cvNodes [data-cond-edge="${edgeId}"]`).click();
  await waitCondPop(edgeId);
  await page.locator('#cvCondPop [data-cond-field="bool"]').selectOption('false');
  await page.locator('#cvCondPop [data-act="cond-confirm"]').click();
  await waitEdgeCondition(edgeId, { op: 'is', value: false });
  assert.equal((await page.locator(`#cvNodes [data-cond-edge="${edgeId}"]`).textContent()).trim(), '= false');
  await page.locator(`#cvNodes [data-cond-edge="${edgeId}"]`).click();
  await waitCondPop(edgeId);
  assert.equal(await page.locator('#cvCondPop [data-cond-field="bool"]').inputValue(), 'false');
  await page.locator('#cvCondPop [data-cond-field="bool"]').selectOption('true');
  await page.locator('#cvCondPop [data-act="cond-confirm"]').click();
  await waitEdgeCondition(edgeId, null);
  assert.equal((await page.locator(`#cvNodes [data-cond-edge="${edgeId}"]`).textContent()).trim(), '= true');
  await page.locator('#cvUndo').click();
  await waitEdgeCondition(edgeId, { op: 'is', value: false });

  /* Inspector entry writes the same shape and one undo restores it. */
  await openCanvas();
  await clickEdge(edgeId);
  await waitInspectorBranch('edge', edgeId);
  assert.equal(await page.locator('#cvInspectorBody [data-ins-branch="edge"]').getAttribute('data-edge-type'), 'boolean');
  await page.locator('#cvInspectorBody [data-cond-field="bool"]').selectOption('false');
  await page.locator('#cvInspectorBody [data-act="ins-save-edge"]').click();
  await waitEdgeCondition(edgeId, { op: 'is', value: false });
  snapshot = await appSnapshot();
  assert.equal(snapshot.kind, 'behavior');
  await page.locator('#cvUndo').click();
  await waitEdgeCondition(edgeId, null);
  assert.deepEqual((await appSnapshot()).history, { undo: false, redo: true });
});

test('7.4-07-selected-edge-inspector', async () => {
  await openCanvas();
  const edgeId = 'e-cap-strong-and-power';
  await clickEdge(edgeId);
  await waitInspectorBranch('edge', edgeId);
  const text = await page.locator('#cvInspectorBody').innerText();
  assert.match(text, /单选题/);
  assert.match(text, /prob\.strong/);
  assert.match(text, /与门-2/);
  assert.match(text, /in_1/);
  assert.match(text, /> 0\.65/);
  const typeLabel = await page.evaluate(() => {
    const rows = Array.from(document.querySelectorAll('#cvInspectorBody .rtc-ins-row'));
    const row = rows.find(item => item.querySelector('span') &&
      item.querySelector('span').textContent.trim() === window.t('cv.ins.type'));
    return row && row.querySelector('b') ? row.querySelector('b').textContent.trim() : '';
  });
  assert.equal(typeLabel, await page.evaluate(() => window.t('cv.ins.typeNumber')),
    'numeric edge inspector did not label the edge type');

  await page.locator('#cvInspectorBody [data-act="ins-delete-edge"]').click();
  await page.waitForFunction(() => !document.getElementById('cvDeleteModal').hidden, null, { timeout: 3000 });
  assert.equal(await page.locator('#cvDeleteBody li').count(), 1);
  const dialog = await page.locator('#cvDeleteBody').innerText();
  assert.match(dialog, /单选题/);
  assert.match(dialog, /prob\.strong/);
  assert.match(dialog, /与门-2/);
  assert.match(dialog, /in_1/);
  await page.locator('[data-act="delete-confirm"]').click();
  await page.waitForFunction(id => !window.App.graph.edges.some(edge => edge.id === id), edgeId);
  assert.equal(await page.locator(`#cvEdges path.rtc-edge[data-edge-id="${edgeId}"]`).count(), 0);
  assert.deepEqual(await danglingEdgeIds(), []);
  assert.equal((await issueCodes()).includes('edge_port_missing'), false);

  await page.locator('#cvUndo').click();
  await page.waitForFunction(id => window.App.graph.edges.some(edge => edge.id === id), edgeId);
  const restored = await edgeOf(edgeId);
  assert.deepEqual(restored.condition, { op: 'gt', value: 0.65 });
  assert.equal(restored.target, 'and-power');
  assert.equal(restored.target_port, 'in_1');
  assert.equal((await appSnapshot()).kind, 'clean');
});

test('Task 11 · validate-bar edge and graph issues locate and open the same condition editor / graph settings', async () => {
  await openCanvas();
  const edgeId = 'e-clarify-deep';
  await page.evaluate(id => {
    window.App.change('behavior', tree => {
      tree.root.edges.find(edge => edge.id === id).condition = { op: 'bogus', value: 1 };
    });
  }, edgeId);
  await page.waitForFunction(id => window.App.issues.some(issue =>
    issue.edge_id === id && issue.code === 'numeric_edge_condition_invalid'), edgeId);
  assert.equal((await issueCodes()).filter(code => code === 'numeric_edge_condition_invalid').length, 1);

  await openIssuesPanel();
  await page.locator('#cvIssues [data-issue-edge="e-clarify-deep"]').click();
  await waitCondPop(edgeId);
  assert.deepEqual(await page.evaluate(() => window.App.kit.getSelection()), { kind: 'edge', id: edgeId });
  await page.locator('#cvCondPop [data-cond-field="op"]').selectOption('gt');
  await page.locator('#cvCondPop [data-cond-field="value"]').fill('0.61');
  await page.locator('#cvCondPop [data-act="cond-confirm"]').click();
  await page.waitForFunction(() => !window.App.issues.some(issue => issue.code === 'numeric_edge_condition_invalid'));
  assert.equal(await page.locator('#cvCondPop').isHidden(), true);

  /* Graph-level issue opens graph settings and can be fixed there. */
  await page.evaluate(() => {
    window.App.change('behavior', tree => { tree.root.otherwise_target = null; });
  });
  await page.waitForFunction(() => window.App.issues.some(issue => issue.code === 'fallback_missing'));
  await page.locator('#cvIssuesBtn').click();
  if (await page.locator('#cvIssues').isHidden()) await page.locator('#cvIssuesBtn').click();
  await page.locator('#cvIssues [data-issue-graph="1"]').first().click();
  assert.equal(await page.evaluate(() =>
    document.getElementById('cvInspector').classList.contains('is-focus')), true,
  'graph issue locate did not light up the inspector');
  assert.equal(await page.evaluate(() => {
    const style = getComputedStyle(document.getElementById('cvInspector'));
    return style.outlineStyle + ' ' + style.outlineWidth;
  }), 'solid 2px', 'is-focus has no visible token-based focus ring');
  await waitInspectorBranch('graph', 'root');
  assert.deepEqual(await page.evaluate(() => window.App.kit.getSelection()), { kind: null, id: null });
  await page.locator('#cvInspectorBody [data-field="otherwise"]').selectOption('standard-target');
  await page.locator('#cvInspectorBody [data-act="ins-save-graph"]').click();
  await page.waitForFunction(() => !window.App.issues.some(issue => issue.code === 'fallback_missing'));
  assert.equal(await page.evaluate(() => window.App.graph.otherwise_target), 'standard-target');
  await page.locator('#cvUndo').click();
  await page.waitForFunction(() => window.App.graph.otherwise_target === null);
});

test('7.4-08-logic-slot-rows', async () => {
  await openCanvas();
  const before = await appSnapshot();
  await page.locator('[data-lib-kind="logic-and"]').click();
  const andId = await page.evaluate(known => {
    const node = window.App.graph.nodes.find(item =>
      item.type === 'logic' && item.operator === 'and' && known.indexOf(item.id) < 0);
    return node && node.id;
  }, before.nodes.map(node => node.id));
  assert.ok(andId, 'AND node was not created');
  await page.waitForFunction(id => window.App.issues.some(issue =>
    issue.node_id === id && issue.code === 'logic_slot_count'), andId);
  assert.equal(await page.locator(`#cvNodes [data-node-id="${andId}"] .rtc-slot`).count(), 2,
    'a new AND gate must render both input rows');
  assert.equal(await page.locator('#cvNodes [data-node-id="and-power"] .rtc-slot').count(), 3,
    'AND renders its two occupied rows plus the next free slot');
  assert.equal(await page.locator('#cvNodes [data-node-id="or-ctx"]').getAttribute('data-operator'), 'or');
  const orRows = page.locator('#cvNodes [data-node-id="or-ctx"] .rtc-slot');
  assert.equal(await orRows.count(), 3, 'OR renders its two occupied rows plus the next free slot');
  assert.match(await orRows.nth(0).innerText(), /文本条件/);
  assert.match(await orRows.nth(0).innerText(), /in_1/);
  assert.match(await orRows.nth(1).innerText(), /时段条件/);
  assert.match(await orRows.nth(1).innerText(), /in_2/);
  assert.equal(await orRows.nth(2).locator('.rtc-slot-add').count(), 0,
    'empty slot rows no longer render an add chip');
  assert.match(await orRows.nth(2).getAttribute('title'), /检查器|inspector/,
    'the third OR row is the next empty slot');

  /* The AND node was just created (selected + centered): clear selection and pan it
     far off-center so the locate click is the only reason it becomes selected/centered. */
  await page.evaluate(() => {
    window.App.kit.clearSelection();
    window.App.kit.zoomBy(0.5);
  });
  const blank = await blankPoint();
  assert.ok(blank, 'no blank stage point available for panning');
  await page.mouse.move(blank.x, blank.y);
  await page.mouse.down();
  await page.mouse.move(blank.x - 320, blank.y + 190, { steps: 10 });
  await page.mouse.up();
  await page.evaluate(() => window.App.kit.clearSelection());
  assert.deepEqual(await page.evaluate(() => window.App.kit.getSelection()), { kind: null, id: null });
  const offCenter = await page.evaluate(id => {
    const stage = document.getElementById('cvStage').getBoundingClientRect();
    const rect = document.querySelector(`[data-node-id="${id}"]`).getBoundingClientRect();
    return Math.hypot(
      rect.left + rect.width / 2 - (stage.left + stage.width / 2),
      rect.top + rect.height / 2 - (stage.top + stage.height / 2));
  }, andId);
  assert.ok(offCenter > 120, `AND node was not moved off-center before locate: ${offCenter}px`);

  await openIssuesPanel();
  await page.locator(`#cvIssues [data-issue-node="${andId}"]`).first().click();
  assert.deepEqual(await page.evaluate(() => window.App.kit.getSelection()), { kind: 'node', id: andId });
  const centered = await page.evaluate(id => {
    const stage = document.getElementById('cvStage').getBoundingClientRect();
    const rect = document.querySelector(`[data-node-id="${id}"]`).getBoundingClientRect();
    return Math.abs(rect.left + rect.width / 2 - (stage.left + stage.width / 2)) < 60 &&
      Math.abs(rect.top + rect.height / 2 - (stage.top + stage.height / 2)) < 60;
  }, andId);
  assert.equal(centered, true, 'node issue locate did not center the node');

  await page.locator('[data-lib-kind="logic-not"]').click();
  const notId = await page.evaluate(known => {
    const node = window.App.graph.nodes.find(item =>
      item.type === 'logic' && item.operator === 'not' && known.indexOf(item.id) < 0);
    return node && node.id;
  }, before.nodes.map(node => node.id));
  assert.ok(notId, 'NOT node was not created');
  assert.equal(await page.locator(`#cvNodes [data-node-id="${notId}"] .rtc-slot`).count(), 1,
    'NOT must render exactly one input row');

  await page.evaluate(id => {
    window.App.change('behavior', tree => {
      window.RTGraph.addEdge(tree.root, {
        source: 'cond-text', source_port: 'out', target: id, target_port: 'in_1', condition: null
      });
      window.RTGraph.addEdge(tree.root, {
        source: 'cond-peak', source_port: 'out', target: id, target_port: 'in_2', condition: null
      });
    });
  }, notId);
  await page.waitForFunction(id => window.App.issues.some(issue =>
    issue.node_id === id && issue.code === 'logic_slot_count' && /当前 2 个/.test(issue.zh)), notId);
  assert.equal(await page.locator(`#cvNodes [data-node-id="${notId}"] .rtc-slot`).count(), 1,
    'NOT with two incoming edges still renders a single slot row');
});

test('7.4-09-question-target-rows', async () => {
  await openCanvas();
  await dragPort('cond-text', 'out', { node: 'capability', port: 'in_2' });
  await page.waitForFunction(() => window.App.graph.edges.some(edge =>
    edge.target === 'capability' && edge.target_port === 'in_2'));

  assert.equal(await page.locator('#cvNodes [data-node-id="capability"] .rtc-slot').count(), 3);
  assert.equal(await page.locator('#cvNodes [data-node-id="capability"] .rtc-slots-note').count(), 0,
    'the OR explanation note is still rendered on the card');
  const rows = page.locator('#cvNodes [data-node-id="capability"] .rtc-slot');
  assert.match(await rows.nth(0).innerText(), /开始/);
  assert.match(await rows.nth(0).innerText(), /in_1/);
  assert.equal((await rows.nth(0).locator('[data-cond-edge]').textContent()).trim(), '= true');
  assert.match(await rows.nth(1).innerText(), /文本条件/);
  assert.equal((await rows.nth(1).locator('[data-cond-edge]').textContent()).trim(), '= true');

  await rows.nth(1).locator('[data-cond-edge]').click();
  await waitCondPop(await rows.nth(1).locator('[data-cond-edge]').getAttribute('data-cond-edge'));
  await page.locator('#cvCondPop [data-cond-field="bool"]').selectOption('false');
  await page.locator('#cvCondPop [data-act="cond-confirm"]').click();
  await page.waitForFunction(() => {
    const row = document.querySelectorAll('#cvNodes [data-node-id="capability"] .rtc-slot')[1];
    return row && row.querySelector('[data-cond-edge]').textContent.trim() === '= false';
  });

  await dragPort('cond-text', 'out', { node: 'strong-target', port: 'in_2' });
  await page.waitForFunction(() => window.App.graph.edges.some(edge =>
    edge.target === 'strong-target' && edge.target_port === 'in_2'));
  assert.equal(await page.locator('#cvNodes [data-node-id="strong-target"] .rtc-slot').count(), 3);
  assert.equal(await page.locator('#cvNodes [data-node-id="strong-target"] .rtc-slots-note').count(), 0,
    'the OR explanation note is still rendered on the target card');
  const targetRows = page.locator('#cvNodes [data-node-id="strong-target"] .rtc-slot');
  assert.match(await targetRows.nth(0).innerText(), /单选题|置信度/);
  assert.match(await targetRows.nth(1).innerText(), /文本条件/);
});

test('review-chinese-node-names', async () => {
  await openCanvas();
  const names = await page.evaluate(() => {
    const title = id => {
      const card = document.querySelector('#cvNodes [data-node-id="' + id + '"] .rtc-title');
      return card ? card.textContent.trim() : null;
    };
    const firstSlot = document.querySelector('#cvNodes [data-node-id="capability"] .rtc-slot');
    return {
      capability: title('capability'),
      domain: title('domain'),
      clarify: title('clarify'),
      slotSource: firstSlot ? firstSlot.innerText : '',
      idTooltip: document.querySelector('#cvNodes [data-node-id="capability"] .rtc-title').getAttribute('title'),
    };
  });
  assert.equal(names.capability, '单选题', 'choice question card title is not Chinese');
  assert.equal(names.domain, '是否问题-1', 'first noul card title is not Chinese');
  assert.equal(names.clarify, '是否问题-2', 'second noul card title is not Chinese');
  assert.match(names.slotSource, /开始/, 'slot source label is not Chinese');
  assert.equal(names.idTooltip, 'capability', 'raw node id is not kept in the tooltip');

  await clickNode('capability');
  await page.waitForFunction(() =>
    document.getElementById('cvInspectorSub').textContent.trim() === '单选题');
});

test('option-delete-cascades-edges', async () => {
  await openCanvas();
  await clickNode('capability');
  await waitInspectorBranch('node', 'capability');
  await page.locator('#cvInspectorBody [data-opt-row="strong"] [data-act="opt-delete"]').click();
  await page.waitForFunction(() => !document.getElementById('cvDeleteModal').hidden, null, { timeout: 3000 });
  assert.equal(await page.locator('#cvDeleteBody li').count(), 3);
  const dialog = await page.locator('#cvDeleteBody').innerText();
  assert.match(dialog, /selected\.strong/);
  assert.match(dialog, /prob\.strong/);
  assert.match(dialog, /模型目标-3/);
  assert.match(dialog, /与门-2/);
  await page.locator('[data-act="delete-confirm"]').click();
  await page.waitForFunction(() => !window.App.graph.nodes.find(node => node.id === 'capability')
    .options.some(option => option.key === 'strong'));
  assert.equal(await page.locator('#cvNodes [data-node-id="capability"] [data-optkey="strong"]').count(), 0);
  assert.deepEqual(await danglingEdgeIds(), []);
  assert.equal((await issueCodes()).includes('edge_port_missing'), false);
  assert.equal(await page.evaluate(() => window.App.graph.edges.some(edge =>
    edge.source === 'capability' && (edge.source_port === 'selected.strong' || edge.source_port === 'prob.strong'))), false);

  await page.locator('#cvUndo').click();
  await page.waitForFunction(() => window.App.graph.nodes.find(node => node.id === 'capability')
    .options.some(option => option.key === 'strong'));
  const restored = await appSnapshot();
  const restoredEdges = restored.edges.filter(edge => edge.source === 'capability' &&
    (edge.source_port === 'selected.strong' || edge.source_port === 'prob.strong'));
  assert.equal(restoredEdges.length, 3, 'one undo did not restore all option edges');
  assert.equal(restoredEdges.find(edge => edge.source_port === 'prob.strong').condition.value, 0.65);
  assert.equal(restoredEdges.find(edge => edge.source_port === 'selected.strong').condition, null);
  assert.deepEqual(await danglingEdgeIds(), []);
  assert.equal(restored.kind, 'clean');
});

test('Task 11 · option key rename syncs selected/prob source ports and conflicts stay in the editor', async () => {
  await openCanvas();
  await clickNode('capability');
  await waitInspectorBranch('node', 'capability');
  const historyBefore = (await appSnapshot()).history;

  await page.locator('#cvInspectorBody [data-opt-row="easy"] [data-opt-field="key"]').fill('strong');
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await page.waitForFunction(() => {
    const error = document.querySelector('#cvInspectorBody [data-ins-error]');
    return error && !error.hidden && error.textContent.trim().length > 0;
  });
  const conflict = (await page.locator('#cvInspectorBody [data-ins-error]').textContent()).trim();
  assert.match(conflict, /已存在|already exists/i);
  assert.match(conflict, /strong/, 'conflict message does not name the blocking option key');
  let snapshot = await appSnapshot();
  assert.deepEqual(snapshot.history, historyBefore, 'conflicting rename pushed history');
  assert.equal(await page.evaluate(() => !!window.RTGraph.findNode(window.App.graph, 'capability')
    .options.find(option => option.key === 'easy')), true);
  assert.equal(snapshot.edges.find(edge => edge.id === 'e-selected-easy-light').source_port, 'selected.easy');

  await page.locator('#cvInspectorBody [data-opt-row="easy"] [data-opt-field="key"]').fill('light');
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await page.waitForFunction(() => window.App.graph.edges.some(edge => edge.source_port === 'selected.light'));
  snapshot = await appSnapshot();
  assert.equal(snapshot.edges.find(edge => edge.id === 'e-selected-easy-light').source_port, 'selected.light');
  assert.equal(snapshot.edges.find(edge => edge.id === 'e-selected-easy-light').condition, null);
  const ports = await page.evaluate(() => window.RTGraph.portsOf(
    window.RTGraph.findNode(window.App.graph, 'capability'), window.App.graph).map(port => port.id));
  assert.ok(ports.includes('selected.light') && ports.includes('prob.light'));
  assert.equal(ports.includes('selected.easy'), false);
  assert.equal(await page.locator('#cvNodes [data-node-id="capability"] [data-optkey="light"]').count(), 1);

  await page.locator('#cvUndo').click();
  await page.waitForFunction(() => window.App.graph.edges.some(edge => edge.source_port === 'selected.easy'));
  snapshot = await appSnapshot();
  assert.equal(await page.evaluate(() => window.RTGraph.findNode(window.App.graph, 'capability')
    .options.some(option => option.key === 'easy')), true);
  assert.equal(snapshot.edges.some(edge => edge.source_port === 'selected.light'), false);
});

test('Task 11 · swapping two option keys in one save renames every row and follows source ports', async () => {
  await openCanvas();
  await clickNode('capability');
  await waitInspectorBranch('node', 'capability');

  /* easy→strong and strong→light in the same save: every final key is unique, but
     row 1's new key is still held by row 2 until it moves. Two-phase rename must win. */
  await page.locator('#cvInspectorBody [data-opt-row="easy"] [data-opt-field="key"]').fill('strong');
  await page.locator('#cvInspectorBody [data-opt-row="strong"] [data-opt-field="key"]').fill('light');
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await page.waitForFunction(() => {
    const node = window.RTGraph.findNode(window.App.graph, 'capability');
    return node && node.options[0].key === 'strong' && node.options[2].key === 'light';
  }, null, { timeout: 4000 });
  assert.equal(await page.locator('#cvInspectorBody [data-ins-error]').isHidden(), true,
    'valid two-key swap was rejected');

  const snapshot = await appSnapshot();
  assert.equal(snapshot.kind, 'behavior');
  assert.equal(JSON.stringify(snapshot).includes('__opt_tmp'), false,
    'temporary rename keys leaked into the graph');
  const edgeById = Object.fromEntries(snapshot.edges.map(edge => [edge.id, edge]));
  assert.equal(edgeById['e-selected-easy-light'].source_port, 'selected.strong',
    'renamed easy row did not follow to selected.strong');
  assert.equal(edgeById['e-cap-strong-and-power'].source_port, 'prob.light',
    'renamed strong row did not follow to prob.light');
  assert.equal(edgeById['e-cap-strong-and-power'].condition.value, 0.65,
    'swap changed an unrelated condition');
  assert.equal(edgeById['e-cap-strong-not'].source_port, 'prob.light');
  assert.equal(edgeById['e-selected-strong-strong'].source_port, 'selected.light');
  assert.equal(edgeById['e-selected-standard-standard'].source_port, 'selected.standard');

  const options = await page.evaluate(() => JSON.parse(JSON.stringify(
    window.RTGraph.findNode(window.App.graph, 'capability').options)));
  assert.deepEqual(options.map(option => option.key), ['strong', 'standard', 'light']);
  assert.equal(options[0].label, '轻量模型', 'label did not stay with the renamed easy row');
  assert.equal(options[2].label, '增强模型', 'label did not stay with the renamed strong row');
  const ports = await page.evaluate(() => window.RTGraph.portsOf(
    window.RTGraph.findNode(window.App.graph, 'capability'), window.App.graph).map(port => port.id));
  assert.ok(ports.includes('selected.strong') && ports.includes('prob.strong'));
  assert.ok(ports.includes('selected.light') && ports.includes('prob.light'));
  assert.equal(ports.includes('selected.easy'), false);
  assert.equal(await page.locator('#cvNodes [data-node-id="capability"] [data-optkey="strong"]').count(), 1);
  assert.equal(await page.locator('#cvNodes [data-node-id="capability"] [data-optkey="light"]').count(), 1);
  assert.deepEqual((await appSnapshot()).history, { undo: true, redo: false },
    'two-key swap must be exactly one history entry');

  await page.locator('#cvUndo').click();
  await page.waitForFunction(() => {
    const node = window.RTGraph.findNode(window.App.graph, 'capability');
    return node && node.options[0].key === 'easy' && node.options[2].key === 'strong';
  });
  const restored = await appSnapshot();
  assert.equal(restored.edges.find(edge => edge.id === 'e-selected-easy-light').source_port, 'selected.easy');
  assert.equal(restored.edges.find(edge => edge.id === 'e-selected-strong-strong').source_port, 'selected.strong');
  assert.equal(restored.edges.find(edge => edge.id === 'e-cap-strong-and-power').source_port, 'prob.strong');
  assert.deepEqual(restored.history, { undo: false, redo: true });
});

test('Task 11 · inspector save and delete bind to the rendered branch, not a silently moved kit selection', async () => {
  await openCanvas();
  await clickNode('capability');
  await waitInspectorBranch('node', 'capability');
  /* Move the kit selection without re-rendering the inspector: the rendered form
     still belongs to capability, so save/delete must use capability's id. */
  await page.evaluate(() => window.App.kit.select('node', 'cond-text', true));
  assert.deepEqual(await page.evaluate(() => window.App.kit.getSelection()), { kind: 'node', id: 'cond-text' });
  const condTextBefore = await page.evaluate(() => JSON.parse(JSON.stringify(
    window.RTGraph.findNode(window.App.graph, 'cond-text'))));
  const draft = '只写入渲染时绑定的 capability 节点。';
  await page.locator('#cvInspectorBody [data-field="instructions"]').fill(draft);
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await page.waitForFunction(value => {
    const node = window.RTGraph.findNode(window.App.graph, 'capability');
    return node && node.instructions.indexOf(value) >= 0;
  }, draft);
  assert.deepEqual(await page.evaluate(() => JSON.parse(JSON.stringify(
    window.RTGraph.findNode(window.App.graph, 'cond-text')))), condTextBefore,
  'inspector save wrote to the silently moved kit selection');
  await page.locator('#cvUndo').click();
  await page.waitForFunction(() => window.RTGraph.findNode(window.App.graph, 'capability')
    .instructions.indexOf('只写入') < 0);

  /* Option delete resolves the node id from the rendered branch as well. */
  await clickNode('capability');
  await waitInspectorBranch('node', 'capability');
  await page.evaluate(() => window.App.kit.select('node', 'cond-text', true));
  await page.locator('#cvInspectorBody [data-opt-row="strong"] [data-act="opt-delete"]').click();
  await page.waitForFunction(() => !document.getElementById('cvDeleteModal').hidden, null, { timeout: 3000 });
  const optionDialog = await page.locator('#cvDeleteBody').innerText();
  assert.match(optionDialog, /单选题/);
  assert.match(optionDialog, /selected\.strong/);
  assert.match(optionDialog, /prob\.strong/);
  await page.locator('[data-act="delete-cancel"]').click();

  /* Edge delete lists the rendered edge even when the kit selection points at another. */
  await clickEdge('e-cap-strong-and-power');
  await waitInspectorBranch('edge', 'e-cap-strong-and-power');
  await page.evaluate(() => window.App.kit.select('edge', 'e-selected-easy-light', true));
  await page.locator('#cvInspectorBody [data-act="ins-delete-edge"]').click();
  await page.waitForFunction(() => !document.getElementById('cvDeleteModal').hidden, null, { timeout: 3000 });
  const edgeDialog = await page.locator('#cvDeleteBody').innerText();
  assert.match(edgeDialog, /与门-2/);
  assert.match(edgeDialog, /in_1/);
  await page.locator('[data-act="delete-cancel"]').click();
  assert.equal(await page.locator('#cvDeleteModal').isHidden(), true);
  assert.equal(await page.evaluate(() => window.App.graph.edges
    .filter(edge => edge.id === 'e-cap-strong-and-power').length), 1);
});

test('Task 11 · question and condition inspectors edit instructions, options, keywords, periods and criteria', async () => {
  await openCanvas();
  await clickNode('capability');
  await waitInspectorBranch('node', 'capability');
  const original = await page.evaluate(() => JSON.parse(JSON.stringify(
    window.RTGraph.findNode(window.App.graph, 'capability'))));
  const instructions = '这条请求需要哪个能力档位？请结合复杂度与输出长度判断。';
  await page.locator('#cvInspectorBody [data-field="instructions"]').fill(instructions);
  await page.locator('#cvInspectorBody [data-opt-row="easy"] [data-opt-field="label"]').fill('轻量档');
  await page.locator('#cvInspectorBody [data-act="opt-add"]').click();
  const newRow = page.locator('#cvInspectorBody [data-opt-row]').last();
  await newRow.locator('[data-opt-field="key"]').fill('medium');
  await newRow.locator('[data-opt-field="label"]').fill('中档模型');
  await newRow.locator('[data-opt-field="capability"]').fill('中等复杂度写作与总结。');
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await page.waitForFunction(() => {
    const node = window.RTGraph.findNode(window.App.graph, 'capability');
    return node && node.options.length === 4 && node.instructions.indexOf('复杂度') >= 0;
  });
  let snapshot = await appSnapshot();
  assert.equal(snapshot.kind, 'behavior');
  assert.equal(await page.evaluate(() => !!window.RTGraph.findNode(window.App.graph, 'capability')
    .options.find(option => option.key === 'easy')), true);
  const saved = await page.evaluate(() => JSON.parse(JSON.stringify(
    window.RTGraph.findNode(window.App.graph, 'capability'))));
  assert.equal(saved.options.find(option => option.key === 'easy').label, '轻量档');
  assert.deepEqual(saved.options.find(option => option.key === 'medium'),
    { key: 'medium', label: '中档模型', capability: '中等复杂度写作与总结。' });
  await page.locator('#cvUndo').click();
  await page.waitForFunction(() => window.RTGraph.findNode(window.App.graph, 'capability').options.length === 3);
  snapshot = await appSnapshot();
  const undone = await page.evaluate(() => JSON.parse(JSON.stringify(
    window.RTGraph.findNode(window.App.graph, 'capability'))));
  assert.equal(undone.instructions, original.instructions);
  assert.equal(undone.options.find(option => option.key === 'easy').label, original.options[0].label);

  await clickNode('cond-text');
  await waitInspectorBranch('node', 'cond-text');
  await page.locator('#cvInspectorBody [data-field="keywords"]').fill('抢修, 停电, 电梯, 故障');
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await page.waitForFunction(() => {
    const node = window.RTGraph.findNode(window.App.graph, 'cond-text');
    return node && node.contains_any.indexOf('电梯') >= 0;
  });
  assert.deepEqual(await page.evaluate(() => window.RTGraph.findNode(window.App.graph, 'cond-text').contains_any),
    ['抢修', '停电', '电梯', '故障']);
  assert.equal(await page.locator('#cvNodes [data-node-id="cond-text"] [data-kw="电梯"]').count(), 1);
  await page.locator('#cvUndo').click();
  await page.waitForFunction(() => window.RTGraph.findNode(window.App.graph, 'cond-text').contains_any.indexOf('电梯') < 0);

  await clickNode('cond-peak');
  await waitInspectorBranch('node', 'cond-peak');
  await page.locator('#cvInspectorBody [data-field="periods"] input[value="normal"]').check();
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await page.waitForFunction(() => {
    const node = window.RTGraph.findNode(window.App.graph, 'cond-peak');
    return node && node.periods.indexOf('normal') >= 0;
  });
  assert.deepEqual(await page.evaluate(() => window.RTGraph.findNode(window.App.graph, 'cond-peak').periods),
    ['peak', 'normal']);
  assert.equal(await page.locator('#cvNodes [data-node-id="cond-peak"] [data-period="normal"]').count(), 1);
  await page.locator('#cvUndo').click();
  await page.waitForFunction(() => window.RTGraph.findNode(window.App.graph, 'cond-peak').periods.length === 1);

  const beforeScore = await appSnapshot();
  await page.locator('[data-lib-kind="question-score"]').click();
  const scoreIds = await page.evaluate(known => window.App.graph.nodes
    .filter(node => node.type === 'question' && node.question_type === 'score' && known.indexOf(node.id) < 0)
    .map(node => node.id), beforeScore.nodes.map(node => node.id));
  assert.equal(scoreIds.length, 1);
  await waitInspectorBranch('node', scoreIds[0]);
  await page.locator('#cvInspectorBody [data-field="criteria"]').fill('2, 5, 9');
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await page.waitForFunction(id => {
    const node = window.RTGraph.findNode(window.App.graph, id);
    return node && JSON.stringify(node.criteria) === '[2,5,9]';
  }, scoreIds[0]);
});

test('Task 11 · logic and target inspectors edit operator, models, reasoning, priority and strategy with guards', async () => {
  await openCanvas();
  await clickNode('and-power');
  await waitInspectorBranch('node', 'and-power');
  const noopHistory = (await appSnapshot()).history;
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await page.waitForTimeout(50);
  assert.deepEqual((await appSnapshot()).history, noopHistory, 'no-op node save pushed history');
  await page.locator('#cvInspectorBody [data-field="operator"]').selectOption('or');
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await page.waitForFunction(() => window.RTGraph.findNode(window.App.graph, 'and-power').operator === 'or');
  assert.equal(await page.locator('#cvNodes [data-node-id="and-power"]').getAttribute('data-operator'), 'or');
  await page.locator('#cvUndo').click();
  await page.waitForFunction(() => window.RTGraph.findNode(window.App.graph, 'and-power').operator === 'and');

  await clickNode('strong-target');
  await waitInspectorBranch('node', 'strong-target');
  const modelIds = await page.evaluate(() => Array.from(
    document.querySelectorAll('#cvInspectorBody [data-model-field="model"] option')).map(option => option.value));
  assert.ok(modelIds.includes('deepseek-v3') && modelIds.includes('qwen3-32b') && modelIds.includes('qwen3-8b'),
    'model options do not come from /admin/models: ' + modelIds.join(','));
  const reasoning = await page.evaluate(() => Array.from(
    document.querySelectorAll('#cvInspectorBody [data-model-field="reasoning"] option')).map(option => option.value));
  assert.ok(reasoning.includes('low') && reasoning.includes('medium') && reasoning.includes('high'),
    'reasoning options do not come from the model profile: ' + reasoning.join(','));

  const historyBefore = (await appSnapshot()).history;
  await page.locator('#cvInspectorBody [data-field="priority"]').fill('1');
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await page.waitForFunction(() => {
    const error = document.querySelector('#cvInspectorBody [data-ins-error]');
    return error && !error.hidden && error.textContent.trim().length > 0;
  });
  assert.match((await page.locator('#cvInspectorBody [data-ins-error]').textContent()).trim(), /priority/i);
  assert.deepEqual((await appSnapshot()).history, historyBefore, 'duplicate priority pushed history');
  assert.equal(await page.evaluate(() => window.RTGraph.findNode(window.App.graph, 'strong-target').priority), 3);

  await page.locator('#cvInspectorBody [data-field="priority"]').fill('7');
  await page.locator('#cvInspectorBody [data-model-field="model"]').first().selectOption('deepseek-v3');
  await page.locator('#cvInspectorBody [data-model-field="reasoning"]').first().selectOption('high');
  await page.locator('#cvInspectorBody [data-field="strategy"]').selectOption('least_inflight');
  await page.locator('#cvInspectorBody [data-field="capability"]').fill('强推理通用模型：复杂分析。');
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await page.waitForFunction(() => {
    const node = window.RTGraph.findNode(window.App.graph, 'strong-target');
    return node && node.priority === 7 && node.models.length === 1 &&
      node.models[0].model === 'deepseek-v3' && node.models[0].reasoning === 'high';
  });
  const target = await page.evaluate(() => JSON.parse(JSON.stringify(
    window.RTGraph.findNode(window.App.graph, 'strong-target'))));
  assert.equal(target.strategy, 'least_inflight');
  assert.equal(target.capability, '强推理通用模型：复杂分析。');
  await page.locator('#cvUndo').click();
  await page.waitForFunction(() => {
    const node = window.RTGraph.findNode(window.App.graph, 'strong-target');
    return node && node.priority === 3 && node.models[0].model === 'qwen3-32b' && node.models[0].reasoning === 'medium';
  });
});

test('Task 11 · graph settings edit name/timezone/sticky/fallbacks, show limits and reject an invalid timezone', async () => {
  await openCanvas();
  await waitInspectorBranch('graph', 'root');
  assert.equal(await page.locator('#cvInspectorBody [data-field="name"]').inputValue(), BASELINE.name);
  assert.equal(await page.locator('#cvInspectorBody [data-field="timezone"]').inputValue(), BASELINE.timezone);
  assert.equal(await page.locator('#cvInspectorBody [data-field="sticky"]').isChecked(), true);
  assert.equal(await page.locator('#cvInspectorBody [data-field="stickyTtl"]').inputValue(), '1800');
  assert.equal(await page.locator('#cvInspectorBody [data-field="otherwise"]').inputValue(), 'standard-target');
  assert.equal(await page.locator('#cvInspectorBody [data-field="errorFallback"]').inputValue(), 'light-target');
  const limitRows = await page.evaluate(() => {
    const read = name => {
      const row = document.querySelector('#cvInspectorBody [data-limit="' + name + '"] b');
      return row ? row.textContent.trim() : null;
    };
    const graph = window.App.graph;
    return {
      nodes: read('nodes'), edges: read('edges'),
      questions: read('questions'), waves: read('waves'),
      counts: {
        nodes: graph.nodes.length,
        edges: graph.edges.length,
        questions: graph.nodes.filter(node => node.type === 'question').length
      }
    };
  });
  assert.deepEqual(limitRows.counts, { nodes: EXAMPLE_NODES, edges: EXAMPLE_EDGES, questions: 3 });
  assert.equal(limitRows.nodes, EXAMPLE_NODES + ' / 200');
  assert.equal(limitRows.edges, '15 / 400');
  assert.equal(limitRows.questions, '3 / 32');
  assert.equal(limitRows.waves, '≤ 8');

  const noopHistory = (await appSnapshot()).history;
  await page.locator('#cvInspectorBody [data-act="ins-save-graph"]').click();
  await page.waitForTimeout(50);
  assert.deepEqual((await appSnapshot()).history, noopHistory, 'no-op graph save pushed history');

  const historyBefore = (await appSnapshot()).history;
  await page.locator('#cvInspectorBody [data-field="timezone"]').fill('Mars/Phobos');
  await page.locator('#cvInspectorBody [data-act="ins-save-graph"]').click();
  await page.waitForFunction(() => {
    const error = document.querySelector('#cvInspectorBody [data-ins-error]');
    return error && !error.hidden && error.textContent.trim().length > 0;
  });
  assert.match((await page.locator('#cvInspectorBody [data-ins-error]').textContent()).trim(), /时区|timezone/i);
  assert.equal(await page.evaluate(() => window.App.tree.timezone), BASELINE.timezone);
  assert.deepEqual((await appSnapshot()).history, historyBefore);

  await page.locator('#cvInspectorBody [data-field="name"]').fill('电力优先决策组 · v2');
  await page.locator('#cvInspectorBody [data-field="timezone"]').fill('UTC');
  await page.locator('#cvInspectorBody [data-field="sticky"]').uncheck();
  await page.locator('#cvInspectorBody [data-field="stickyTtl"]').fill('900');
  await page.locator('#cvInspectorBody [data-field="otherwise"]').selectOption('power-target');
  await page.locator('#cvInspectorBody [data-field="errorFallback"]').selectOption('power-target');
  await page.locator('#cvInspectorBody [data-act="ins-save-graph"]').click();
  await page.waitForFunction(() => window.App.tree.name === '电力优先决策组 · v2' &&
    window.App.tree.timezone === 'UTC' && window.App.tree.sticky.enabled === false &&
    window.App.tree.sticky.ttl_seconds === 900 && window.App.graph.otherwise_target === 'power-target');
  const snapshot = await appSnapshot();
  assert.equal(snapshot.kind, 'behavior');
  assert.equal(await page.evaluate(() => window.App.graph.error_fallback), 'power-target');
  await page.locator('#cvUndo').click();
  await page.waitForFunction(expected => window.App.tree.name === expected.name &&
    window.App.tree.timezone === expected.timezone && window.App.tree.sticky.enabled === true &&
    window.App.tree.sticky.ttl_seconds === expected.sticky.ttl_seconds &&
    window.App.graph.otherwise_target === 'standard-target', BASELINE);
  assert.deepEqual((await appSnapshot()).history, { undo: false, redo: true });
});

test('Task 11 · Backspace inside editor inputs is safe, all overlays close with Esc and labels are locatable', async () => {
  await openCanvas();
  const before = await appSnapshot();
  await clickNode('capability');
  await waitInspectorBranch('node', 'capability');
  await page.locator('#cvInspectorBody [data-field="instructions"]').click();
  await page.keyboard.press('Backspace');
  await page.keyboard.press('Delete');
  await expectNever(() => !document.getElementById('cvDeleteModal').hidden,
    'Delete/Backspace in an inspector input opened the delete confirmation');
  assert.equal((await appSnapshot()).nodes.length, before.nodes.length);

  const chip = page.locator('#cvNodes [data-cond-edge="e-cap-strong-and-power"]');
  const chipAria = await chip.getAttribute('aria-label');
  assert.ok(chipAria && chipAria.includes('in_1'), 'condition chip has no locatable aria-label');
  await chip.click();
  await waitCondPop('e-cap-strong-and-power');
  const popAria = await page.evaluate(() => ({
    role: document.getElementById('cvCondPop').getAttribute('role'),
    label: document.getElementById('cvCondPop').getAttribute('aria-label')
  }));
  assert.equal(popAria.role, 'dialog');
  assert.equal(popAria.label, await page.evaluate(() => window.t('cv.condTitle')));
  assert.equal(await page.locator('#cvInspector').getAttribute('aria-label'),
    await page.evaluate(() => window.t('cv.inspector')));
  await page.locator('#cvCondPop [data-cond-field="value"]').click();
  await page.keyboard.press('Backspace');
  await page.keyboard.press('Delete');
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#cvCondPop').isHidden(), true);

  const point = await blankPoint();
  await dragPort('capability', 'prob.easy', point);
  await page.waitForFunction(() => !document.getElementById('cvQuick').hidden, null, { timeout: 3000 });
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#cvQuick').isHidden(), true);

  await clickNode('and-power');
  await page.keyboard.press('Delete');
  await page.waitForFunction(() => !document.getElementById('cvDeleteModal').hidden, null, { timeout: 3000 });
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#cvDeleteModal').isHidden(), true);

  await openIssuesPanel();
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#cvIssues').isHidden(), true);
});

test('Task 11 · boot unlocks the canvas before catalogues and late catalogue data does not wipe inspector edits', async () => {
  let release = () => {};
  const gate = new Promise(resolve => { release = resolve; });
  const hold = async route => { await gate; await route.continue(); };
  await page.route('**/admin/models', hold);
  await page.route('**/admin/time-periods', hold);
  try {
    await page.goto(routerBase + '/console/routing.html');
    await page.waitForSelector('#cvStage [data-node-id]', { timeout: 20000 });
    await page.waitForFunction(() => window.App && window.App.booted && window.App.tree);
    assert.equal(await page.evaluate(() => window.App.models.length), 0,
      'catalogue resolved before the test released it');

    /* Palette creation must work while models/periods are still pending. */
    const before = await appSnapshot();
    await page.locator('[data-lib-kind="logic-not"]').click();
    await page.waitForFunction(count => window.App.graph.nodes.length === count + 1, before.nodes.length,
      { timeout: 4000 });
    assert.equal((await appSnapshot()).nodes.length, before.nodes.length + 1,
      'palette creation was blocked by the pending catalogues');

    /* Type, blur, then let the catalogues settle: the unsaved draft must survive. */
    await page.evaluate(() => window.App.kit.select('node', 'capability'));
    await waitInspectorBranch('node', 'capability');
    const draft = '目录晚到不能清掉这段未保存输入';
    await page.locator('#cvInspectorBody [data-field="title"]').fill(draft);
    await page.locator('#cvInspectorBody [data-field="title"]').evaluate(element => element.blur());
    release();
    await page.waitForFunction(() => window.App.models.length > 0 && window.App.periods.length > 0);
    await page.waitForTimeout(250);
    assert.equal(await page.locator('#cvInspectorBody [data-field="title"]').inputValue(), draft,
      'late catalogue refresh wiped an unsaved inspector edit');
    assert.equal(await page.evaluate(() => window.App.tree.name), BASELINE.name);
  } finally {
    release();
    await page.unroute('**/admin/models');
    await page.unroute('**/admin/time-periods');
  }
});

test('Task 11 · admin API timeout rejects a hung request with a clear localized error', async () => {
  await openCanvas();
  let held = null;
  const hung = async route => { held = route; };
  const timeoutProbe = url => url.pathname === '/admin/time-periods' && url.searchParams.has('timeout-probe');
  await page.route(timeoutProbe, hung);
  try {
    const result = await page.evaluate(async () => {
      const path = '/admin/time-periods?timeout-probe=1';
      try {
        await api.req('GET', path, undefined, { timeoutMs: 1000 });
        return { threw: false };
      } catch (error) {
        return {
          threw: true,
          timeout: error.timeout === true,
          status: error.status,
          message: error.message,
          expected: tf('api.timeout', { seconds: 1, path: path })
        };
      }
    });
    assert.equal(result.threw, true, 'hung admin request did not reject');
    assert.equal(result.timeout, true, 'timeout error is not marked: ' + result.message);
    assert.equal(result.status, 0);
    assert.equal(result.message, result.expected, 'timeout error message is not the localized clear message');
  } finally {
    if (held) { try { await held.abort(); } catch { /* already gone */ } }
    await page.unroute(timeoutProbe);
  }
});

/* ── Task 12 · 保存 / 启用 / dirty / 导入导出 / 校验定位 ────────────── */

/* 每个 Task 12 用例都从同一份服务端基线开始：power-priority 恢复为示例图并启用。
   管理面没有 DELETE /admin/trees/{id}，用例新建的组留在临时库里但保持未启用。 */
async function ensureBaselineTree() {
  const restored = await api('PUT', '/admin/trees/' + TREE_ID, {
    id: TREE_ID,
    name: BASELINE.name,
    timezone: BASELINE.timezone,
    sticky: BASELINE.sticky,
    root: EXAMPLE
  });
  if (!restored.tree.enabled) await api('POST', '/admin/trees/' + TREE_ID + '/enable');
  return (await api('GET', '/admin/trees/' + TREE_ID)).tree;
}

async function serverTree(id) {
  return (await api('GET', '/admin/trees/' + encodeURIComponent(id))).tree;
}

async function dirtyBadge() {
  return page.evaluate(() => {
    const badge = document.getElementById('cvDirty');
    return {
      hidden: badge.hidden,
      kind: badge.dataset.kind,
      text: badge.textContent.trim(),
      appKind: window.App.kind(),
      cleanHidden: document.getElementById('cvClean').hidden
    };
  });
}

async function dragNodeBy(nodeId, dx, dy) {
  const before = await page.evaluate(id => {
    const node = window.App.graph.nodes.find(item => item.id === id);
    return { x: node.position.x, y: node.position.y };
  }, nodeId);
  const box = await page.locator(`[data-node-id="${nodeId}"]`).first().boundingBox();
  assert.ok(box, `node not visible: ${nodeId}`);
  const start = { x: box.x + box.width / 2, y: box.y + 11 };
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await page.mouse.move(start.x + dx, start.y + dy, { steps: 8 });
  await page.mouse.up();
  await page.waitForFunction(([id, previous]) => {
    const node = window.App.graph.nodes.find(item => item.id === id);
    return node && (node.position.x !== previous.x || node.position.y !== previous.y);
  }, [nodeId, before], { timeout: 4000 });
  return page.evaluate(id => {
    const node = window.App.graph.nodes.find(item => item.id === id);
    return { x: node.position.x, y: node.position.y };
  }, nodeId);
}

async function saveNow() {
  await page.locator('#cvSave').click();
  await page.waitForFunction(() => window.App.changeKind === 'clean' && !!window.App.saved,
    null, { timeout: 8000 });
}

async function beforeUnloadBlocked() {
  return page.evaluate(() => {
    const event = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(event);
    return event.defaultPrevented;
  });
}

async function exportViaUi() {
  const downloadPromise = page.waitForEvent('download', { timeout: 10000 });
  await page.locator('#cvExport').click();
  const download = await downloadPromise;
  const file = await download.path();
  assert.ok(file, 'browser download has no local path');
  return { name: download.suggestedFilename(), text: fs.readFileSync(file, 'utf8') };
}

async function setImportPayload(payload, name) {
  await page.locator('#cvImportFile').setInputFiles({
    name: name || 'graph.sr-routing-graph-v3.json',
    mimeType: 'application/json',
    buffer: Buffer.from(typeof payload === 'string' ? payload : JSON.stringify(payload))
  });
}

async function waitConfirmDialog() {
  await page.waitForSelector('#cvConfirmModal:not([hidden])', { timeout: 4000 });
  return page.locator('#cvConfirmBody').innerText();
}

async function waitToastIncludes(fragment, timeout) {
  await page.waitForFunction(value => {
    const host = document.getElementById('cvToast');
    return !!host && host.textContent.indexOf(value) >= 0;
  }, fragment, { timeout: timeout || 6000 });
}

test('Task 12 · moving a node saves layout-only: revision unchanged, coordinates persist, badge 仅布局', async () => {
  const baseline = await ensureBaselineTree();
  await openCanvas();
  const moved = await dragNodeBy('start', 66, 38);
  const badge = await dirtyBadge();
  assert.equal(badge.hidden, false);
  assert.equal(badge.kind, 'layout');
  assert.equal(badge.appKind, 'layout');
  assert.equal(badge.text, await page.evaluate(() => window.t('cv.dirtyLayout')));

  await saveNow();
  const saved = await page.evaluate(() => ({
    kind: window.App.kind(),
    revision: window.App.tree.tree_revision,
    savedRevision: window.App.saved.tree_revision,
    badgeHidden: document.getElementById('cvDirty').hidden
  }));
  assert.equal(saved.kind, 'clean');
  assert.equal(saved.revision, baseline.tree_revision, 'layout-only save changed the local revision');
  assert.equal(saved.savedRevision, baseline.tree_revision);
  assert.equal(saved.badgeHidden, true);

  const server = await serverTree(TREE_ID);
  assert.equal(server.tree_revision, baseline.tree_revision,
    'a position-only save changed the server tree_revision');
  const start = server.root.nodes.find(node => node.id === 'start');
  assert.ok(Math.abs(start.position.x - moved.x) < 0.01, 'saved X coordinate differs from the draft');
  assert.ok(Math.abs(start.position.y - moved.y) < 0.01, 'saved Y coordinate differs from the draft');
});

test('Task 12 · save clears dirty while the undo history stays usable', async () => {
  await ensureBaselineTree();
  await openCanvas();
  await page.evaluate(() => window.App.kit.clearSelection());
  await waitInspectorBranch('graph', 'root');
  await page.locator('#cvInspectorBody [data-field="name"]').fill('保存后仍可撤销');
  await page.locator('#cvInspectorBody [data-act="ins-save-graph"]').click();
  await page.waitForFunction(() => window.App.tree.name === '保存后仍可撤销' && window.App.kind() === 'display');
  const beforeSave = await dirtyBadge();
  assert.equal(beforeSave.hidden, false);
  assert.equal(beforeSave.text, await page.evaluate(() => window.t('cv.dirty')));

  await saveNow();
  const after = await page.evaluate(() => ({
    kind: window.App.kind(),
    canUndo: window.App.kit.canUndo(),
    canRedo: window.App.kit.canRedo(),
    revision: window.App.tree.tree_revision,
    savedRevision: window.App.saved.tree_revision,
    badgeHidden: document.getElementById('cvDirty').hidden
  }));
  assert.equal(after.kind, 'clean');
  assert.equal(after.canUndo, true, 'save cleared the undo history');
  assert.equal(after.canRedo, false);
  assert.equal(after.badgeHidden, true);
  assert.equal(after.revision, after.savedRevision);
  assert.equal((await serverTree(TREE_ID)).name, '保存后仍可撤销');

  await page.locator('#cvUndo').click();
  await page.waitForFunction(expected => window.App.tree.name === expected, BASELINE.name);
  assert.equal((await dirtyBadge()).kind, 'display', 'undoing a save did not mark the draft dirty again');
});

test('Task 12 · timezone/sticky are behavior while title/capability/name are display changes', async () => {
  const baseline = await ensureBaselineTree();
  await openCanvas();
  await waitInspectorBranch('graph', 'root');
  await page.locator('#cvInspectorBody [data-field="timezone"]').fill('UTC');
  await page.locator('#cvInspectorBody [data-act="ins-save-graph"]').click();
  await page.waitForFunction(() => window.App.tree.timezone === 'UTC');
  let badge = await dirtyBadge();
  assert.equal(badge.kind, 'behavior', 'timezone change was not classified as behavior');
  assert.equal(badge.text, await page.evaluate(() => window.t('cv.dirty')));
  await page.locator('#cvUndo').click();
  await page.waitForFunction(expected => window.App.tree.timezone === expected && window.App.kind() === 'clean', BASELINE.timezone);

  await clickNode('capability');
  await waitInspectorBranch('node', 'capability');
  await page.locator('#cvInspectorBody [data-field="title"]').fill('能力分档（展示标题）');
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await page.waitForFunction(() =>
    window.App.graph.nodes.find(node => node.id === 'capability').title === '能力分档（展示标题）');
  assert.equal((await dirtyBadge()).kind, 'display', 'question title was not classified as display');

  await clickNode('strong-target');
  await waitInspectorBranch('node', 'strong-target');
  await page.locator('#cvInspectorBody [data-field="capability"]').fill('强推理（展示说明）');
  await page.locator('#cvInspectorBody [data-act="ins-save-node"]').click();
  await page.waitForFunction(() =>
    window.App.graph.nodes.find(node => node.id === 'strong-target').capability === '强推理（展示说明）');
  assert.equal((await dirtyBadge()).kind, 'display', 'target capability was not classified as display');

  await page.evaluate(() => window.App.kit.clearSelection());
  await waitInspectorBranch('graph', 'root');
  await page.locator('#cvInspectorBody [data-field="name"]').fill('展示字段决策组');
  await page.locator('#cvInspectorBody [data-act="ins-save-graph"]').click();
  await page.waitForFunction(() => window.App.tree.name === '展示字段决策组');
  badge = await dirtyBadge();
  assert.equal(badge.kind, 'display', 'tree name was not classified as display');
  assert.equal(badge.text, await page.evaluate(() => window.t('cv.dirty')));

  await saveNow();
  const server = await serverTree(TREE_ID);
  assert.equal(server.tree_revision, baseline.tree_revision,
    'a display-only save changed the server tree_revision');
  assert.equal(server.name, '展示字段决策组');
  assert.equal(server.root.nodes.find(node => node.id === 'capability').title, '能力分档（展示标题）');
  assert.equal(server.root.nodes.find(node => node.id === 'strong-target').capability, '强推理（展示说明）');
});

test('Task 12 · unsaved switch-group asks one shared confirmation and beforeunload guards only dirty drafts', async () => {
  await ensureBaselineTree();
  const otherId = 'qa-switch-group';
  const existing = await api('GET', '/admin/trees');
  if (!existing.trees.some(tree => tree.id === otherId)) {
    await api('POST', '/admin/trees/' + TREE_ID + '/copy', { id: otherId, name: 'QA 切换组' });
  }
  await openCanvas();
  /* clean 草稿不得 preventDefault；dirty 草稿必须 preventDefault（且只在 dirty 时）。 */
  let blocked = await beforeUnloadBlocked();
  assert.equal(blocked, false, 'a clean tree blocked beforeunload');

  await dragNodeBy('start', 40, 30);
  blocked = await beforeUnloadBlocked();
  assert.equal(blocked, true, 'a dirty draft did not prevent the beforeunload default');

  const select = page.locator('#cvTreeSelect');
  await select.selectOption(otherId);
  const dialog = await waitConfirmDialog();
  assert.ok(dialog.includes(await page.evaluate(() => window.t('cv.confirmSwitchTitle'))),
    'switch confirmation is not the shared dirty dialog');
  await page.locator('#cvConfirmBody [data-act="confirm-cancel"]').click();
  await page.waitForFunction(() => document.getElementById('cvConfirmModal').hidden);
  assert.equal(await page.evaluate(() => window.App.treeId), TREE_ID);
  assert.equal(await select.inputValue(), TREE_ID);
  assert.equal((await dirtyBadge()).kind, 'layout');

  await select.selectOption(otherId);
  await waitConfirmDialog();
  await page.locator('#cvConfirmBody [data-act="confirm-discard"]').click();
  await page.waitForFunction(id => window.App.treeId === id && window.App.changeKind === 'clean',
    otherId, { timeout: 8000 });
  assert.equal(await page.evaluate(() => window.App.tree.name), 'QA 切换组');
  blocked = await beforeUnloadBlocked();
  assert.equal(blocked, false, 'the clean switched tree still prevented beforeunload');
  await ensureBaselineTree();
});

test('minimal-new-group-template', async () => {
  await ensureBaselineTree();
  await openCanvas();
  await page.locator('#cvNewTree').click();
  await page.waitForFunction(() => window.App.saved === null && window.App.kind() !== 'clean');
  const draft = await page.evaluate(() => ({
    id: window.App.tree.id,
    treeId: window.App.treeId,
    kind: window.App.kind(),
    types: window.App.graph.nodes.map(node => node.type).sort(),
    edges: window.App.graph.edges.map(edge => [edge.source, edge.source_port, edge.target, edge.target_port]),
    otherwise: window.App.graph.otherwise_target,
    errorFallback: window.App.graph.error_fallback,
    target: (window.App.graph.nodes.find(node => node.type === 'target') || {}).id,
    finite: window.App.graph.nodes.every(node =>
      window.RTGraph && node.position && Number.isFinite(node.position.x) && Number.isFinite(node.position.y))
  }));
  assert.equal(draft.kind, 'behavior');
  assert.match(draft.id, /^[a-z0-9]{12}$/, 'new tree id is not the 12-char UUID slice');
  assert.deepEqual(draft.types, ['start', 'target']);
  assert.equal(draft.edges.length, 1);
  assert.deepEqual(draft.edges[0], ['start', 'out', draft.target, 'in_1']);
  assert.equal(draft.otherwise, draft.target, 'otherwise fallback does not point at the target');
  assert.equal(draft.errorFallback, draft.target, 'error fallback does not point at the target');
  assert.equal(draft.finite, true, 'new tree nodes have no finite coordinates');

  await saveNow();
  const saved = await page.evaluate(() => ({
    id: window.App.treeId,
    savedId: window.App.saved.id,
    enabled: window.App.tree.enabled,
    kind: window.App.kind()
  }));
  assert.equal(saved.id, draft.id);
  assert.equal(saved.savedId, draft.id);
  assert.equal(saved.enabled, false);
  assert.equal(saved.kind, 'clean');
  const server = await serverTree(draft.id);
  assert.equal(server.enabled, false);
  assert.equal(server.root.nodes.length, 2);
  assert.equal(server.root.edges.length, 1);

  await page.locator('#cvEnable').click();
  await page.waitForFunction(() => window.App.tree.enabled === true, null, { timeout: 8000 });
  assert.equal((await page.locator('#cvEnableLabel').textContent()).trim(),
    await page.evaluate(() => window.t('cv.enabled')));
  const enabled = await serverTree(draft.id);
  assert.equal(enabled.enabled, true);
  const list = await api('GET', '/admin/trees');
  assert.equal(list.trees.find(tree => tree.id === draft.id).enabled, true);
  assert.equal(list.trees.find(tree => tree.id === TREE_ID).enabled, false);
  await ensureBaselineTree();
});

test('7.4-13-save-enable-unsaved-badge', async () => {
  await ensureBaselineTree();
  await openCanvas();
  /* 未保存提示：布局改动显示「仅布局」徽标；保存后徽标消失。 */
  await dragNodeBy('start', 26, 16);
  let badge = await dirtyBadge();
  assert.equal(badge.hidden, false, 'a dirty draft did not show the unsaved badge');
  assert.equal(badge.kind, 'layout');
  assert.equal(badge.text, await page.evaluate(() => window.t('cv.dirtyLayout')));
  await saveNow();
  assert.equal((await dirtyBadge()).hidden, true, 'save left the unsaved badge visible');
  assert.equal(await page.evaluate(() => window.App.kind()), 'clean');
  const requests = [];
  const onRequest = request => {
    const url = new URL(request.url());
    if (request.method() === 'POST' && url.pathname === '/admin/trees') requests.push('create');
    if (request.method() === 'POST' && /\/admin\/trees\/[^/]+\/enable$/.test(url.pathname)) requests.push('enable');
  };
  page.on('request', onRequest);
  try {
    await page.locator('#cvNewTree').click();
    await page.waitForFunction(() => window.App.saved === null);
    await page.locator('#cvEnable').click();
    await page.waitForFunction(() => window.App.tree.enabled === true && window.App.changeKind === 'clean',
      null, { timeout: 10000 });
    assert.deepEqual(requests, ['create', 'enable'], 'enable did not auto-save the dirty new tree first');
    assert.equal((await dirtyBadge()).hidden, true);
    const server = await serverTree(await page.evaluate(() => window.App.treeId));
    assert.equal(server.enabled, true);
  } finally {
    page.off('request', onRequest);
    await ensureBaselineTree();
  }
});

test('Task 12 · enabling an already-enabled dirty draft saves it first and never re-enables', async () => {
  await ensureBaselineTree();
  await openCanvas();
  await dragNodeBy('start', 30, 20);
  const requests = [];
  const onRequest = request => {
    const url = new URL(request.url());
    if (request.method() === 'PUT' && url.pathname === '/admin/trees/' + TREE_ID) requests.push('save');
    if (request.method() === 'POST' && /\/admin\/trees\/[^/]+\/enable$/.test(url.pathname)) requests.push('enable');
  };
  page.on('request', onRequest);
  try {
    await page.locator('#cvEnable').click();
    await page.waitForFunction(() => window.App.changeKind === 'clean');
    await waitToastIncludes(await page.evaluate(() => window.t('cv.toastAlreadyEnabled')));
    await page.waitForTimeout(250);
    assert.deepEqual(requests, ['save'],
      'enable short-circuited on an enabled tree before saving its dirty draft');
    assert.equal((await dirtyBadge()).hidden, true);
    assert.equal((await serverTree(TREE_ID)).enabled, true);
  } finally {
    page.off('request', onRequest);
    await ensureBaselineTree();
  }
});

test('Task 12 · a boot fallback draft saves as a new UUID-slice tree, not "local"', async () => {
  const emptyTreeList = async route => {
    if (route.request().method() !== 'GET') { await route.continue(); return; }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ trees: [], snapshot_revision: '' })
    });
  };
  const treeListRoute = url => url.pathname === '/admin/trees';
  await page.route(treeListRoute, emptyTreeList);
  try {
    await openCanvas();
    const draft = await page.evaluate(() => ({
      treeId: window.App.treeId,
      tree: window.App.tree.id,
      saved: window.App.saved,
      kind: window.App.kind(),
      nodes: window.App.graph.nodes.length
    }));
    assert.equal(draft.saved, null);
    assert.equal(draft.treeId, draft.tree, 'boot fallback envelope id differs from the draft tree id');
    assert.match(draft.treeId, /^[0-9a-f]{12}$/, 'boot fallback id is not a frontend UUID slice: ' + draft.treeId);
    assert.notEqual(draft.treeId, 'local');
    assert.equal(draft.nodes, 2);
    assert.equal(draft.kind, 'behavior');

    const posted = page.waitForRequest(request =>
      request.method() === 'POST' && new URL(request.url()).pathname === '/admin/trees');
    await page.locator('#cvSave').click();
    const request = await posted;
    const payload = request.postDataJSON();
    assert.match(payload.id, /^[0-9a-f]{12}$/, 'POST /admin/trees did not use the UUID-slice id');
    assert.equal(payload.id, draft.treeId);
    await page.waitForFunction(id => window.App.changeKind === 'clean' && window.App.treeId === id,
      draft.treeId, { timeout: 8000 });
    const listed = (await api('GET', '/admin/trees')).trees.find(tree => tree.id === draft.treeId);
    assert.ok(listed, 'boot fallback save did not create a tree with the draft id');
    assert.equal(listed.enabled, false);
  } finally {
    await page.unroute(treeListRoute, emptyTreeList);
    await ensureBaselineTree();
  }
});

test('Task 12 · loading the example asks for confirmation and replaces the graph as one undo step', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const moved = await dragNodeBy('start', 48, 34);

  await page.locator('#cvLoadExample').click();
  const dialog = await waitConfirmDialog();
  assert.ok(dialog.includes(await page.evaluate(() => window.t('cv.confirmExampleTitle'))),
    'example load did not reuse the shared dirty dialog');
  await page.locator('#cvConfirmBody [data-act="confirm-cancel"]').click();
  await page.waitForFunction(() => document.getElementById('cvConfirmModal').hidden);
  const kept = await page.evaluate(() => {
    const node = window.App.graph.nodes.find(item => item.id === 'start');
    return { x: node.position.x, y: node.position.y, kind: window.App.kind() };
  });
  assert.deepEqual({ x: kept.x, y: kept.y }, { x: moved.x, y: moved.y });
  assert.equal(kept.kind, 'layout');

  await page.locator('#cvLoadExample').click();
  await waitConfirmDialog();
  await page.locator('#cvConfirmBody [data-act="confirm-discard"]').click();
  /* 示例是异步 fetch 的：等待它真正替换草稿（示例 start 坐标），不能只看节点数（本来就是 15）。 */
  await page.waitForFunction(expected => {
    const node = window.App.graph.nodes.find(item => item.id === 'start');
    return window.App.graph.nodes.length === expected.nodes &&
      window.App.graph.edges.length === expected.edges &&
      node && node.position.x === expected.start.x && node.position.y === expected.start.y &&
      window.App.kind() === 'clean';
  }, { nodes: EXAMPLE_NODES, edges: EXAMPLE_EDGES, start: EXAMPLE_START }, { timeout: 8000 });
  const loaded = await page.evaluate(() => ({
    kind: window.App.kind(),
    canUndo: window.App.kit.canUndo(),
    start: (() => {
      const node = window.App.graph.nodes.find(item => item.id === 'start');
      return { x: node.position.x, y: node.position.y };
    })()
  }));
  assert.equal(loaded.kind, 'clean');
  assert.equal(loaded.canUndo, true);
  assert.notDeepEqual(loaded.start, { x: moved.x, y: moved.y });

  /* 一次撤销必须回到载入前的完整草稿（拖拽后的坐标），而不是半张示例图。 */
  await page.locator('#cvUndo').click();
  await page.waitForFunction(expected =>
    window.App.graph.nodes.length === expected && window.App.kind() === 'layout', EXAMPLE_NODES);
  const undone = await page.evaluate(() => {
    const node = window.App.graph.nodes.find(item => item.id === 'start');
    return { x: node.position.x, y: node.position.y };
  });
  assert.deepEqual(undone, { x: moved.x, y: moved.y },
    'loading the example was not a single replace-entire-graph history operation');
});

test('Task 12 · loading the example from a clean saved draft still confirms before replacing it', async () => {
  await ensureBaselineTree();
  await openCanvas();
  /* clean 但内容已和示例不同（拖动后保存），让“载入示例”成为一次可见的整图替换。 */
  const moved = await dragNodeBy('start', 52, 36);
  await saveNow();
  assert.equal((await dirtyBadge()).kind, 'clean');

  await page.locator('#cvLoadExample').click();
  const dialog = await waitConfirmDialog();
  assert.ok(dialog.includes(await page.evaluate(() => window.t('cv.confirmLoadExample'))),
    'clean example load did not use the clean confirmation title');
  assert.ok(dialog.includes(await page.evaluate(() => window.t('cv.confirmLoadExampleClean'))),
    'clean example load did not explain that the whole graph is replaced');
  assert.equal(dialog.includes(await page.evaluate(() => window.t('cv.confirmDirtyBody'))), false,
    'clean example load reused the discard-draft copy');

  await page.locator('#cvConfirmBody [data-act="confirm-cancel"]').click();
  await page.waitForFunction(() => document.getElementById('cvConfirmModal').hidden);
  const kept = await page.evaluate(() => {
    const node = window.App.graph.nodes.find(item => item.id === 'start');
    return {
      start: { x: node.position.x, y: node.position.y },
      kind: window.App.kind(),
      nodes: window.App.graph.nodes.length
    };
  });
  assert.deepEqual(kept.start, moved, 'cancelling the clean example load changed the draft');
  assert.equal(kept.kind, 'clean');
  assert.equal(kept.nodes, EXAMPLE_NODES);

  await page.locator('#cvLoadExample').click();
  await waitConfirmDialog();
  await page.locator('#cvConfirmBody [data-act="confirm-discard"]').click();
  await page.waitForFunction(expected => {
    const node = window.App.graph.nodes.find(item => item.id === 'start');
    return window.App.graph.nodes.length === expected.nodes &&
      window.App.graph.edges.length === expected.edges &&
      node && node.position.x === expected.start.x && node.position.y === expected.start.y;
  }, { nodes: EXAMPLE_NODES, edges: EXAMPLE_EDGES, start: EXAMPLE_START }, { timeout: 8000 });
  const loaded = await page.evaluate(() => ({
    canUndo: window.App.kit.canUndo(),
    kind: window.App.kind()
  }));
  assert.equal(loaded.canUndo, true, 'confirming the example load did not add a history entry');
  /* 保存后的草稿与示例只差节点坐标，所以内容分类是 layout（整图替换仍算一个历史项）。 */
  assert.equal(loaded.kind, 'layout',
    'replacing the saved layout with the example was not classified as a layout change');

  /* 一次撤销回到保存后的拖拽坐标，证明示例载入只产生一个历史项。 */
  await page.locator('#cvUndo').click();
  await page.waitForFunction(expected => {
    const node = window.App.graph.nodes.find(item => item.id === 'start');
    return node && node.position.x === expected.x && node.position.y === expected.y &&
      window.App.kind() === 'clean';
  }, moved, { timeout: 4000 });
  await ensureBaselineTree();
});

test('Task 12 · export downloads the complete v3 envelope including timezone and sticky', async () => {
  const baseline = await ensureBaselineTree();
  await openCanvas();
  const download = await exportViaUi();
  assert.equal(download.name, TREE_ID + '.sr-routing-graph-v3.json');
  const parsed = JSON.parse(download.text);
  assert.equal(parsed.kind, 'sr-routing-graph');
  assert.equal(parsed.version, 3);
  assert.equal(parsed.name, baseline.name);
  assert.equal(parsed.timezone, BASELINE.timezone);
  assert.deepEqual(parsed.sticky, BASELINE.sticky);
  assert.equal(parsed.root.nodes.length, EXAMPLE_NODES);
  assert.equal(parsed.root.edges.length, EXAMPLE_EDGES);
  assert.equal(parsed.root.otherwise_target, EXAMPLE_TARGETS.otherwise);
  assert.equal(parsed.root.error_fallback, EXAMPLE_TARGETS.error);
  assert.equal((await dirtyBadge()).kind, 'clean', 'export mutated the draft');
});

test('envelope-roundtrip-preserves-revision', async () => {
  const baseline = await ensureBaselineTree();
  await openCanvas();
  const source = await exportViaUi();
  const sourceCanonical = await page.evaluate(text => {
    const parsed = JSON.parse(text);
    return window.RTGraph.behaviorCanonical({
      name: parsed.name, timezone: parsed.timezone, sticky: parsed.sticky, root: parsed.root
    });
  }, source.text);

  await page.locator('#cvNewTree').click();
  await page.waitForFunction(() => window.App.saved === null && window.App.graph.nodes.length === 2);
  const newId = await page.evaluate(() => window.App.treeId);
  await setImportPayload(source.text);
  await page.waitForFunction(expected =>
    window.App.graph.nodes.length === expected.nodes && window.App.graph.edges.length === expected.edges,
  { nodes: EXAMPLE_NODES, edges: EXAMPLE_EDGES }, { timeout: 8000 });
  const imported = await page.evaluate(() => ({
    id: window.App.treeId,
    treeId: window.App.tree.id,
    name: window.App.tree.name,
    timezone: window.App.tree.timezone,
    sticky: window.App.tree.sticky,
    kind: window.App.kind(),
    canonical: window.RTGraph.behaviorCanonical(window.App.tree),
    canUndo: window.App.kit.canUndo()
  }));
  assert.equal(imported.id, newId, 'import replaced the target group id');
  assert.equal(imported.treeId, newId);
  assert.equal(imported.name, JSON.parse(source.text).name);
  assert.equal(imported.timezone, BASELINE.timezone);
  assert.deepEqual(imported.sticky, BASELINE.sticky);
  assert.equal(imported.canonical, sourceCanonical, 'import round-trip changed the behavior canonical');
  assert.equal(imported.kind, 'behavior');
  assert.equal(imported.canUndo, true);

  /* 保存回服务端：行为 canonical 相同 ⇒ 服务端 tree_revision 必须与导出源一致。 */
  await saveNow();
  const roundTripped = await serverTree(newId);
  assert.equal(roundTripped.tree_revision, baseline.tree_revision,
    'export → import → save changed the behavior revision');
  assert.equal(roundTripped.root.nodes.length, EXAMPLE_NODES);
  assert.equal(roundTripped.root.edges.length, EXAMPLE_EDGES);

  await page.locator('#cvUndo').click();
  await page.waitForFunction(() => window.App.graph.nodes.length === 2);
  assert.equal(await page.evaluate(() => window.App.graph.edges.length), 1,
    'one undo did not restore the pre-import draft');
});

test('7.4-14-import-export-v3-only', async () => {
  await ensureBaselineTree();
  await openCanvas();
  /* 导出只产出 sr-routing-graph v3；导入下面只接受同一格式。 */
  const exported = await exportViaUi();
  const exportedDoc = JSON.parse(exported.text);
  assert.equal(exportedDoc.kind, 'sr-routing-graph');
  assert.equal(exportedDoc.version, 3);
  assert.equal(exportedDoc.root.type, 'graph');
  await dragNodeBy('start', 36, 24);

  /* 未保存时先弹共享确认框；取消后不选文件、草稿保持。 */
  await page.locator('#cvImport').click();
  const dialog = await waitConfirmDialog();
  assert.ok(dialog.includes(await page.evaluate(() => window.t('cv.confirmImportTitle'))),
    'import did not reuse the shared dirty dialog');
  await page.locator('#cvConfirmBody [data-act="confirm-cancel"]').click();
  await page.waitForFunction(() => document.getElementById('cvConfirmModal').hidden);
  assert.equal((await dirtyBadge()).kind, 'layout');

  const rejection = await page.evaluate(() => window.RTGraph.REJECT_MESSAGE);
  const keptBefore = await page.evaluate(() => {
    const node = window.App.graph.nodes.find(item => item.id === 'start');
    return { x: node.position.x, y: node.position.y };
  });
  await setImportPayload({ root: { type: 'jev', nodes: [], edges: [] }, version: 1 }, 'legacy-jev.json');
  await waitToastIncludes(rejection);
  const kept = await page.evaluate(() => {
    const node = window.App.graph.nodes.find(item => item.id === 'start');
    return { x: node.position.x, y: node.position.y, kind: window.App.kind() };
  });
  assert.deepEqual({ x: kept.x, y: kept.y }, keptBefore, 'a rejected legacy import replaced the draft');
  assert.equal(kept.kind, 'layout');
  assert.equal(await page.evaluate(() => window.App.graph.nodes.length), EXAMPLE_NODES);

  /* 正确 kind、version=2：同一固定拒绝文案。 */
  await clearToasts();
  const source = await exportViaUi();
  const version2 = JSON.parse(source.text);
  version2.version = 2;
  await clearToasts();
  await setImportPayload(version2, 'version-2.json');
  await waitToastIncludes(rejection);
  assert.deepEqual(await page.evaluate(() => {
    const node = window.App.graph.nodes.find(item => item.id === 'start');
    return { x: node.position.x, y: node.position.y };
  }), keptBefore, 'a rejected version-2 import replaced the draft');
});

test('Task 12 · a v3 document with a malformed root reports graph_parse_error and keeps the draft', async () => {
  await ensureBaselineTree();
  await openCanvas();
  await dragNodeBy('start', 34, 22);
  const draftOf = () => page.evaluate(() => {
    const node = window.App.graph.nodes.find(item => item.id === 'start');
    return {
      start: { x: node.position.x, y: node.position.y },
      kind: window.App.kind(),
      nodes: window.App.graph.nodes.length,
      edges: window.App.graph.edges.length
    };
  });
  const kept = await draftOf();

  await clearToasts();
  await setImportPayload({
    kind: 'sr-routing-graph',
    version: 3,
    name: 'v3 但 root 非法',
    timezone: BASELINE.timezone,
    sticky: BASELINE.sticky,
    root: { type: 'jev', branches: [] }
  }, 'malformed-v3.json');

  await waitToastIncludes(await page.evaluate(n => window.tf('cv.toastImportIssues', { n }), 1));
  const issues = await page.evaluate(() => window.App.issues.map(issue => ({
    code: issue.code, level: issue.level, zh: issue.zh, en: issue.en,
    node_id: issue.node_id, edge_id: issue.edge_id, port: issue.port
  })));
  /* 被拒文件的 Issue 不能留在校验条：否则「定位」会指向草稿里不存在的节点。
     校验条恢复为当前草稿的 4 条提示（§5.3）。 */
  assert.deepEqual(issues.map(issue => issue.code), ['hint', 'hint', 'hint', 'hint'],
    'a malformed v3 import leaked its parse issue into the draft validation bar');
  assert.equal(issues.every(issue => issue.level === 'hint'), true);
  assert.deepEqual(issues.map(issue => [issue.node_id, issue.edge_id]),
    [['', ''], ['', ''], ['', ''], ['', '']]);
  assert.equal(await page.locator('#cvIssues .rtc-issue.is-hard').count(), 0,
    'the validation bar rendered the rejected file as a hard issue');
  assert.equal(await page.locator('#cvIssues [data-locate]').count(), 0,
    'the rejected file left locatable issues pointing at draft nodes');
  assert.deepEqual(await draftOf(), kept, 'a malformed v3 import changed the draft');
});

test('Task 12 · a valid v3 import without positions lays every node out with finite coordinates', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const stripped = JSON.parse(JSON.stringify(EXAMPLE));
  stripped.nodes.forEach(node => { delete node.position; });
  await setImportPayload({
    kind: 'sr-routing-graph',
    version: 3,
    name: BASELINE.name,
    timezone: BASELINE.timezone,
    sticky: BASELINE.sticky,
    root: stripped
  }, 'missing-positions.json');
  await page.waitForFunction(expected =>
    window.App.graph.nodes.length === expected && window.App.graph.nodes.every(node =>
      node.position && Number.isFinite(node.position.x) && Number.isFinite(node.position.y)),
  EXAMPLE_NODES, { timeout: 8000 });
  const result = await page.evaluate(() => ({
    kind: window.App.kind(),
    issues: window.App.issues.filter(issue => issue.level !== 'hint').map(issue => issue.code),
    nodes: window.App.graph.nodes.map(node => ({ id: node.id, x: node.position.x, y: node.position.y })),
    expected: window.RTGraph.layoutPositions('dag', {
      nodes: window.App.graph.nodes, edges: window.App.graph.edges
    })
  }));
  assert.deepEqual(result.issues, [], 'position-less valid v3 import failed structure validation');
  result.nodes.forEach(node => {
    assert.ok(Number.isFinite(node.x) && Number.isFinite(node.y), `${node.id} has no finite position`);
    assert.deepEqual({ x: node.x, y: node.y }, result.expected[node.id],
      `${node.id} was not placed by the shared DAG layout`);
  });
  assert.equal(result.kind, 'layout', 'filling missing positions must stay layout-only');
});

test('7.4-12-auto-layout-layout-only', async () => {
  const baseline = await ensureBaselineTree();
  await openCanvas();
  await dragNodeBy('start', 70, 45);
  const expected = await page.evaluate(() => window.RTGraph.layoutPositions('dag', window.App.graph));
  await page.locator('#cvAutoLayout').click();
  await page.waitForFunction(positions => window.App.graph.nodes.every(node => {
    const next = positions[node.id];
    return next && node.position.x === next.x && node.position.y === next.y;
  }), expected, { timeout: 4000 });
  const laidOut = await page.evaluate(() => ({
    kind: window.App.kind(),
    badge: document.getElementById('cvDirty').dataset.kind,
    positions: Object.fromEntries(window.App.graph.nodes.map(node =>
      [node.id, { x: node.position.x, y: node.position.y }]))
  }));
  assert.equal(laidOut.kind, 'layout');
  assert.equal(laidOut.badge, 'layout');

  await saveNow();
  const server = await serverTree(TREE_ID);
  assert.equal(server.tree_revision, baseline.tree_revision,
    'auto layout leaked into the server behavior revision');
  server.root.nodes.forEach(node => {
    const position = laidOut.positions[node.id];
    assert.ok(position, `${node.id} missing from the client layout`);
    assert.ok(Math.abs(node.position.x - position.x) < 0.01, `${node.id} saved X differs from the DAG layout`);
    assert.ok(Math.abs(node.position.y - position.y) < 0.01, `${node.id} saved Y differs from the DAG layout`);
  });
});

test('7.4-15-validation-bar-and-locate', async () => {
  await ensureBaselineTree();
  await openCanvas();
  /* 用禁用中的新组（enable 才会先保存）；载入示例，让 422 Issue 有可定位的节点/边。 */
  await page.locator('#cvNewTree').click();
  await page.waitForFunction(() => window.App.saved === null && window.App.graph.nodes.length === 2);
  const targetId = await page.evaluate(() => window.App.treeId);
  await page.locator('#cvLoadExample').click();
  await waitConfirmDialog();
  await page.locator('#cvConfirmBody [data-act="confirm-discard"]').click();
  await page.waitForFunction(expected => window.App.graph.nodes.length === expected.nodes &&
    window.App.graph.edges.length === expected.edges &&
    window.App.graph.nodes.some(node => node.id === 'deep-target'),
  { nodes: EXAMPLE_NODES, edges: EXAMPLE_EDGES }, { timeout: 8000 });
  await saveNow();
  assert.equal(await page.evaluate(() => window.App.tree.enabled), false,
    'the fixture tree must start disabled so enable really runs');
  await dragNodeBy('start', 40, 30);
  const issuesPayload = [
    { code: 'invalid_timezone', level: 'hard', zh: '时区无效（QA）', en: 'Invalid timezone (QA)', node_id: '', edge_id: '', port: '' },
    { code: 'question_incomplete', level: 'hard', zh: '问题节点不完整（QA）', en: 'Incomplete question (QA)', node_id: 'capability', edge_id: '', port: '' },
    { code: 'numeric_edge_condition_invalid', level: 'hard', zh: '槽位条件非法（QA）', en: 'Invalid slot condition (QA)', node_id: 'deep-target', edge_id: 'e-clarify-deep', port: 'in_1' },
    { code: 'fallback_missing', level: 'hard', zh: '兜底缺失（QA）', en: 'Missing fallback (QA)', node_id: '', edge_id: '', port: 'otherwise_target' }
  ];
  const enableRequests = [];
  const onRequest = request => {
    if (request.method() === 'POST' && /\/admin\/trees\/[^/]+\/enable$/.test(new URL(request.url()).pathname)) {
      enableRequests.push(request.url());
    }
  };
  const putHandler = async route => {
    const url = new URL(route.request().url());
    if (route.request().method() === 'PUT' && url.pathname === '/admin/trees/' + targetId) {
      await route.fulfill({
        status: 422,
        contentType: 'application/json',
        body: JSON.stringify({ detail: issuesPayload })
      });
      return;
    }
    await route.continue();
  };
  page.on('request', onRequest);
  await page.route('**/admin/trees/**', putHandler);
  try {
    /* 只允许这一条故意 422：URL 精确到本用例的新组，状态必须是 422。 */
    expectHttpStatusError({ url: new RegExp('/admin/trees/' + targetId + '$'), status: /422/ });
    await page.locator('#cvEnable').click();
    await page.waitForFunction(() => window.App.issues.length === 8 &&
      !document.getElementById('cvIssues').hidden, null, { timeout: 8000 });
    await page.waitForTimeout(300);
    assert.equal(expectedHttpStatusErrors.length, 0,
      'the scoped 422 console error was not observed at the expected URL/status');
    assert.equal(enableRequests.length, 0, 'enable was sent even though the save failed');
    assert.equal(await page.evaluate(() => window.App.tree.enabled), false);
    assert.deepEqual(await page.evaluate(() => window.App.issues.filter(issue => issue.level !== 'hint')
      .map(issue => issue.code)),
      ['invalid_timezone', 'question_incomplete', 'numeric_edge_condition_invalid', 'fallback_missing']);
    /* §5.3：服务端 4 个错误必须与当前草稿的 4 条提示合并显示。 */
    assert.equal(await page.evaluate(() => window.App.issues.filter(issue => issue.level === 'hint').length),
      4, 'the 422 issues dropped the 4 RTGraph hints');
    assert.equal(await page.evaluate(() => window.RTGraph.validate(window.App.tree.root)
      .filter(issue => issue.level === 'hint').length), 4);
    assert.equal(await page.evaluate(() => window.App.issues.every(issue =>
      issue.level === 'hint' || issue.level === 'hard')), true);

    /* 节点定位：选中并居中 */
    await page.locator('#cvIssues [data-issue-node="capability"]').click();
    assert.deepEqual(await page.evaluate(() => window.App.kit.getSelection()),
      { kind: 'node', id: 'capability' });
    assert.equal(await page.evaluate(() => {
      const stage = document.getElementById('cvStage').getBoundingClientRect();
      const rect = document.querySelector('[data-node-id="capability"]').getBoundingClientRect();
      return Math.abs(rect.left + rect.width / 2 - (stage.left + stage.width / 2)) < 60 &&
        Math.abs(rect.top + rect.height / 2 - (stage.top + stage.height / 2)) < 60;
    }), true, 'node issue locate did not center the node');
    /* 等保存失败 toast 自然消失，截一张干净的「校验错误 + 已定位节点」证据图。 */
    await page.waitForTimeout(4300);
    await screenshot('validation-error-locating-1440.png');

    /* 边 / 槽位定位：Issue 保留 slot 字段，选中边并打开同一条件浮层 */
    const slotIssue = await page.evaluate(() =>
      window.App.issues.find(issue => issue.code === 'numeric_edge_condition_invalid'));
    assert.equal(slotIssue.port, 'in_1', 'the slot issue lost its port');
    await page.locator('#cvIssues [data-issue-edge="e-clarify-deep"]').click();
    await waitCondPop('e-clarify-deep');
    assert.deepEqual(await page.evaluate(() => window.App.kit.getSelection()),
      { kind: 'edge', id: 'e-clarify-deep' });

    /* 图级定位：条件浮层此刻仍开着；定位必须自己关掉它并聚焦检查器图设置。 */
    await page.locator('#cvIssues [data-issue-graph="1"]').first().click();
    assert.equal(await page.evaluate(() => document.getElementById('cvCondPop').hidden), true,
      'graph-level locate left the condition popup open');
    assert.deepEqual(await page.evaluate(() => window.App.kit.getSelection()), { kind: null, id: null });
    assert.equal(await page.evaluate(() =>
      document.getElementById('cvInspector').classList.contains('is-focus')), true);
    await waitInspectorBranch('graph', 'root');

    /* Task 14：试算/收起会把检查器折叠起来；图级定位必须重新展开图设置，
       否则「定位」只把焦点送进一个不可见的面板。 */
    await page.evaluate(() => window.App.collapsePanels());
    await page.waitForFunction(() =>
      document.getElementById('cvInspector').classList.contains('is-collapsed'));
    await page.locator('#cvIssues [data-issue-graph="1"]').first().click();
    assert.equal(await page.evaluate(() =>
      document.getElementById('cvInspector').classList.contains('is-collapsed')), false,
    'graph-level locate left the collapsed inspector hidden');
    await waitInspectorBranch('graph', 'root');
    assert.equal(await page.evaluate(() =>
      document.getElementById('cvInspector').classList.contains('is-focus')), true);
  } finally {
    resetExpectedHttpStatusErrors();
    page.off('request', onRequest);
    await page.unroute('**/admin/trees/**', putHandler);
    await ensureBaselineTree();
  }
});

test('Task 12 · example graph saves, enables and survives a page refresh with a consistent revision', async () => {
  await ensureBaselineTree();
  await openCanvas();
  await page.locator('#cvNewTree').click();
  await page.waitForFunction(() => window.App.saved === null && window.App.graph.nodes.length === 2);
  const newId = await page.evaluate(() => window.App.treeId);
  await page.locator('#cvLoadExample').click();
  await waitConfirmDialog();
  await page.locator('#cvConfirmBody [data-act="confirm-discard"]').click();
  /* 新建草稿是 2 个节点：必须等示例 root 真正替换后再保存。 */
  await page.waitForFunction(expected => window.App.graph.nodes.length === expected.nodes &&
    window.App.graph.edges.length === expected.edges &&
    window.App.graph.nodes.some(node => node.id === 'deep-target'),
  { nodes: EXAMPLE_NODES, edges: EXAMPLE_EDGES }, { timeout: 8000 });
  await saveNow();

  const savedServer = await serverTree(newId);
  assert.equal(savedServer.enabled, false);
  assert.equal(savedServer.root.nodes.length, EXAMPLE_NODES);
  assert.equal(savedServer.root.edges.length, EXAMPLE_EDGES);
  const revision = savedServer.tree_revision;

  await page.locator('#cvEnable').click();
  await page.waitForFunction(() => window.App.tree.enabled === true, null, { timeout: 8000 });
  const enabledServer = await serverTree(newId);
  assert.equal(enabledServer.enabled, true);
  assert.equal(enabledServer.tree_revision, revision, 'enabling the example graph changed its revision');

  await openCanvas();
  const after = await page.evaluate(() => ({
    treeId: window.App.treeId,
    revision: window.App.tree.tree_revision,
    enabled: window.App.tree.enabled,
    nodes: window.App.graph.nodes.length,
    edges: window.App.graph.edges.length,
    kind: window.App.kind(),
    revisionLabel: document.getElementById('cvRevision').textContent
  }));
  assert.equal(after.treeId, newId, 'refresh booted a different tree than the enabled one');
  assert.equal(after.revision, revision, 'refreshed tree revision differs from the GET revision');
  assert.equal(after.enabled, true);
  assert.equal(after.nodes, EXAMPLE_NODES);
  assert.equal(after.edges, EXAMPLE_EDGES);
  assert.equal(after.kind, 'clean');
  assert.ok(after.revisionLabel.includes(revision.slice(0, 10)),
    'toolbar revision label does not show the refreshed revision');
  assert.equal((await serverTree(newId)).tree_revision, revision);
  await ensureBaselineTree();
});

/* ── Task 13 · 真实试算 / trace-only 回放 / 轨迹抽屉 / 波次布局 ─────── */

/* 样例输入是「文本 + 固定 now」的请求预设：断言用 fixture 的文本/时间，
   但答案永远从实时 /admin/try 读，QA 不把 fixture answers 传给页面。 */
const TRY_SAMPLES = FIXTURE.scenarios.map(scenario => ({
  id: scenario.id,
  name: scenario.name,
  text: scenario.text,
  now: scenario.now
}));

async function prepareTryCanvas() {
  await ensureBaselineTree();
  await openCanvas();
  const treeId = await page.evaluate(() => window.App.treeId);
  if (treeId !== TREE_ID) {
    await page.evaluate(id => window.App.selectTree(id), TREE_ID);
    await page.waitForFunction(id => window.App.treeId === id && window.App.booted,
      TREE_ID, { timeout: 10000 });
  }
  await page.waitForFunction(() => window.App && !window.App.tryRunning);
}

async function waitTryResult(expected) {
  await page.waitForFunction(want => {
    const replay = window.App.replay;
    if (!replay || window.App.tryRunning) return false;
    if (!want) return true;
    if (want.text != null && replay.text !== want.text) return false;
    if (want.now !== undefined && (replay.now || null) !== want.now) return false;
    return true;
  }, expected || null, { timeout: 15000 });
}

async function runTrySample(index) {
  const sample = TRY_SAMPLES[index];
  assert.ok(sample, `missing scenario fixture at index ${index}`);
  await page.locator(`[data-try-sample="${index}"]`).click();
  await waitTryResult({ text: sample.text, now: sample.now });
  return sample;
}

async function runTryManual(text) {
  await page.locator('#cvTryInput').fill(text);
  await page.locator('#cvTryRun').click();
  await waitTryResult({ text: text });
}

/* 一次性读取回放数据 + 卡片/边/结论/抽屉的 DOM 状态；所有断言都基于
   页面公开状态（App.replay / data-* / class），不猜内部实现。 */
async function trySnapshot() {
  return page.evaluate(() => {
    const replay = window.App.replay;
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
      running: window.App.tryRunning,
      runDisabled: document.getElementById('cvTryRun').disabled,
      topRunDisabled: document.getElementById('cvTryBtn').disabled,
      palCollapsed: document.getElementById('cvPalette').classList.contains('is-collapsed'),
      inspCollapsed: document.getElementById('cvInspector').classList.contains('is-collapsed')
    };
  });
}

function assertIncludes(haystack, needle, message) {
  assert.ok(String(haystack).indexOf(needle) >= 0,
    `${message || 'missing text'} (wanted ${JSON.stringify(needle)} in ${JSON.stringify(String(haystack).slice(0, 400))})`);
}

test('7.4-16-try-input-samples-loading-lock', async () => {
  await prepareTryCanvas();
  /* 试算台 = 文本输入 + 5 个样例 chip；运行中由下面断言 loading 与编辑锁。 */
  assert.equal(await page.locator('#cvTryInput').count(), 1, 'try input is missing');
  assert.equal(await page.locator('#cvTryChips [data-try-sample]').count(), 5,
    'the try bar does not offer exactly five sample chips');
  const samples = await page.evaluate(() => window.RTTryRun.samples());
  assert.equal(samples.length, 5);
  samples.forEach((sample, index) => {
    assert.equal(sample.text, TRY_SAMPLES[index].text, `sample ${index} text drifted`);
  });
  const before = await page.evaluate(() => ({
    position: Object.assign({}, window.App.graph.nodes.find(node => node.id === 'start').position),
    undo: window.App.kit.canUndo(),
    nodes: window.App.graph.nodes.length
  }));
  let release = null;
  const gate = new Promise(resolve => { release = resolve; });
  const tryMatcher = url => url.pathname === '/admin/try';
  await page.route(tryMatcher, async route => {
    await gate;
    await route.continue();
  });
  try {
    await page.locator('[data-try-sample="0"]').click();
    await page.waitForFunction(() => window.App.tryRunning === true, null, { timeout: 5000 });
    const loading = await page.evaluate(() => ({
      running: window.App.tryRunning,
      runDisabled: document.getElementById('cvTryRun').disabled,
      topDisabled: document.getElementById('cvTryBtn').disabled,
      runText: document.getElementById('cvTryRun').textContent.trim(),
      expectedRunText: window.t('cv.tryRunning'),
      inputDisabled: document.getElementById('cvTryInput').disabled,
      chipDisabled: document.querySelector('[data-try-sample="0"]').disabled,
      bodyRunning: document.body.classList.contains('is-try-running'),
      stageLocked: document.getElementById('cvStage').classList.contains('is-locked'),
      stageCursor: getComputedStyle(document.getElementById('cvStage')).cursor,
      stagePulse: getComputedStyle(document.getElementById('cvStage'), '::after').animationName,
      palCollapsed: document.getElementById('cvPalette').classList.contains('is-collapsed'),
      inspCollapsed: document.getElementById('cvInspector').classList.contains('is-collapsed')
    }));
    assert.equal(loading.running, true);
    assert.equal(loading.runDisabled, true, 'run button stayed enabled during the request');
    assert.equal(loading.topDisabled, true, 'top-bar try button stayed enabled during the request');
    assert.equal(loading.runText, loading.expectedRunText, 'run button does not show the localized loading copy');
    assert.equal(loading.inputDisabled, true, 'try input stayed editable during the request');
    assert.equal(loading.chipDisabled, true, 'sample chips stayed clickable during the request');
    assert.equal(loading.bodyRunning, true, 'body.is-try-running was not applied during the request');
    assert.equal(loading.stageLocked, true, 'stage.is-locked was not applied during the request');
    assert.equal(loading.stageCursor, 'progress', 'locked stage did not show the progress cursor affordance');
    assert.equal(loading.stagePulse, 'rtc-lock-pulse', 'locked stage did not show the token-based lock pulse');
    assert.equal(loading.palCollapsed, true, 'node library did not auto-collapse for the run');
    assert.equal(loading.inspCollapsed, true, 'inspector did not auto-collapse for the run');

    /* 请求在途时拖节点必须被拒绝：位置、历史栈和节点数都不能变。 */
    const box = await page.locator('[data-node-id="start"]').first().boundingBox();
    assert.ok(box, 'start node not visible');
    await page.mouse.move(box.x + 24, box.y + 10);
    await page.mouse.down();
    await page.mouse.move(box.x + 84, box.y + 58, { steps: 8 });
    await page.mouse.up();
    await page.waitForTimeout(140);
    const during = await page.evaluate(() => ({
      position: Object.assign({}, window.App.graph.nodes.find(node => node.id === 'start').position),
      undo: window.App.kit.canUndo(),
      nodes: window.App.graph.nodes.length
    }));
    assert.deepEqual(during.position, before.position, 'dragging a node mutated the draft during the run');
    assert.equal(during.undo, before.undo, 'dragging a node pushed history during the run');
    assert.equal(during.nodes, before.nodes);
    /* 即使绕过 disabled 直接派发 click，飞行中的 chip 也不能改写输入框。 */
    const guarded = await page.evaluate(() => {
      document.querySelector('[data-try-sample="3"]')
        .dispatchEvent(new MouseEvent('click', { bubbles: true }));
      return document.getElementById('cvTryInput').value;
    });
    assert.equal(guarded, TRY_SAMPLES[0].text, 'a chip click rewrote the in-flight input text');
  } finally {
    /* gate 释放后请求继续；保留路由（已 resolve 的 gate 不再阻塞后续用例）。 */
    release();
  }
  await waitTryResult({ text: TRY_SAMPLES[0].text });
  const after = await page.evaluate(() => ({
    running: window.App.tryRunning,
    runDisabled: document.getElementById('cvTryRun').disabled,
    topDisabled: document.getElementById('cvTryBtn').disabled,
    replay: !!window.App.replay,
    inputDisabled: document.getElementById('cvTryInput').disabled,
    chipDisabled: document.querySelector('[data-try-sample="0"]').disabled,
    bodyRunning: document.body.classList.contains('is-try-running'),
    stageLocked: document.getElementById('cvStage').classList.contains('is-locked'),
    stageCursor: getComputedStyle(document.getElementById('cvStage')).cursor,
    palCollapsed: document.getElementById('cvPalette').classList.contains('is-collapsed'),
    inspCollapsed: document.getElementById('cvInspector').classList.contains('is-collapsed')
  }));
  assert.equal(after.running, false);
  assert.equal(after.runDisabled, false, 'run button stayed disabled after the result');
  assert.equal(after.topDisabled, false);
  assert.equal(after.replay, true);
  assert.equal(after.inputDisabled, false, 'try input stayed disabled after the result');
  assert.equal(after.chipDisabled, false, 'sample chips stayed disabled after the result');
  assert.equal(after.bodyRunning, false, 'body.is-try-running survived the result');
  assert.equal(after.stageLocked, false, 'stage.is-locked survived the result');
  assert.equal(after.stageCursor === 'progress', false, 'stage kept the progress cursor after the result');
  assert.equal(after.palCollapsed, true, 'node library expanded again after the run');
  assert.equal(after.inspCollapsed, true, 'inspector expanded again after the run');

  /* 用户仍可手动再展开两个面板（规格 §0-10：结束保持收起，可再展开）。 */
  await page.locator('#cvPalToggle').click();
  await page.locator('#cvInspToggle').click();
  await page.waitForFunction(() => !document.getElementById('cvPalette').classList.contains('is-collapsed') &&
    !document.getElementById('cvInspector').classList.contains('is-collapsed'));
});

test('Task 13 · a setup exception still releases the run lock and reset cannot strand it', async () => {
  await prepareTryCanvas();
  const failure = await page.evaluate(async () => {
    const originalCollapse = window.App.collapsePanels;
    window.App.collapsePanels = function () {
      window.App.kit.setLocked(true);
      document.body.classList.add('is-try-running');
      throw new Error('boom-setup');
    };
    let result = null;
    try {
      result = await window.RTTryRun.run('setup failure');
    } catch (error) {
      result = 'rejected:' + String(error && error.message ? error.message : error);
    }
    window.App.collapsePanels = originalCollapse;
    return {
      result: result,
      running: window.App.tryRunning,
      locked: window.App.kit.isLocked(),
      stageLocked: document.getElementById('cvStage').classList.contains('is-locked'),
      bodyRunning: document.body.classList.contains('is-try-running'),
      inputDisabled: document.getElementById('cvTryInput').disabled,
      chipsDisabled: document.querySelector('[data-try-sample="0"]').disabled,
      runDisabled: document.getElementById('cvTryRun').disabled
    };
  });
  assert.equal(failure.result, false, 'run() did not absorb the setup exception');
  assert.equal(failure.running, false, 'a setup exception left App.tryRunning stuck');
  assert.equal(failure.locked, false, 'a setup exception left the canvas kit locked');
  assert.equal(failure.stageLocked, false, 'a setup exception left stage.is-locked');
  assert.equal(failure.bodyRunning, false, 'a setup exception left body.is-try-running');
  assert.equal(failure.inputDisabled, false, 'a setup exception left the try input disabled');
  assert.equal(failure.chipsDisabled, false, 'a setup exception left the sample chips disabled');
  assert.equal(failure.runDisabled, false, 'a setup exception left the run button disabled');

  /* reset()（清除/换组/编辑）碰到飞行中的 run 时必须主动解锁，而不是等一个 token 已失效的 finally。 */
  const stranded = await page.evaluate(() => {
    window.App.tryRunning = true;
    window.App.kit.setLocked(true);
    document.body.classList.add('is-try-running');
    window.RTTryRun.reset();
    const snapshot = {
      running: window.App.tryRunning,
      locked: window.App.kit.isLocked(),
      stageLocked: document.getElementById('cvStage').classList.contains('is-locked'),
      bodyRunning: document.body.classList.contains('is-try-running'),
      inputDisabled: document.getElementById('cvTryInput').disabled
    };
    window.App.render();
    return snapshot;
  });
  assert.equal(stranded.running, false, 'reset() left App.tryRunning stuck');
  assert.equal(stranded.locked, false, 'reset() left the canvas kit locked');
  assert.equal(stranded.stageLocked, false, 'reset() left stage.is-locked');
  assert.equal(stranded.bodyRunning, false, 'reset() left body.is-try-running');
  assert.equal(stranded.inputDisabled, false, 'reset() left the try input disabled');
});

test('Task 13 · five chips mirror the mock scenarios and answers follow the live draft', async () => {
  await prepareTryCanvas();
  const samples = await page.evaluate(() => window.RTTryRun.samples());
  assert.equal(samples.length, TRY_SAMPLES.length);
  samples.forEach((sample, index) => {
    assert.equal(sample.text, TRY_SAMPLES[index].text, `chip ${index} text drifted from the mock scenario`);
    assert.equal(sample.now, TRY_SAMPLES[index].now, `chip ${index} now drifted from the fixture`);
  });
  const labels = await page.evaluate(() => Array.prototype.map.call(
    document.querySelectorAll('#cvTryChips [data-try-sample]'), el => el.textContent.trim()));
  assert.deepEqual(labels, TRY_SAMPLES.map(sample => sample.name));

  /* IME 组词中的 Enter 只用于上屏，不能被当成运行快捷键。 */
  const composing = await page.evaluate(() => {
    const input = document.getElementById('cvTryInput');
    const event = new KeyboardEvent('keydown', {
      key: 'Enter', bubbles: true, cancelable: true, isComposing: true
    });
    input.dispatchEvent(event);
    return { prevented: event.defaultPrevented, running: window.App.tryRunning };
  });
  assert.equal(composing.prevented, false, 'Enter during IME composition was handled as a run');
  assert.equal(composing.running, false, 'Enter during IME composition started a run');

  await runTrySample(0);
  let snap = await trySnapshot();
  assert.equal(snap.final.target_id, 'power-target');
  assert.equal(snap.now, TRY_SAMPLES[0].now, 'sample run did not send the fixed scenario now');

  /* 改当前草稿的阈值后重跑：服务端按新草稿求值，power 链不再命中。 */
  await page.evaluate(() => {
    window.App.change('behavior', tree => {
      const edge = tree.root.edges.find(item => item.id === 'e-domain-and-power');
      edge.condition = { op: 'gt', value: 0.99 };
    });
  });
  assert.equal(await page.evaluate(() => window.App.replay), null,
    'editing the draft after a result did not clear the replay');
  await runTrySample(0);
  snap = await trySnapshot();
  assert.equal(snap.final.target_id, 'strong-target', 'the rerun did not use the edited threshold');
  assert.equal(snap.nodes['power-target'].replay, 'not-triggered');
  assert.equal(snap.nodes['strong-target'].replay, 'final');
  assert.deepEqual(snap.final.candidates.map(candidate => candidate.target_id), ['strong-target']);
});

test('7.4-17-replay-slots-gates-targets-priority', async () => {
  await prepareTryCanvas();
  await runTrySample(0);
  const snap = await trySnapshot();
  assert.equal(snap.final.target_id, 'power-target');
  assert.equal(snap.final.kind, 'target');
  assert.equal(snap.final.selection_reason, 'target');
  assert.equal(snap.final.model, 'qwen3.8-flash-sft');
  assert.deepEqual(snap.final.candidates, [
    { target_id: 'power-target', priority: 5, via_node: 'and-power', via_port: 'in_1', wave: 1 },
    { target_id: 'strong-target', priority: 3, via_node: 'capability', via_port: 'in_1', wave: 1 }
  ]);
  assert.deepEqual(snap.waves.map(wave => wave.questions.sort()), [['capability', 'domain']]);
  assert.equal(snap.resultHidden, false, 'conclusion panel stayed hidden');
  assert.equal(snap.resultFinal, 'power-target');
  assertIncludes(snap.resultText, 'power-target', 'conclusion does not name the final target');
  assertIncludes(snap.resultText, 'strong-target', 'conclusion does not show the rival candidate');
  assertIncludes(snap.resultText, 'p5', 'conclusion lost the winner priority');
  assertIncludes(snap.resultText, 'p3', 'conclusion lost the rival priority');

  /* 槽位：两个 true 槽位 + 条件文本都在卡片上。 */
  assert.equal(snap.slots.andPowerIn1.status, 'true');
  assertIncludes(snap.slots.andPowerIn1.classes, 'is-true');
  assertIncludes(snap.slots.andPowerIn1.live, '0.84');
  assertIncludes(snap.slots.andPowerIn1.live, 'true');
  assert.equal(snap.slots.andPowerIn2.status, 'true');
  assertIncludes(snap.slots.andPowerIn2.live, '0.92');
  assert.equal(snap.slots.powerIn1.status, 'true');

  /* 门三值、问题概率/selected、P(yes)、条件节点。 */
  assert.equal(snap.nodes['and-power'].gate, 'true');
  assert.equal(snap.nodes['or-ctx'].gate, 'true');
  assert.equal(snap.nodes['not-cap'].gate, 'false');
  assert.equal(snap.nodes['and-follow'].gate, 'false');
  assert.deepEqual(snap.nodes.capability.selectedOptions, ['strong']);
  assert.equal(snap.nodes.capability.live['prob.strong'], '0.84');
  assert.equal(snap.nodes.capability.live.p_top, '0.84');
  assert.equal(snap.nodes.capability.live.margin, '0.72');
  assert.equal(snap.nodes.domain.live.yes, '0.92');
  assert.equal(snap.nodes.domain.live.no, '0.08');
  assert.equal(snap.nodes['cond-text'].replay, 'hit');
  assert.equal(snap.nodes['cond-peak'].replay, 'miss');
  assertIncludes(snap.nodes['cond-text'].state, await page.evaluate(() => window.t('cv.tryHit')));

  /* target 命中/候选/未触发 + 边高亮 + 波次头条件。 */
  assert.equal(snap.nodes['power-target'].replay, 'final');
  assertIncludes(snap.nodes['power-target'].foot, 'p5');
  assert.equal(snap.nodes['strong-target'].replay, 'candidate');
  assertIncludes(snap.nodes['strong-target'].foot, 'p3');
  assert.equal(snap.nodes['light-target'].replay, 'not-triggered');
  assert.equal(snap.svgReplay, true);
  assertIncludes(snap.edges['e-cap-strong-and-power'], 'is-true');
  assertIncludes(snap.edges['e-selected-strong-strong'], 'is-true');
  assertIncludes(snap.edges['e-selected-easy-light'], 'is-false');
  assert.equal(snap.hudHidden, false, 'replay HUD is hidden');
  assert.equal(snap.hudTitles.length, 1);
  assertIncludes(snap.hudTitles[0], '0.84');
  assertIncludes(snap.hudTitles[0], '> 0.65');
  assertIncludes(snap.hudTitles[0], '> 0.7');
  await screenshot('s1-replay-1440.png');
});

test('Task 13 · the HUD keeps eight wave capsules and the final chip reachable', async () => {
  await prepareTryCanvas();
  await runTrySample(2);
  const normal = await page.evaluate(() => {
    const hud = document.getElementById('cvReplayHud');
    return {
      waves: hud.querySelectorAll('[data-hud-wave]').length,
      scrollWidth: hud.scrollWidth,
      clientWidth: hud.clientWidth
    };
  });
  assert.equal(normal.waves, 2);
  assert.ok(normal.scrollWidth <= normal.clientWidth + 1,
    'normal wave counts must not introduce HUD scrolling');

  await page.setViewportSize({ width: 1280, height: 800 });
  try {
    const eight = await page.evaluate(() => {
      const trace = [];
      for (let wave = 1; wave <= 8; wave += 1) {
        trace.push({
          event: 'wave', wave: wave, questions: ['capability'],
          duration_ms: 4, failed: false, error: null
        });
      }
      trace.push({
        event: 'final', target_id: 'power-target', kind: 'target', priority: 1,
        candidates: [{
          target_id: 'power-target', priority: 1, via_node: 'and-power', via_port: 'in_1', wave: 8
        }]
      });
      window.RTTryRun.apply(window.RTTryRun.rebuild(trace, window.App.graph));
      const hud = document.getElementById('cvReplayHud');
      const final = hud.querySelector('.rtc-hud-final');
      const waves = hud.querySelectorAll('[data-hud-wave]');
      const style = getComputedStyle(hud);
      const hudRect = hud.getBoundingClientRect();
      hud.scrollLeft = hud.scrollWidth;
      const finalRect = final.getBoundingClientRect();
      hud.scrollLeft = 0;
      const firstRect = waves[0].getBoundingClientRect();
      return {
        waves: waves.length,
        finalCount: hud.querySelectorAll('.rtc-hud-final').length,
        overflowX: style.overflowX,
        flexWrap: style.flexWrap,
        scrollWidth: hud.scrollWidth,
        clientWidth: hud.clientWidth,
        finalRight: finalRect.right,
        finalLeft: finalRect.left,
        firstLeft: firstRect.left,
        hudLeft: hudRect.left,
        hudRight: hudRect.right
      };
    });
    assert.equal(eight.waves, 8, 'the HUD dropped wave capsules at the 8-wave limit');
    assert.equal(eight.finalCount, 1, 'the HUD dropped the final target chip');
    assert.equal(eight.overflowX, 'auto', 'the HUD still clips instead of scrolling');
    assert.equal(eight.flexWrap, 'nowrap', 'normal single-row HUD layout changed');
    assert.ok(eight.scrollWidth > eight.clientWidth + 1,
      'an 8-wave HUD does not become horizontally scrollable');
    assert.ok(eight.finalRight <= eight.hudRight + 1,
      'the final chip is not reachable at max HUD scroll');
    assert.ok(eight.finalLeft >= eight.hudLeft - 1,
      'the final chip scrolled past the HUD viewport');
    assert.ok(eight.firstLeft >= eight.hudLeft - 1,
      'the first wave capsule is not reachable at scrollLeft=0');
  } finally {
    await page.evaluate(() => window.RTTryRun.clear());
    await page.setViewportSize({ width: 1440, height: 960 });
  }
});

test('Task 13 · s2 leaves the untriggered clarify question at 未触发 instead of zero', async () => {
  await prepareTryCanvas();
  await runTrySample(1);
  const snap = await trySnapshot();
  assert.equal(snap.final.target_id, 'light-target');
  assert.equal(snap.final.kind, 'target');
  assert.deepEqual(snap.final.candidates.map(candidate => candidate.target_id), ['light-target']);
  assert.equal(snap.waves.length, 1);
  assert.equal(snap.nodeStates.clarify.status, 'not-triggered');
  assert.equal(snap.nodeStates.clarify.wave, null);
  assert.equal(snap.nodeStates.clarify.ports, null);
  assert.equal(snap.nodes.clarify.replay, 'not-triggered');
  const notTriggered = await page.evaluate(() => window.t('cv.tryNotTriggered'));
  assert.equal(snap.nodes.clarify.state, notTriggered);
  assert.equal(snap.nodes.clarify.live.yes, '—', 'untriggered P(yes) was rendered as 0');
  assert.equal(snap.nodes.clarify.live.no, '—', 'untriggered P(no) was rendered as 0');
  assert.equal(snap.nodes.capability.selectedOptions.length, 1);
  assert.equal(snap.nodes.capability.selectedOptions[0], 'easy');
  assert.equal(snap.nodes['light-target'].replay, 'final');
});

test('Task 13 · §3.9 top-level p_top/margin win over the ports copy and never render undefined', async () => {
  await prepareTryCanvas();
  const proof = await page.evaluate(() => {
    const read = () => {
      const node = document.querySelector('#cvNodes [data-node-id="capability"]');
      const live = {};
      node.querySelectorAll('[data-live]').forEach(el => { live[el.dataset.live] = el.textContent.trim(); });
      return {
        pTop: live.p_top,
        margin: live.margin,
        text: node.textContent.replace(/\s+/g, ' ').trim()
      };
    };
    const base = { event: 'question', wave: 1, node_id: 'capability', duration_ms: 3, selected: 'strong' };
    const finalEvent = {
      event: 'final', target_id: 'power-target', kind: 'target', priority: 1,
      candidates: [{ target_id: 'power-target', priority: 1, via_node: 'capability', via_port: 'in_1', wave: 1 }]
    };
    /* 顶层字段是 §3.9 的权威值；ports 里的旧副本即使不同也不能覆盖。 */
    window.RTTryRun.apply(window.RTTryRun.rebuild([
      Object.assign({}, base, { ports: { p_top: 0.11, margin: 0.01 }, p_top: 0.84, margin: 0.72 }),
      finalEvent
    ], window.App.graph));
    const preferred = read();
    /* 没有顶层字段的旧 trace 才回退到 ports。 */
    window.RTTryRun.apply(window.RTTryRun.rebuild([
      Object.assign({}, base, { ports: { p_top: 0.66, margin: 0.5 } }),
      finalEvent
    ], window.App.graph));
    const fallback = read();
    return { preferred: preferred, fallback: fallback };
  });
  assert.equal(proof.preferred.pTop, '0.84', 'p_top did not prefer the question event top-level field');
  assert.equal(proof.preferred.margin, '0.72', 'margin did not prefer the question event top-level field');
  assert.equal(proof.fallback.pTop, '0.66', 'p_top did not fall back to the ports copy');
  assert.equal(proof.fallback.margin, '0.5', 'margin did not fall back to the ports copy');
  [proof.preferred.text, proof.fallback.text].forEach(text => {
    assert.equal(text.indexOf('undefined'), -1, 'question card rendered undefined for p_top/margin');
  });
  await page.evaluate(() => window.RTTryRun.clear());
});

test('Task 13 · s3/s4 replay two waves and the clarify slot condition hits', async () => {
  await prepareTryCanvas();
  await runTrySample(2);
  let snap = await trySnapshot();
  assert.equal(snap.waves.length, 2);
  assert.deepEqual(snap.waves[0].questions.sort(), ['capability', 'domain']);
  assert.deepEqual(snap.waves[1].questions, ['clarify']);
  assert.equal(snap.waves[1].failed, false);
  assert.ok(snap.waves[1].duration_ms > 0, 'second wave has no duration');
  assert.equal(snap.final.target_id, 'deep-target');
  assert.deepEqual(snap.final.candidates.map(candidate => candidate.target_id), ['deep-target', 'standard-target']);
  assert.equal(snap.nodeStates.clarify.status, 'done');
  assert.equal(snap.nodeStates.clarify.wave, 2);
  assert.equal(snap.slots.deepIn1.status, 'true');
  assertIncludes(snap.slots.deepIn1.live, '0.74');
  assertIncludes(snap.edges['e-clarify-deep'], 'is-true');
  assert.equal(snap.nodes['deep-target'].replay, 'final');
  assert.equal(snap.hudTitles.length, 2);
  assertIncludes(snap.hudTitles[1], '0.74');
  assertIncludes(snap.hudTitles[1], '> 0.6');

  await runTrySample(3);
  snap = await trySnapshot();
  assert.equal(snap.waves.length, 2);
  assert.equal(snap.final.target_id, 'deep-target');
  assertIncludes(snap.slots.deepIn1.live, '0.83');
  assert.equal(snap.nodes['deep-target'].replay, 'final');
});

test('Task 13 · s5 voids the failed wave, clears candidates and paints the amber error path', async () => {
  await prepareTryCanvas();
  await runTrySample(4);
  const snap = await trySnapshot();
  assert.equal(snap.final.kind, 'error_fallback');
  assert.equal(snap.final.target_id, 'light-target');
  assert.equal(snap.final.selection_reason, 'error_fallback');
  assert.deepEqual(snap.final.candidates, []);
  assert.deepEqual(snap.candidates, []);
  assert.equal(snap.waves.length, 1);
  assert.equal(snap.waves[0].failed, true);
  assert.ok(snap.waves[0].error, 'failed wave has no error text');
  assert.equal(snap.nodes['light-target'].replay, 'fallback');
  assertIncludes(snap.nodes['light-target'].classes, 'is-fallback');
  ['power-target', 'strong-target', 'deep-target', 'standard-target'].forEach(id => {
    assert.equal(snap.nodes[id].replay, 'not-triggered', `${id} was not voided by the failed wave`);
  });
  const errorEdges = Object.keys(snap.edges).filter(id => snap.edges[id].indexOf('is-error-path') >= 0);
  /* §6.8：只点亮回退目标 light-target 的入边，不能把此前所有 true 边都染成琥珀。 */
  assert.deepEqual(errorEdges, ['e-selected-easy-light'],
    'error fallback path must be exactly the fallback target incoming edge');
  assertIncludes(snap.edges['e-selected-easy-light'], 'is-error-path');
  assert.equal(snap.edges['e-start-capability'].indexOf('is-error-path'), -1,
    'an earlier true edge was painted as an error path');
  assertIncludes(snap.slots.lightIn1.classes, 'is-error-path',
    'the fallback target slot row did not join the amber chain');
  assert.equal(snap.resultKind, 'error_fallback');
  assertIncludes(snap.resultText, '模型目标-5');
  assertIncludes(snap.resultText, await page.evaluate(() => window.t('cv.tryKind.error')));
  await screenshot('s5-fallback-amber-1440.png');

  /* 清除 s5 后琥珀/非法 class 必须一条不剩（旧用例只清过 s1）。 */
  await page.locator('#cvTryClear').click();
  await page.waitForFunction(() => window.App.replay === null &&
    document.getElementById('cvTryResult').hidden === true);
  const cleared = await page.evaluate(() => ({
    replay: window.App.replay,
    errorPaths: document.querySelectorAll('.is-error-path').length,
    invalid: document.querySelectorAll('.is-invalid').length,
    slotStatuses: document.querySelectorAll('#cvNodes .rtc-slot[data-status]').length,
    edgeStatuses: document.querySelectorAll('#cvEdges .rtc-edge[data-status]').length,
    failedNodes: document.querySelectorAll('#cvNodes .rtc-node.is-fail').length,
    fallbackNodes: document.querySelectorAll('#cvNodes .rtc-node.is-fallback').length,
    hasDrawer: document.body.classList.contains('has-drawer'),
    drawerHidden: document.getElementById('cvTraceDrawer').hidden
  }));
  assert.equal(cleared.replay, null);
  assert.equal(cleared.errorPaths, 0, 'clear left is-error-path classes behind');
  assert.equal(cleared.invalid, 0, 'clear left is-invalid classes behind');
  assert.equal(cleared.slotStatuses, 0, 'clear left slot data-status attributes behind');
  assert.equal(cleared.edgeStatuses, 0, 'clear left edge data-status attributes behind');
  assert.equal(cleared.failedNodes, 0);
  assert.equal(cleared.fallbackNodes, 0);
  assert.equal(cleared.hasDrawer, false);
  assert.equal(cleared.drawerHidden, true);
});

test('Task 13 · a failed /admin/try toasts the api error and fabricates no replay', async () => {
  await prepareTryCanvas();
  const matcher = url => url.pathname === '/admin/try';
  await page.route(matcher, route => route.fulfill({
    status: 503,
    contentType: 'application/json',
    body: JSON.stringify({ detail: 'routing backend unavailable' })
  }));
  try {
    expectHttpStatusError({ url: /\/admin\/try$/, status: /503/ });
    await page.locator('[data-try-sample="0"]').click();
    await page.waitForFunction(() => {
      const host = document.getElementById('cvToast');
      return !!host && host.textContent.indexOf('routing backend unavailable') >= 0;
    }, null, { timeout: 10000 });
    await page.waitForTimeout(250);
    const after = await page.evaluate(() => ({
      replay: window.App.replay,
      running: window.App.tryRunning,
      runDisabled: document.getElementById('cvTryRun').disabled,
      resultHidden: document.getElementById('cvTryResult').hidden,
      hudHidden: document.getElementById('cvReplayHud').hidden,
      svgReplay: document.getElementById('cvEdges').classList.contains('is-replay'),
      finalNodes: document.querySelectorAll('#cvNodes .rtc-node.is-final').length,
      lastToast: window.App.lastToast
    }));
    assert.equal(after.replay, null, 'a failed request fabricated a replay result');
    assert.equal(after.running, false);
    assert.equal(after.runDisabled, false, 'the run button stayed disabled after a failed request');
    assert.equal(after.resultHidden, true);
    assert.equal(after.hudHidden, true);
    assert.equal(after.svgReplay, false);
    assert.equal(after.finalNodes, 0);
    assertIncludes(after.lastToast, 'routing backend unavailable');
  } finally {
    resetExpectedHttpStatusErrors();
    await page.unroute(matcher);
  }
});

test('trace-drawer-row-locates-slot', async () => {
  await prepareTryCanvas();
  await runTrySample(0);
  assert.equal(await page.evaluate(() => document.getElementById('cvTraceDrawer').hidden), true,
    'drawer should stay closed until the user opens it');
  await page.locator('#cvTryDrawerBtn').click();
  await page.waitForSelector('#cvTraceDrawer:not([hidden])', { timeout: 4000 });
  const drawer = await trySnapshot();
  assert.equal(drawer.drawerHidden, false);
  assert.equal(drawer.drawerWaveCount, 1, 'drawer must show one wave card per executed wave');
  const waveText = drawer.drawerWaveTexts[0];
  assertIncludes(waveText, await page.evaluate(() => window.tf('cv.tryWave', { n: 1 })));
  assertIncludes(waveText, await page.evaluate(() => window.tf('cv.tryWaveQuestions', { n: 2 })));
  assertIncludes(waveText, 'ms');
  assertIncludes(waveText, '0.84');
  assertIncludes(waveText, '> 0.65');
  const row = page.locator('#cvTraceDrawer [data-trace-edge="e-cap-strong-and-power"]')
    .filter({ hasText: '0.84' }).first();
  assert.equal(await row.count(), 1, 'drawer has no slot row for the and-power in_1 edge');
  const rowText = await row.innerText();
  assertIncludes(rowText, 'in_1', 'drawer slot row lost the slot port (in_1)');
  assertIncludes(rowText, '0.84');
  assertIncludes(rowText, '> 0.65');
  assertIncludes(rowText, 'true');
  assert.equal(await row.getAttribute('role'), 'button', 'drawer slot row is not exposed as a button');
  assert.equal(await row.getAttribute('tabindex'), '0', 'drawer slot row is not keyboard focusable');
  await row.click();
  await page.waitForFunction(() => {
    const selection = window.App.kit.getSelection();
    return selection.kind === 'edge' && selection.id === 'e-cap-strong-and-power';
  });
  assert.equal(await page.evaluate(() => document.getElementById('cvCondPop').hidden), false,
    'clicking a slot row did not open the condition popup');
  assert.equal(await page.evaluate(() => window.RTCondPop.currentEdgeId()), 'e-cap-strong-and-power');
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => document.getElementById('cvCondPop').hidden === true);

  /* §6.11：同一行必须能用 Enter / Space 触发，不必依赖鼠标。 */
  await row.focus();
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => document.getElementById('cvCondPop').hidden === false &&
    window.App.kit.getSelection().id === 'e-cap-strong-and-power');
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => document.getElementById('cvCondPop').hidden === true);
  await row.focus();
  await page.keyboard.press('Space');
  await page.waitForFunction(() => document.getElementById('cvCondPop').hidden === false &&
    window.RTCondPop.currentEdgeId() === 'e-cap-strong-and-power');
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => document.getElementById('cvCondPop').hidden === true);

  await page.locator('#cvTraceDrawer [data-try-act="drawer-close"]').click();
  await page.waitForFunction(() => document.getElementById('cvTraceDrawer').hidden === true);
  assert.equal(await page.evaluate(() => !!window.App.replay), true,
    'closing the drawer cleared the replay');
});

test('Task 13 · Esc closes the drawer and a later render does not reopen it', async () => {
  await prepareTryCanvas();
  await runTrySample(2);
  const closedBottom = await page.evaluate(() =>
    parseFloat(getComputedStyle(document.getElementById('cvTryBar')).bottom));
  await page.locator('#cvTryDrawerBtn').click();
  await page.waitForSelector('#cvTraceDrawer:not([hidden])');
  const opened = await page.evaluate(() => ({
    bottom: parseFloat(getComputedStyle(document.getElementById('cvTryBar')).bottom),
    hasDrawer: document.body.classList.contains('has-drawer'),
    open: window.RTTryRun.isDrawerOpen()
  }));
  assert.equal(opened.hasDrawer, true, 'opening the drawer did not set body.has-drawer');
  assert.equal(opened.open, true, 'opening the drawer did not set the RTTryRun drawer state');
  assert.ok(opened.bottom > closedBottom + 100,
    `drawer did not lift the try bar (${opened.bottom} vs ${closedBottom})`);

  await page.keyboard.press('Escape');
  await page.waitForFunction(() => document.getElementById('cvTraceDrawer').hidden === true &&
    !document.body.classList.contains('has-drawer'));
  const closed = await page.evaluate(() => ({
    bottom: parseFloat(getComputedStyle(document.getElementById('cvTryBar')).bottom),
    hasDrawer: document.body.classList.contains('has-drawer'),
    open: window.RTTryRun.isDrawerOpen(),
    replay: !!window.App.replay,
    drawerHidden: document.getElementById('cvTraceDrawer').hidden
  }));
  assert.equal(closed.drawerHidden, true);
  assert.equal(closed.hasDrawer, false, 'Esc hid the drawer but left body.has-drawer');
  assert.equal(closed.open, false, 'Esc did not clear the RTTryRun drawer state');
  assert.ok(Math.abs(closed.bottom - closedBottom) < 0.5,
    `Esc did not restore the try bar offset (${closed.bottom} vs ${closedBottom})`);
  assert.equal(closed.replay, true, 'closing the drawer cleared the replay result');

  /* 语言切换会走 renderAll → RTTryRun.apply；已关闭的抽屉不得被重新弹开。 */
  await page.evaluate(() => window.setLang('en'));
  await page.waitForFunction(() => document.documentElement.lang === 'en');
  const rerendered = await page.evaluate(() => ({
    drawerHidden: document.getElementById('cvTraceDrawer').hidden,
    hasDrawer: document.body.classList.contains('has-drawer'),
    open: window.RTTryRun.isDrawerOpen(),
    replay: !!window.App.replay
  }));
  assert.equal(rerendered.drawerHidden, true, 'a renderAll reopened the drawer after Esc');
  assert.equal(rerendered.hasDrawer, false, 'a renderAll restored body.has-drawer after Esc');
  assert.equal(rerendered.open, false);
  assert.equal(rerendered.replay, true, 'a renderAll dropped the replay after closing the drawer');
  await page.evaluate(() => window.setLang('zh'));
  await page.waitForFunction(() => document.documentElement.lang === 'zh-CN');
});

test('7.4-19-rerun-and-clear', async () => {
  await prepareTryCanvas();
  await runTrySample(0);
  const first = await page.evaluate(() => ({
    id: window.App.replay.id,
    target: window.App.replay.final.target_id,
    now: window.App.replay.now
  }));
  assert.equal(first.now, TRY_SAMPLES[0].now);
  await page.locator('#cvTryRerun').click();
  await page.waitForFunction(previous => {
    const replay = window.App.replay;
    return replay && !window.App.tryRunning && replay.id !== previous;
  }, first.id, { timeout: 15000 });
  const second = await page.evaluate(() => ({
    id: window.App.replay.id,
    target: window.App.replay.final.target_id,
    now: window.App.replay.now,
    text: window.App.replay.text
  }));
  assert.notEqual(second.id, first.id, 'rerun did not produce a new result');
  assert.equal(second.target, first.target, 'rerun changed the conclusion');
  assert.equal(second.now, first.now, 'rerun did not keep the scenario now');
  assert.equal(second.text, TRY_SAMPLES[0].text);

  await page.locator('#cvTryClear').click();
  const cleared = await page.evaluate(() => ({
    replay: window.App.replay,
    resultHidden: document.getElementById('cvTryResult').hidden,
    drawerHidden: document.getElementById('cvTraceDrawer').hidden,
    hudHidden: document.getElementById('cvReplayHud').hidden,
    svgReplay: document.getElementById('cvEdges').classList.contains('is-replay'),
    finalNodes: document.querySelectorAll('#cvNodes .rtc-node.is-final').length,
    trueEdges: document.querySelectorAll('#cvEdges .rtc-edge.is-true').length,
    runDisabled: document.getElementById('cvTryRun').disabled
  }));
  assert.equal(cleared.replay, null);
  assert.equal(cleared.resultHidden, true);
  assert.equal(cleared.drawerHidden, true);
  assert.equal(cleared.hudHidden, true);
  assert.equal(cleared.svgReplay, false, 'clearing replay left the SVG replay class');
  assert.equal(cleared.finalNodes, 0, 'clearing replay left final node classes');
  assert.equal(cleared.trueEdges, 0, 'clearing replay left highlighted edges');
  assert.equal(cleared.runDisabled, false);

  await runTryManual(TRY_SAMPLES[0].text);
  assert.equal(await page.evaluate(() => window.App.replay.now), null,
    'a manual run must not send a fixed now');
  assert.equal((await trySnapshot()).final.target_id, 'power-target');
});

test('Task 13 · any edit after a result clears the replay immediately', async () => {
  await prepareTryCanvas();
  await runTrySample(2);
  assert.equal(await page.evaluate(() => !!window.App.replay), true);
  await dragNodeBy('start', 58, 34);
  const after = await page.evaluate(() => ({
    replay: window.App.replay,
    resultHidden: document.getElementById('cvTryResult').hidden,
    drawerHidden: document.getElementById('cvTraceDrawer').hidden,
    hudHidden: document.getElementById('cvReplayHud').hidden,
    svgReplay: document.getElementById('cvEdges').classList.contains('is-replay'),
    finalNodes: document.querySelectorAll('#cvNodes .rtc-node.is-final').length,
    kind: window.App.kind()
  }));
  assert.equal(after.replay, null, 'dragging a node did not clear the replay state');
  assert.equal(after.resultHidden, true);
  assert.equal(after.drawerHidden, true);
  assert.equal(after.hudHidden, true);
  assert.equal(after.svgReplay, false);
  assert.equal(after.finalNodes, 0);
  assert.equal(after.kind, 'layout');
});

test('layout-by-waves-is-layout-only', async () => {
  await prepareTryCanvas();
  await runTrySample(2);
  const before = await page.evaluate(() => ({
    canonical: window.RTGraph.behaviorCanonical(window.App.tree),
    positions: Object.fromEntries(window.App.graph.nodes.map(node => [node.id, {
      x: node.position.x, y: node.position.y
    }])),
    undo: window.App.kit.canUndo()
  }));
  const refused = await page.evaluate(() => {
    const originalChange = window.App.change;
    const originalToast = window.App.toast;
    const toasts = [];
    window.App.change = () => false;
    window.App.toast = message => { toasts.push(String(message)); };
    let result = null;
    try {
      result = window.RTTryRun.layoutByWaves();
    } finally {
      window.App.change = originalChange;
      window.App.toast = originalToast;
    }
    return { result: result, toasts: toasts, done: window.t('cv.tryWavesLayoutDone') };
  });
  assert.equal(refused.result, false, 'layoutByWaves claimed success after App.change refused');
  assert.equal(refused.toasts.indexOf(refused.done), -1,
    'layoutByWaves toasted success after App.change refused');

  const moved = await page.evaluate(() => window.RTTryRun.layoutByWaves());
  assert.equal(moved, true, 'layoutByWaves refused to run with a replay result');
  const after = await page.evaluate(() => ({
    canonical: window.RTGraph.behaviorCanonical(window.App.tree),
    kind: window.App.kind(),
    replay: window.App.replay,
    nodes: Object.fromEntries(window.App.graph.nodes.map(node => [node.id, {
      x: node.position.x, y: node.position.y
    }])),
    undo: window.App.kit.canUndo()
  }));
  assert.equal(after.canonical, before.canonical, 'wave layout changed behavior canonical');
  assert.equal(after.kind, 'layout', 'wave layout was recorded as a behavior change');
  assert.equal(after.replay, null, 'wave layout edit did not clear the stale replay');
  assert.equal(after.undo, true, 'wave layout did not push a history entry');
  const changed = Object.keys(before.positions).some(id =>
    before.positions[id].x !== after.nodes[id].x || before.positions[id].y !== after.nodes[id].y);
  assert.equal(changed, true, 'wave layout did not move any node');

  const x = id => after.nodes[id].x;
  const y = id => after.nodes[id].y;
  assert.equal(x('start'), x('cond-text'));
  assert.equal(x('cond-text'), x('cond-peak'));
  assert.equal(x('capability'), x('domain'));
  assert.equal(x('capability'), x('and-power'));
  assert.equal(x('and-power'), x('not-cap'));
  assert.equal(x('and-power'), x('and-follow'));
  assert.equal(x('and-follow'), x('standard-target'));
  assert.equal(x('clarify'), x('deep-target'));
  assert.ok(x('capability') > x('start'));
  assert.ok(x('clarify') > x('capability'));
  const untriggered = ['light-target', 'power-target', 'strong-target'];
  untriggered.forEach(id => {
    assert.equal(x(id), x('light-target'), `${id} is not in the untriggered column`);
  });
  assert.ok(x('light-target') > x('clarify'), 'untriggered column is not after every wave column');
  const untriggeredOrder = untriggered.slice().sort((a, b) => y(a) - y(b));
  assert.deepEqual(untriggeredOrder, untriggered, 'untriggered column is not sorted by node id');
  const column = ['capability', 'domain', 'and-power', 'not-cap', 'and-follow', 'standard-target']
    .sort((a, b) => y(a) - y(b));
  assert.deepEqual(column, column.slice().sort(),
    'a wave column is not sorted by node id (same column must be id-ascending)');

  /* 未触发列头必须存在、跟在未触发列 x 上，并随语言切换重译。 */
  const header = await page.evaluate(() => {
    const el = document.querySelector('#cvWorld .rtc-wavehead');
    const node = window.App.graph.nodes.find(item => item.id === 'light-target');
    return el ? {
      text: el.textContent,
      left: parseFloat(el.style.left),
      top: parseFloat(el.style.top),
      nodeX: node.position.x,
      nodeY: node.position.y,
      expected: window.t('cv.tryUntriggeredColumn')
    } : null;
  });
  assert.ok(header, 'wave layout did not label the untriggered column');
  assert.equal(header.text, header.expected, 'untriggered column header is not localized');
  assert.equal(header.left, header.nodeX, 'untriggered column header is not above its column');
  assert.ok(header.top < header.nodeY, 'untriggered column header is not above the cards');
  await page.evaluate(() => window.setLang('en'));
  await page.waitForFunction(() => document.documentElement.lang === 'en');
  const englishHeader = await page.evaluate(() => ({
    text: document.querySelector('#cvWorld .rtc-wavehead').textContent,
    expected: window.t('cv.tryUntriggeredColumn')
  }));
  assert.equal(englishHeader.text, englishHeader.expected,
    'untriggered column header did not retranslate on language switch');
  await page.evaluate(() => window.setLang('zh'));
  await page.waitForFunction(() => document.documentElement.lang === 'zh-CN');

  /* 第二次整理没有移动：不得入历史、不得清回放、不得报“已整理”，一次 undo 必须回到旧布局。 */
  await runTrySample(2);
  const again = await page.evaluate(() => ({
    result: window.RTTryRun.layoutByWaves(),
    toast: window.App.lastToast,
    same: window.t('cv.tryWavesLayoutSame'),
    replay: !!window.App.replay
  }));
  assert.equal(again.result, true, 'a no-op wave layout should still succeed');
  assert.equal(again.replay, true, 'a no-op wave layout cleared the replay');
  assert.equal(again.toast, again.same, 'a no-op wave layout toasted the moved copy');
  await page.locator('#cvUndo').click();
  await page.waitForFunction(previous => {
    const node = window.App.graph.nodes.find(item => item.id === 'start');
    return Math.abs(node.position.x - previous.x) < 0.01 && Math.abs(node.position.y - previous.y) < 0.01;
  }, before.positions.start);
  assert.equal(await page.evaluate(() => !!document.querySelector('#cvWorld .rtc-wavehead')), false,
    'undo left a stale untriggered column header');
});

test('7.4-18-replay-data-from-admin-try', async () => {
  await prepareTryCanvas();
  const installed = await page.evaluate(() => {
    window.__rtEvalCalls = 0;
    window.__rtEvalOriginal = window.RTGraph.evaluateCondition;
    window.RTGraph.evaluateCondition = function () {
      window.__rtEvalCalls += 1;
      throw new Error('replay must never evaluate conditions locally');
    };
    try {
      window.RTGraph.evaluateCondition({}, {}, 1);
      return false;
    } catch (error) {
      /* 探针本身会计一次；清零后才是回放期间的调用次数。 */
      window.__rtEvalCalls = 0;
      return true;
    }
  });
  assert.equal(installed, true, 'the evaluateCondition guard was not installed');
  try {
    await runTrySample(2);
    const proof = await page.evaluate(() => ({
      calls: window.__rtEvalCalls,
      final: window.App.replay.final.target_id,
      waves: window.App.replay.waves.length,
      clarify: window.App.replay.nodeStates.clarify.status,
      deepSlot: window.App.replay.edgeStates['e-clarify-deep'].status,
      resultHidden: document.getElementById('cvTryResult').hidden,
      finalNodes: document.querySelectorAll('#cvNodes .rtc-node.is-final').length
    }));
    assert.equal(proof.calls, 0, 'replay called RTGraph.evaluateCondition behind the trace');
    assert.equal(proof.final, 'deep-target', 'trace-only replay did not finish');
    assert.equal(proof.waves, 2);
    assert.equal(proof.clarify, 'done');
    assert.equal(proof.deepSlot, 'true');
    assert.equal(proof.resultHidden, false);
    assert.ok(proof.finalNodes >= 1);
  } finally {
    await page.evaluate(() => {
      window.RTGraph.evaluateCondition = window.__rtEvalOriginal;
      delete window.__rtEvalOriginal;
      delete window.__rtEvalCalls;
    });
  }
});

test('Task 13 · replay copy is bilingual and leaves no raw i18n keys', async () => {
  await prepareTryCanvas();
  await runTrySample(0);
  await page.locator('#cvTryDrawerBtn').click();
  await page.waitForSelector('#cvTraceDrawer:not([hidden])');
  const chinese = await page.evaluate(() => ({
    result: document.getElementById('cvTryResult').textContent.replace(/\s+/g, ' ').trim(),
    priorityMeta: window.tf('cv.tryPriorityMeta', { priority: 5 })
  }));
  assertIncludes(chinese.result, chinese.priorityMeta, 'result meta still hardcodes priority');
  const english = await page.evaluate(() => {
    window.setLang('en');
    return {
      lang: document.documentElement.lang,
      result: document.getElementById('cvTryResult').textContent.replace(/\s+/g, ' ').trim(),
      drawer: document.getElementById('cvTraceDrawer').textContent.replace(/\s+/g, ' ').trim(),
      hud: document.getElementById('cvReplayHud').textContent.replace(/\s+/g, ' ').trim(),
      clear: document.getElementById('cvTryClear').textContent.trim(),
      finalLabel: window.t('cv.tryFinal'),
      drawerLabel: window.t('cv.tryDrawer'),
      waveLabel: window.tf('cv.tryWave', { n: 1 }),
      priorityMeta: window.tf('cv.tryPriorityMeta', { priority: 5 }),
      drawerAria: document.getElementById('cvTraceDrawer').getAttribute('aria-label'),
      inspectorTitle: document.getElementById('cvInspToggle').title,
      paletteTitle: document.getElementById('cvPalToggle').title,
      inspectorToggle: window.t('cv.inspectorToggle'),
      paletteToggle: window.t('cv.paletteToggle'),
      cjkPunctuation: /[；、]/.test(document.getElementById('cvTryResult').textContent +
        document.getElementById('cvTraceDrawer').textContent +
        document.getElementById('cvReplayHud').textContent),
      rawKey: /cv\.[a-zA-Z]/.test(document.getElementById('cvTryResult').textContent +
        document.getElementById('cvTraceDrawer').textContent +
        document.getElementById('cvReplayHud').textContent)
    };
  });
  assert.equal(english.lang, 'en');
  assert.equal(english.clear, await page.evaluate(() => window.t('cv.tryClear')));
  assertIncludes(english.result, english.finalLabel);
  assertIncludes(english.result, english.priorityMeta);
  assertIncludes(english.drawer, english.drawerLabel);
  assertIncludes(english.drawer, english.waveLabel);
  assert.equal(english.drawerAria, english.drawerLabel, 'drawer aria-label is not localized');
  assert.equal(english.inspectorTitle, english.inspectorToggle, 'inspector toggle tooltip is not localized');
  assert.equal(english.paletteTitle, english.paletteToggle, 'palette toggle tooltip is not localized');
  assert.equal(english.cjkPunctuation, false, 'English replay UI leaked CJK separators');
  assert.equal(english.rawKey, false, 'replay UI leaked raw i18n keys in English');
  assert.notEqual(english.finalLabel, 'cv.tryFinal');
  await page.evaluate(() => window.setLang('zh'));
  await page.waitForFunction(() => document.documentElement.lang === 'zh-CN');
  assert.equal(await page.evaluate(() => !!window.App.replay), true,
    'language switch dropped the replay');
  await screenshot('task13-replay-s3-drawer.png');
});

/* ── Task 14 · 最终验收矩阵（§7.4 补充项 / 极限图 / 响应式 / 中文） ── */

test('7.4-11-undo-redo-buttons-and-keyboard', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const original = await page.evaluate(() => {
    const node = window.App.graph.nodes.find(item => item.id === 'start');
    return { x: node.position.x, y: node.position.y };
  });
  const moved = await dragNodeBy('start', 46, 26);
  assert.notDeepEqual(moved, original);
  assert.equal(await page.evaluate(() => window.App.kit.canUndo()), true);
  const expectStart = position => page.waitForFunction(expected => {
    const node = window.App.graph.nodes.find(item => item.id === 'start');
    return node && node.position.x === expected.x && node.position.y === expected.y;
  }, position, { timeout: 4000 });

  await page.locator('#cvUndo').click();
  await expectStart(original);
  await page.locator('#cvRedo').click();
  await expectStart(moved);
  await page.keyboard.press(`${MOD}+z`);
  await expectStart(original);
  await page.keyboard.press(`${MOD}+Shift+z`);
  await expectStart(moved);
});

test('quick-create-compatible-node', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const before = await appSnapshot();
  const point = await blankPoint();
  assert.ok(point, 'no blank canvas point for quick create');
  await dragPort('capability', 'prob.easy', point);
  await page.waitForFunction(() => !document.getElementById('cvQuick').hidden, null, { timeout: 3000 });
  const menu = await page.evaluate(() => ({
    kinds: Array.from(document.querySelectorAll('#cvQuick [data-qkind]')).map(el => el.dataset.qkind),
    head: document.querySelector('#cvQuick .rtc-quick-h').textContent,
    quick: window.App.quick
      ? { source: window.App.quick.sourceNodeId, port: window.App.quick.sourcePort }
      : null
  }));
  /* 快速创建只列出「有输入槽位、且当前输出类型可连」的节点；条件/起点不在列。 */
  assert.deepEqual(menu.kinds, [
    'question-choice', 'question-noul', 'question-score',
    'logic-and', 'logic-or', 'logic-not', 'target'
  ]);
  assert.deepEqual(menu.quick, { source: 'capability', port: 'prob.easy' });
  assertIncludes(menu.head, 'capability');

  await page.locator('#cvQuick [data-qkind="target"]').click();
  const after = await appSnapshot();
  assert.equal(after.nodes.length, before.nodes.length + 1);
  assert.equal(after.edges.length, before.edges.length + 1);
  const created = after.nodes.find(node => !before.nodes.some(previous => previous.id === node.id));
  assert.equal(created.type, 'target');
  assert.equal(await page.evaluate(id =>
    window.RTGraph.findNode(window.App.graph, id).priority, created.id), 6,
  'a new target must take max(priority) + 1 under higher-wins');
  const edge = after.edges.find(item => item.target === created.id);
  assert.equal(edge.source, 'capability');
  assert.equal(edge.source_port, 'prob.easy');
  assert.equal(edge.target_port, 'in_1');
  assert.deepEqual(edge.condition, { op: 'gt', value: 0.5 });
  assert.equal(await page.locator('#cvQuick').isHidden(), true);
  assert.deepEqual(await page.evaluate(() => window.App.kit.getSelection()), { kind: 'edge', id: edge.id });
  assert.equal(await page.locator('#cvCondPop').isHidden(), false,
    'quick-creating from a numeric port did not open the condition editor');
  await page.keyboard.press('Escape');

  /* 布尔源：同一份兼容清单；新边默认 = true 且不弹比较编辑器。 */
  await openCanvas();
  const boolBefore = await appSnapshot();
  const boolPoint = await blankPoint();
  await dragPort('start', 'out', boolPoint);
  await page.waitForFunction(() => !document.getElementById('cvQuick').hidden, null, { timeout: 3000 });
  assert.deepEqual(await page.evaluate(() =>
    Array.from(document.querySelectorAll('#cvQuick [data-qkind]')).map(el => el.dataset.qkind)), [
    'question-choice', 'question-noul', 'question-score',
    'logic-and', 'logic-or', 'logic-not', 'target'
  ]);
  await page.locator('#cvQuick [data-qkind="question-noul"]').click();
  const boolAfter = await appSnapshot();
  const boolNode = boolAfter.nodes.find(node => !boolBefore.nodes.some(previous => previous.id === node.id));
  assert.equal(boolNode.type, 'question');
  assert.equal(boolNode.question_type, 'noul');
  const boolEdge = boolAfter.edges.find(item => item.target === boolNode.id);
  assert.ok(boolEdge, 'boolean quick create did not connect the new node');
  assert.equal(boolEdge.condition, null);
  assert.equal(await page.locator('#cvCondPop').isHidden(), true);
});

test('200-node-400-edge-smoke', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const doc = await page.evaluate(() => {
    const G = window.RTGraph;
    const graph = {
      id: 'root', type: 'graph', nodes: [], edges: [],
      otherwise_target: 't67', error_fallback: 't67'
    };
    graph.nodes.push({ id: 'start', type: 'start' });
    graph.nodes.push({ id: 'cond', type: 'condition', source: 'text', contains_any: ['故障'] });
    for (let i = 0; i < 32; i += 1) {
      graph.nodes.push({
        id: 'q' + i, type: 'question', question_type: 'choice',
        instructions: '极限图问题 ' + i + '：请选择最合适的能力档位。',
        options: [
          { key: 'easy', label: '轻量', capability: '简单问答' },
          { key: 'strong', label: '增强', capability: '复杂分析' }
        ]
      });
    }
    for (let i = 0; i < 98; i += 1) {
      graph.nodes.push({ id: 'l' + i, type: 'logic', operator: 'or' });
    }
    for (let i = 0; i < 68; i += 1) {
      graph.nodes.push({
        id: 't' + i, type: 'target',
        models: [{ model: 'deepseek-v3', reasoning: 'high' }], priority: i + 1
      });
    }
    let seq = 0;
    const addEdge = (source, sourcePort, target, targetPort) => {
      seq += 1;
      graph.edges.push({
        id: 'e' + seq, source, source_port: sourcePort,
        target, target_port: targetPort, condition: null
      });
    };
    /* 32：start → 每个问题；196：每个 OR 门由两个问题的 selected 端口各接一条。 */
    for (let i = 0; i < 32; i += 1) addEdge('start', 'out', 'q' + i, G.nextSlot(graph, 'q' + i));
    for (let i = 0; i < 98; i += 1) {
      const logic = 'l' + i;
      addEdge('q' + ((2 * i) % 32), 'selected.easy', logic, G.nextSlot(graph, logic));
      addEdge('q' + ((2 * i + 1) % 32), 'selected.strong', logic, G.nextSlot(graph, logic));
    }
    /* 68：前 68 个门 → target[i]；104：再按固定公式补齐到 400 边。 */
    for (let i = 0; i < 68; i += 1) {
      addEdge('l' + i, 'out', 't' + i, G.nextSlot(graph, 't' + i));
    }
    for (let i = 0; i < 104; i += 1) {
      const target = 't' + ((7 * i + 1) % 68);
      addEdge('l' + (i % 98), 'out', target, G.nextSlot(graph, target));
    }
    return {
      kind: 'sr-routing-graph', version: 3, name: '极限图 200 节点 / 400 边',
      timezone: 'Asia/Shanghai', sticky: { enabled: false, ttl_seconds: 1800 }, root: graph
    };
  });
  assert.equal(doc.root.nodes.length, 200, 'generator did not make 200 nodes');
  assert.equal(doc.root.edges.length, 400, 'generator did not make 400 edges');

  await setImportPayload(doc, 'extreme-200-400.json');
  await page.waitForFunction(() => window.App.graph.nodes.length === 200 &&
    window.App.graph.edges.length === 400, null, { timeout: 20000 });
  await page.waitForFunction(() =>
    document.querySelectorAll('#cvStage [data-node-id]').length === 200 &&
    document.querySelectorAll('#cvEdges path.rtc-edge').length === 400, null, { timeout: 20000 });

  const counts = await page.evaluate(() => {
    const graph = window.App.graph;
    const byType = {};
    graph.nodes.forEach(node => { byType[node.type] = (byType[node.type] || 0) + 1; });
    return {
      byType,
      nodes: graph.nodes.length,
      edges: graph.edges.length,
      startOut: graph.edges.filter(edge => edge.source === 'start').length,
      questionIn: graph.edges.filter(edge => edge.target.charAt(0) === 'q').length,
      logicIn: graph.edges.filter(edge => edge.target.charAt(0) === 'l').length,
      targetIn: graph.edges.filter(edge => edge.target.charAt(0) === 't').length,
      domNodes: document.querySelectorAll('#cvStage [data-node-id]').length,
      domEdges: document.querySelectorAll('#cvEdges path.rtc-edge').length,
      logicInputs: graph.nodes.filter(node => node.type === 'logic').map(node => ({
        id: node.id,
        inputs: graph.edges.filter(edge => edge.target === node.id).length
      })),
      hard: window.App.issues.filter(issue => issue.level !== 'hint').map(issue => issue.code),
      limit: window.App.issues.filter(issue => issue.code === 'limit_exceeded').length,
      priorities: graph.nodes.filter(node => node.type === 'target')
        .map(node => node.priority).sort((a, b) => a - b),
      fallbacks: [graph.otherwise_target, graph.error_fallback]
    };
  });
  assert.deepEqual(counts.byType, { start: 1, condition: 1, question: 32, logic: 98, target: 68 });
  assert.equal(counts.nodes, 200);
  assert.equal(counts.edges, 400);
  assert.equal(counts.startOut, 32);
  assert.equal(counts.questionIn, 32);
  assert.equal(counts.logicIn, 196);
  /* 不只看总数：每个 OR 门必须恰好 2 条输入，生成器的取模错误不会被 196 掩盖。 */
  assert.equal(counts.logicInputs.length, 98);
  const wrongGates = counts.logicInputs.filter(gate => gate.inputs !== 2);
  assert.deepEqual(wrongGates, [],
    'OR gates with a slot count other than 2: ' + JSON.stringify(wrongGates.slice(0, 5)));
  assert.equal(counts.targetIn, 172);
  assert.equal(counts.domNodes, 200, 'the DOM did not render 200 node cards');
  assert.equal(counts.domEdges, 400, 'the DOM did not render 400 edge paths');
  assert.equal(counts.limit, 0, 'the 200/400 graph reported limit_exceeded');
  assert.deepEqual(counts.hard, [], 'the extreme graph failed structural validation');
  assert.deepEqual(counts.priorities, Array.from({ length: 68 }, (_, index) => index + 1));
  assert.deepEqual(counts.fallbacks, ['t67', 't67']);

  /* 平移 / 滚轮缩放 / fit 仍有响应；不设置任何未经验证的帧率指标。 */
  const center = await page.evaluate(() => {
    const stage = document.getElementById('cvStage').getBoundingClientRect();
    const area = window.App.kit.safeArea(20);
    return { x: stage.left + area.x + area.w / 2, y: stage.top + area.y + area.h / 2 };
  });
  const viewport = await page.evaluate(() => window.App.kit.getViewport());
  await page.mouse.move(center.x, center.y);
  await page.mouse.down({ button: 'middle' });
  await page.mouse.move(center.x + 44, center.y + 26, { steps: 6 });
  await page.mouse.up({ button: 'middle' });
  const panned = await page.evaluate(() => window.App.kit.getViewport());
  assert.ok(Math.abs((panned.tx - viewport.tx) - 44) < 2, 'extreme graph ignored a pan');
  assert.ok(Math.abs((panned.ty - viewport.ty) - 26) < 2, 'extreme graph ignored a pan');
  const labelBefore = await page.locator('#cvZoomLabel').textContent();
  await page.mouse.move(center.x, center.y);
  await page.mouse.wheel(0, -240);
  await page.waitForFunction(previous => document.querySelector('#cvZoomLabel').textContent !== previous,
    labelBefore, { timeout: 3000 });
  const zoomed = await page.evaluate(() => window.App.kit.getViewport());
  assert.ok(zoomed.k > panned.k, 'wheel zoom did not react on the extreme graph');
  await page.locator('[data-act="zoom-fit"]').click();
  await page.waitForFunction(() => {
    const current = window.App.kit.getViewport();
    return Number.isFinite(current.tx) && Number.isFinite(current.ty) && current.k > 0;
  }, null, { timeout: 3000 });
  const fitted = await page.evaluate(() => ({
    viewport: window.App.kit.getViewport(),
    minK: window.RTCanvas.MIN_K
  }));
  assert.equal(Math.round(fitted.viewport.k * 1000), Math.round(fitted.minK * 1000),
    'the 200-node graph should clamp fit at the minimum zoom');
  await page.locator('[data-act="zoom-reset"]').click();
  await page.waitForFunction(() => document.querySelector('#cvZoomLabel').textContent === '100%');
  await screenshot('extreme-200-400.png');
});

test('condition-only-question-is-valid', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const doc = {
    kind: 'sr-routing-graph',
    version: 3,
    name: '条件可达性图',
    timezone: 'Asia/Shanghai',
    sticky: { enabled: false, ttl_seconds: 1800 },
    root: {
      id: 'root',
      type: 'graph',
      nodes: [
        { id: 'start', type: 'start' },
        { id: 'cond-gate', type: 'condition', source: 'text', contains_any: ['故障'] },
        {
          id: 'cond-q', type: 'question', question_type: 'choice',
          instructions: '仅由第 0 阶段条件节点触发的问题。',
          options: [
            { key: 'easy', label: '轻量', capability: '简单问答' },
            { key: 'strong', label: '增强', capability: '复杂分析' }
          ]
        },
        { id: 'target-cond', type: 'target', models: [{ model: 'qwen3-8b' }], priority: 1 }
      ],
      edges: [
        {
          id: 'e-cond-q', source: 'cond-gate', source_port: 'out',
          target: 'cond-q', target_port: 'in_1', condition: null
        },
        {
          id: 'e-q-target', source: 'cond-q', source_port: 'selected.easy',
          target: 'target-cond', target_port: 'in_1', condition: null
        }
      ],
      otherwise_target: 'target-cond',
      error_fallback: 'target-cond'
    }
  };
  /* 新建未启用的组再导入：保存走 POST /admin/trees，不会覆盖已启用的基线组。 */
  await page.locator('#cvNewTree').click();
  await page.waitForFunction(() => window.App.saved === null && window.App.graph.nodes.length === 2);
  await setImportPayload(doc, 'condition-only.json');
  await page.waitForFunction(() => window.App.graph.nodes.length === 4 && window.App.graph.edges.length === 2);
  const imported = await page.evaluate(() => ({
    hard: window.App.issues.filter(issue => issue.level !== 'hint').map(issue => issue.code),
    questionSources: window.App.graph.edges
      .filter(edge => edge.target === 'cond-q').map(edge => edge.source),
    startOut: window.App.graph.edges.filter(edge => edge.source === 'start').length
  }));
  assert.deepEqual(imported.hard, [], 'condition-only reachability was rejected by the client validator');
  assert.deepEqual(imported.questionSources, ['cond-gate'],
    'the fixture question must be fed by the condition node only');
  assert.equal(imported.startOut, 0,
    'the fixture start node must be isolated, so the question is only reachable from the condition');

  await saveNow();
  const treeId = await page.evaluate(() => window.App.treeId);
  const saved = await serverTree(treeId);
  assert.equal(saved.enabled, false);
  assert.equal(saved.root.nodes.length, 4);
  assert.equal(saved.root.edges.length, 2);
  assert.match(saved.tree_revision, /^[0-9a-f]{64}$/);
  assert.equal(saved.root.otherwise_target, 'target-cond');

  /* 只由第 0 阶段条件触发的问题也必须能被真链路执行：启用后走 /admin/try。 */
  await api('POST', '/admin/trees/' + encodeURIComponent(treeId) + '/enable');
  const tried = await api('POST', '/admin/try', {
    tree_id: treeId,
    text: '10kV 线路故障，请安排抢修。',
    now: S1_NOW
  });
  assert.equal(tried.outcome.type, 'hit', 'a condition-only question did not route');
  assert.equal(tried.outcome.target_id, 'target-cond',
    'the condition-only question did not reach its selected.easy target');
  assert.equal(tried.outcome.waves.length, 1, 'the condition-only graph needed more than one wave');
  await ensureBaselineTree();
});

test('layout-persists-across-save-and-reload', async () => {
  const baseline = await ensureBaselineTree();
  await openCanvas();
  const moved = await dragNodeBy('start', 54, 32);
  await saveNow();
  const saved = await page.evaluate(() => ({
    revision: window.App.tree.tree_revision,
    positions: Object.fromEntries(window.App.graph.nodes.map(node =>
      [node.id, { x: node.position.x, y: node.position.y }]))
  }));
  assert.equal(saved.revision, baseline.tree_revision, 'a layout-only save changed the revision');

  await page.reload();
  await page.waitForSelector('#cvStage [data-node-id]', { timeout: 20000 });
  await page.waitForFunction(() => window.App && window.App.booted && window.App.kit);
  await page.waitForFunction(() => window.App.changeKind === 'clean');
  const after = await page.evaluate(() => ({
    revision: window.App.tree.tree_revision,
    kind: window.App.kind(),
    badgeHidden: document.getElementById('cvDirty').hidden,
    positions: Object.fromEntries(window.App.graph.nodes.map(node =>
      [node.id, { x: node.position.x, y: node.position.y }]))
  }));
  assert.equal(after.revision, saved.revision, 'reload changed the tree revision');
  assert.equal(after.kind, 'clean');
  assert.equal(after.badgeHidden, true);
  Object.keys(saved.positions).forEach(id => {
    assert.ok(after.positions[id], `${id} disappeared after reload`);
    assert.ok(Math.abs(after.positions[id].x - saved.positions[id].x) < 0.01, `${id} X moved after reload`);
    assert.ok(Math.abs(after.positions[id].y - saved.positions[id].y) < 0.01, `${id} Y moved after reload`);
  });
  assert.ok(Math.abs(after.positions.start.x - moved.x) < 0.01, 'the dragged layout was not persisted');
});

test('try-uses-draft-timezone-and-sticky', async () => {
  await prepareTryCanvas();
  try {
    await page.evaluate(() => window.App.kit.clearSelection());
    await waitInspectorBranch('graph', 'root');
    await page.locator('#cvInspectorBody [data-field="timezone"]').fill('UTC');
    await page.locator('#cvInspectorBody [data-field="sticky"]').uncheck();
    await page.locator('#cvInspectorBody [data-field="stickyTtl"]').fill('600');
    await page.locator('#cvInspectorBody [data-act="ins-save-graph"]').click();
    await page.waitForFunction(() => window.App.tree.timezone === 'UTC' &&
      window.App.tree.sticky.enabled === false && window.App.tree.sticky.ttl_seconds === 600);

    const captured = page.waitForRequest(request =>
      request.method() === 'POST' && new URL(request.url()).pathname === '/admin/try');
    await page.locator('[data-try-sample="0"]').click();
    const payload = (await captured).postDataJSON();
    assert.equal(payload.timezone, 'UTC', 'try did not send the draft timezone');
    assert.deepEqual(payload.sticky, { enabled: false, ttl_seconds: 600 },
      'try did not send the draft sticky settings');
    assert.ok(payload.root && payload.root.nodes.length === EXAMPLE_NODES,
      'try did not send the draft graph');
    assert.equal(payload.now, TRY_SAMPLES[0].now);
    assert.equal(payload.session_id, '');

    await waitTryResult({ text: TRY_SAMPLES[0].text, now: TRY_SAMPLES[0].now });
    const trace = await page.evaluate(() => ({
      timezone: window.App.replay.meta.timezone,
      active: window.App.replay.meta.active_periods.slice()
    }));
    assert.equal(trace.timezone, 'UTC', 'the returned trace did not use the draft timezone');
    assert.deepEqual(trace.active, ['offpeak'], 'UTC 04:00 should resolve to the offpeak period');
  } finally {
    await page.evaluate(() => window.RTTryRun.clear()).catch(() => {});
    await ensureBaselineTree();
  }
});

/* ── Task 14 · final acceptance hardening ───────────────────────────── */

test('Task 14 · a malformed v3 import with non-array fields is rejected before applying', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const draftOf = () => page.evaluate(() => ({
    nodes: window.App.graph.nodes.map(node => node.id),
    edges: window.App.graph.edges.map(edge => edge.id),
    models: window.App.graph.nodes.find(node => node.type === 'target').models,
    kind: window.App.kind()
  }));
  const kept = await draftOf();
  await clearToasts();

  /* models: 5 曾在 cards.js 里直接 .map 崩溃：parseImport 必须在应用前拒绝。 */
  const malformed = JSON.parse(JSON.stringify(EXAMPLE));
  malformed.nodes.find(node => node.type === 'target').models = 5;
  const probe = await page.evaluate(payload => {
    const result = window.RTGraph.parseImport(JSON.stringify(payload));
    return {
      ok: result.ok,
      error: result.error,
      codes: (result.issues || []).map(issue => issue.code),
      zh: (result.issues || []).map(issue => issue.zh).join(' ')
    };
  }, {
    kind: 'sr-routing-graph', version: 3, name: '非法 models',
    timezone: BASELINE.timezone, sticky: BASELINE.sticky, root: malformed
  });
  assert.equal(probe.ok, false, 'parseImport accepted a non-array models field');
  assert.ok(probe.codes.includes('graph_parse_error'), 'missing graph_parse_error issue');
  assert.match(probe.zh, /models/);

  await setImportPayload({
    kind: 'sr-routing-graph', version: 3, name: '非法 models',
    timezone: BASELINE.timezone, sticky: BASELINE.sticky, root: malformed
  }, 'malformed-models.json');
  await waitToastIncludes(await page.evaluate(n => window.tf('cv.toastImportIssues', { n }), 1));
  assert.deepEqual(await draftOf(), kept, 'a rejected malformed v3 import changed the draft');
  assert.equal(await page.evaluate(() => window.App.issues.every(issue => issue.level === 'hint')), true,
    'the rejected file leaked hard issues into the current draft validation bar');
});

test('Task 14 · the only start node cannot be deleted', async () => {
  await ensureBaselineTree();
  await openCanvas();
  await clearToasts();
  await clickNode('start');
  await page.waitForFunction(() => window.App.kit.getSelection().kind === 'node' &&
    window.App.kit.getSelection().id === 'start');
  const before = await page.evaluate(() => ({
    nodes: window.App.graph.nodes.length,
    undo: window.App.kit.canUndo()
  }));
  await page.keyboard.press('Delete');
  await waitToastIncludes(await page.evaluate(() => window.t('cv.deleteStartBlocked')));
  assert.equal(await page.evaluate(() => document.getElementById('cvDeleteModal').hidden), true,
    'the delete dialog opened for the start node');
  assert.deepEqual(await page.evaluate(() => ({
    nodes: window.App.graph.nodes.length,
    hasStart: window.App.graph.nodes.some(node => node.id === 'start'),
    undo: window.App.kit.canUndo()
  })), { nodes: before.nodes, hasStart: true, undo: before.undo },
  'a start-node delete mutated the draft or pushed history');

  /* 检查器删除按钮走同一个 requestDelete 守卫。 */
  await clearToasts();
  await waitInspectorBranch('node', 'start');
  await page.locator('#cvInspectorBody [data-act="ins-delete-node"]').click();
  await waitToastIncludes(await page.evaluate(() => window.t('cv.deleteStartBlocked')));
  assert.equal(await page.evaluate(() => document.getElementById('cvDeleteModal').hidden), true,
    'the inspector delete button opened the dialog for the start node');
});

test('Task 14 · an in-flight run rejects condition and inspector saves instead of closing as success', async () => {
  await prepareTryCanvas();
  const edgeId = 'e-cap-strong-and-power';
  const original = await page.evaluate(id => {
    window.App.kit.select('edge', id);
    window.RTCondPop.open(id);
    return window.App.graph.edges.find(edge => edge.id === id).condition;
  }, edgeId);
  assert.deepEqual(original, { op: 'gt', value: 0.65 });

  let release = null;
  const gate = new Promise(resolve => { release = resolve; });
  const matcher = url => url.pathname === '/admin/try';
  await page.route(matcher, async route => { await gate; await route.continue(); });
  try {
    await page.locator('[data-try-sample="0"]').click();
    await page.waitForFunction(() => window.App.tryRunning === true, null, { timeout: 5000 });
    assert.equal(await page.evaluate(() => document.getElementById('cvCondPop').hidden), true,
      'starting a run left the condition editor open');

    /* 运行锁下仍强行打开并确认：change() 返回 false，浮层必须保持打开并报错。 */
    const conditionResult = await page.evaluate(id => {
      window.RTCondPop.open(id);
      const pop = document.getElementById('cvCondPop');
      pop.querySelector('[data-cond-field="value"]').value = '0.91';
      pop.querySelector('form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
      const box = pop.querySelector('[data-cond-error]');
      return {
        hidden: pop.hidden,
        error: box ? box.textContent : '',
        errorHidden: box ? box.hidden : true,
        value: window.App.graph.edges.find(edge => edge.id === id).condition.value
      };
    }, edgeId);
    assert.equal(conditionResult.hidden, false, 'the condition editor closed on a rejected save');
    assert.equal(conditionResult.value, 0.65, 'a rejected condition edit mutated the draft');
    assert.equal(conditionResult.errorHidden, false, 'no inline condition error was shown');
    assert.equal(conditionResult.error, await page.evaluate(() => window.t('cv.saveBlocked')));

    /* 检查器走同一条 App.change 守卫，也必须内联报错且不写草稿。 */
    const inspectorResult = await page.evaluate(() => {
      window.App.kit.select('node', 'capability');
      const body = document.getElementById('cvInspectorBody');
      const form = body.querySelector('[data-ins-form="node"]');
      form.querySelector('[data-field="instructions"]').value = '运行中不应写入的文本';
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
      const box = body.querySelector('[data-ins-error]');
      return {
        error: box ? box.textContent : '',
        hidden: box ? box.hidden : true,
        instructions: window.App.graph.nodes.find(node => node.id === 'capability').instructions
      };
    });
    assert.equal(inspectorResult.hidden, false, 'the inspector did not show an inline error');
    assert.equal(inspectorResult.error, await page.evaluate(() => window.t('cv.saveBlocked')));
    assert.notEqual(inspectorResult.instructions, '运行中不应写入的文本',
      'a rejected inspector save mutated the draft');
  } finally {
    release();
  }
  await waitTryResult({ text: TRY_SAMPLES[0].text });
  await page.evaluate(() => window.RTCondPop.close());
  await page.unroute(matcher);
});

test('Task 14 · an in-flight save is busy, blocks switching and discards a stale envelope', async () => {
  await ensureBaselineTree();
  await openCanvas();
  await dragNodeBy('start', 22, 12);
  let release = null;
  const gate = new Promise(resolve => { release = resolve; });
  const matcher = url => url.pathname === '/admin/trees/' + TREE_ID;
  await page.route(matcher, async route => { await gate; await route.continue(); });
  try {
    await page.locator('#cvSave').click();
    await page.waitForFunction(() => window.App.saving === true, null, { timeout: 5000 });
    const busy = await page.evaluate(() => ({
      saving: window.App.saving,
      disabled: document.getElementById('cvSave').disabled,
      ariaBusy: document.getElementById('cvSave').getAttribute('aria-busy'),
      label: document.querySelector('#cvSave span[data-i18n]').textContent,
      expectedLabel: window.t('cv.saving'),
      newDisabled: document.getElementById('cvNewTree').disabled,
      importDisabled: document.getElementById('cvImport').disabled
    }));
    assert.equal(busy.saving, true);
    assert.equal(busy.disabled, true, 'the save button stayed enabled while saving');
    assert.equal(busy.ariaBusy, 'true', 'the save button did not expose aria-busy');
    assert.equal(busy.label, busy.expectedLabel, 'the save button did not show the busy label');
    assert.equal(busy.newDisabled, true, 'New stayed enabled while saving');
    assert.equal(busy.importDisabled, true, 'Import stayed enabled while saving');

    /* 保存回包落地前切换草稿必须被拒绝。 */
    const switched = await page.evaluate(() => window.App.selectTree('other-tree'));
    assert.equal(switched, false, 'selectTree allowed a switch while saving');
    assert.equal(await page.evaluate(() => window.App.lastToast),
      await page.evaluate(() => window.t('cv.toastSavingBlocked')));

    /* 即使有代码绕过 selectTree 直接 loadTree，旧回包也不能写进新草稿。 */
    const raced = await page.evaluate(() => {
      const other = window.RTGraph.createMinimalTree('qwen3-8b');
      other.id = 'stale-race';
      other.name = 'raced tree';
      window.App.loadTree(other, true);
      return { id: window.App.treeId, name: window.App.tree.name };
    });
    assert.deepEqual(raced, { id: 'stale-race', name: 'raced tree' });
    release();
    await page.waitForFunction(() => window.App.saving === false, null, { timeout: 8000 });
    const after = await page.evaluate(() => ({
      id: window.App.treeId,
      name: window.App.tree.name,
      revision: window.App.tree.tree_revision || '',
      savedId: window.App.saved && window.App.saved.id
    }));
    assert.deepEqual(after, { id: 'stale-race', name: 'raced tree', revision: '', savedId: 'stale-race' },
      'a stale save envelope was applied to the freshly loaded draft');
  } finally {
    release();
  }
  await page.unroute(matcher);
  await ensureBaselineTree();
});

test('Task 14 · New, Import and Load example stay inert until the canvas is booted', async () => {
  let release = () => {};
  const gate = new Promise(resolve => { release = resolve; });
  const hold = async route => {
    await gate;
    try { await route.continue(); } catch (error) { /* the test already unrouted it */ }
  };
  await page.route('**/admin/trees', hold);
  try {
    await page.goto(routerBase + '/console/routing.html');
    await page.waitForFunction(() => window.App && window.App.kit && !window.App.booted,
      null, { timeout: 10000 });
    const notReady = await page.evaluate(() => window.t('cv.toastNotReady'));
    for (const id of ['cvNewTree', 'cvImport', 'cvLoadExample']) {
      await clearToasts();
      await page.locator('#' + id).click();
      await waitToastIncludes(notReady);
      assert.equal(await page.evaluate(() => window.App.tree), null,
        `#${id} mutated the draft before boot`);
      assert.equal(await page.evaluate(() => document.getElementById('cvConfirmModal').hidden), true,
        `#${id} opened a confirm dialog before boot`);
    }
    /* 早到的 change() 是安全的 false，不 clone(null)、不抛异常。 */
    const changeResult = await page.evaluate(() =>
      window.App.change('behavior', () => { throw new Error('mutate must not run before boot'); }));
    assert.equal(changeResult, false, 'change() before boot did not return false');
  } finally {
    release();
  }
  await page.waitForFunction(() => window.App.booted && window.App.tree, null, { timeout: 15000 });
  await page.unroute('**/admin/trees');
});

test('7.4-20-responsive-keyboard-and-visual', async () => {
  await ensureBaselineTree();
  await page.setViewportSize({ width: 1440, height: 960 });
  await openCanvas();
  const metrics = () => page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
    scrollHeight: document.documentElement.scrollHeight,
    clientHeight: document.documentElement.clientHeight
  }));
  let size = await metrics();
  assert.ok(size.scrollWidth <= size.clientWidth + 1,
    `1440 horizontal overflow: ${size.scrollWidth} > ${size.clientWidth}`);
  assert.ok(size.scrollHeight <= size.clientHeight + 1,
    `1440 vertical overflow: ${size.scrollHeight} > ${size.clientHeight}`);
  await screenshot('canvas-default-1440.png');

  /* fit 使用安全区：选中节点不能被节点库/检查器/缩放条/试算台/小地图盖住。 */
  await clickNode('deep-target');
  const fitInfo = await page.evaluate(() => {
    window.App.kit.fit({ padding: 24 });
    const stage = document.getElementById('cvStage').getBoundingClientRect();
    const area = window.App.kit.safeArea(12);
    const rect = document.querySelector('[data-node-id="deep-target"]').getBoundingClientRect();
    const node = {
      left: rect.left - stage.left,
      top: rect.top - stage.top,
      right: rect.right - stage.left,
      bottom: rect.bottom - stage.top
    };
    const overlays = ['cvPalette', 'cvInspector', 'cvZoom', 'cvTryBar', 'cvMinimap']
      .map(id => {
        const el = document.getElementById(id);
        if (!el || el.hidden) return null;
        const box = el.getBoundingClientRect();
        return {
          id, left: box.left, top: box.top,
          right: box.right, bottom: box.bottom
        };
      }).filter(Boolean);
    return { area, node, selection: window.App.kit.getSelection(), overlays, stage: { left: stage.left, top: stage.top } };
  });
  assert.deepEqual(fitInfo.selection, { kind: 'node', id: 'deep-target' });
  assert.ok(fitInfo.node.left >= fitInfo.area.x - 1 &&
    fitInfo.node.right <= fitInfo.area.x + fitInfo.area.w + 1,
  'fit placed the selected node outside the safe area horizontally');
  assert.ok(fitInfo.node.top >= fitInfo.area.y - 1 &&
    fitInfo.node.bottom <= fitInfo.area.y + fitInfo.area.h + 1,
  'fit placed the selected node outside the safe area vertically');
  const covered = fitInfo.overlays.some(box => {
    const rect = {
      left: fitInfo.stage.left + fitInfo.node.left,
      top: fitInfo.stage.top + fitInfo.node.top,
      right: fitInfo.stage.left + fitInfo.node.right,
      bottom: fitInfo.stage.top + fitInfo.node.bottom
    };
    return rect.left < box.right && rect.right > box.left &&
      rect.top < box.bottom && rect.bottom > box.top;
  });
  assert.equal(covered, false, 'the selected node is covered by an overlay after fit');

  /* Tab 导航：每个落点都有可见 focus 态。 */
  await page.evaluate(() => {
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  });
  const focusStates = [];
  for (let index = 0; index < 6; index += 1) {
    await page.keyboard.press('Tab');
    focusStates.push(await page.evaluate(() => {
      const el = document.activeElement;
      const style = getComputedStyle(el);
      return {
        tag: el.tagName,
        id: el.id,
        focusVisible: el.matches(':focus-visible'),
        outline: style.outlineStyle !== 'none' && style.outlineWidth !== '0px',
        shadow: style.boxShadow !== 'none'
      };
    }));
  }
  focusStates.forEach((state, index) => {
    assert.equal(state.focusVisible, true, `Tab stop ${index} (${state.tag}#${state.id}) is not focus-visible`);
    assert.ok(state.outline || state.shadow,
      `Tab stop ${index} (${state.tag}#${state.id}) has no visible focus state`);
  });

  /* 删除确认：焦点入场、Tab 圈定、Esc 关闭并把焦点还给触发按钮。 */
  await clickNode('capability');
  await waitInspectorBranch('node', 'capability');
  const trigger = '#cvInspectorBody [data-act="ins-delete-node"]';
  await page.locator(trigger).click();
  await page.waitForFunction(() => !document.getElementById('cvDeleteModal').hidden);
  const initial = await page.evaluate(() => ({
    inside: document.getElementById('cvDeleteBody').contains(document.activeElement),
    act: document.activeElement.dataset ? document.activeElement.dataset.act : ''
  }));
  assert.equal(initial.inside, true, 'the delete dialog did not move focus inside');
  assert.equal(initial.act, 'delete-cancel');
  /* 真正的环绕：从 confirm 按 Tab 回到 cancel，从 cancel 按 Shift+Tab 回到
     confirm。只来回按 Tab/Shift+Tab 而不跨过首尾，证明不了焦点陷阱。 */
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.activeElement.dataset.act), 'delete-confirm',
    'Tab from the cancel button did not reach the confirm button');
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.activeElement.dataset.act), 'delete-cancel',
    'Tab from the last control did not wrap forward to the first');
  await page.keyboard.press('Shift+Tab');
  assert.equal(await page.evaluate(() => document.activeElement.dataset.act), 'delete-confirm',
    'Shift+Tab from the first control did not wrap backward to the last');
  await page.keyboard.press('Shift+Tab');
  assert.equal(await page.evaluate(() => document.activeElement.dataset.act), 'delete-cancel');
  for (let index = 0; index < 5; index += 1) {
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() =>
      document.getElementById('cvDeleteBody').contains(document.activeElement)), true,
    'Tab escaped the delete dialog');
    await page.keyboard.press('Shift+Tab');
    assert.equal(await page.evaluate(() =>
      document.getElementById('cvDeleteBody').contains(document.activeElement)), true,
    'Shift+Tab escaped the delete dialog');
  }
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => document.getElementById('cvDeleteModal').hidden);
  const restored = await page.evaluate(() => ({
    act: document.activeElement.dataset ? document.activeElement.dataset.act : '',
    insideInspector: document.getElementById('cvInspectorBody').contains(document.activeElement),
    selection: window.App.kit.getSelection()
  }));
  assert.equal(restored.act, 'ins-delete-node', 'Esc did not return focus to the delete trigger');
  assert.equal(restored.insideInspector, true);
  assert.deepEqual(restored.selection, { kind: 'node', id: 'capability' });

  /* 1280×800：同一页无水平/垂直溢出，两个面板仍可见。 */
  await page.setViewportSize({ width: 1280, height: 800 });
  await openCanvas();
  size = await metrics();
  assert.ok(size.scrollWidth <= size.clientWidth + 1,
    `1280 horizontal overflow: ${size.scrollWidth} > ${size.clientWidth}`);
  assert.ok(size.scrollHeight <= size.clientHeight + 1,
    `1280 vertical overflow: ${size.scrollHeight} > ${size.clientHeight}`);
  const narrow = await page.evaluate(() => ({
    palette: document.getElementById('cvPalette').getBoundingClientRect().width > 0,
    inspector: document.getElementById('cvInspector').getBoundingClientRect().width > 0,
    zoom: /^\d+%$/.test(document.getElementById('cvZoomLabel').textContent)
  }));
  assert.deepEqual(narrow, { palette: true, inspector: true, zoom: true });
  await screenshot('canvas-1280.png');
  await page.setViewportSize({ width: 1440, height: 960 });
});

test('7.4-21-chinese-product-and-actionable-errors', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const language = await page.evaluate(() => ({
    lang: document.documentElement.lang,
    save: document.getElementById('cvSave').textContent.trim(),
    palette: document.querySelector('#cvPalette .rtc-pal-t span').textContent.trim(),
    tryRun: document.getElementById('cvTryRun').textContent.trim(),
    rawKey: /cv\.[a-zA-Z]/.test(document.body.textContent)
  }));
  assert.equal(language.lang, 'zh-CN');
  assert.equal(language.rawKey, false, 'the Chinese page leaked raw i18n keys');
  assert.match(language.save, /保存/);
  assert.match(language.palette, /节点/);
  assert.match(language.tryRun, /试算|运行/);

  /* 具体可操作：被占用槽位的拒绝点名「空槽位 / 下一个 in_N」，且不落边。 */
  await clearToasts();
  const before = (await appSnapshot()).edges.length;
  await dragPort('capability', 'prob.easy', { node: 'and-power', port: 'in_1' });
  await waitToastIncludes('空槽位');
  const occupied = await toastText();
  assert.match(occupied, /占用|已连接/);
  assert.match(occupied, /in_N/);
  assert.equal((await appSnapshot()).edges.length, before);

  /* 槽位条件的错误同样具体：文案来自词典、指出阈值非法，且不写回。 */
  const edgeId = 'e-cap-strong-and-power';
  const original = (await edgeOf(edgeId)).condition;
  await page.locator(`#cvNodes [data-cond-edge="${edgeId}"]`).click();
  await waitCondPop(edgeId);
  await page.locator('#cvCondPop [data-cond-field="value"]').fill('NaN');
  await page.locator('#cvCondPop [data-act="cond-confirm"]').click();
  assert.equal((await page.locator('#cvCondPop [data-cond-error]').textContent()).trim(),
    await page.evaluate(() => window.t('cv.cond.badValue')));
  assert.deepEqual((await edgeOf(edgeId)).condition, original);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#cvCondPop').isHidden(), true);

  /* 校验错误带节点/边/槽位定位字段，可直接跳到出问题的位置。 */
  await page.evaluate(() => {
    window.App.change('behavior', tree => {
      tree.root.edges.find(edge => edge.id === 'e-clarify-deep').condition = { op: 'bogus', value: 1 };
    });
  });
  const issue = await page.evaluate(() => {
    const found = window.App.issues.find(item => item.code === 'numeric_edge_condition_invalid');
    return found ? {
      zh: found.zh, edge: found.edge_id, node: found.node_id, port: found.port
    } : null;
  });
  assert.ok(issue, 'invalid slot condition produced no located issue');
  assert.match(issue.zh, /算子|阈值|条件/);
  assert.equal(issue.edge, 'e-clarify-deep');
  assert.equal(issue.node, 'deep-target');
  assert.equal(issue.port, 'in_1');
});

test('review-long-instruction-is-clamped-without-overlap', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const long = '你是一个模型路由器。下面是一条用户请求，请根据每个模型的能力说明选择最合适的模型。'.repeat(4);
  await page.evaluate(text => {
    window.App.change('behavior', tree => {
      window.RTGraph.findNode(tree.root, 'capability').instructions = text;
    });
  }, long);
  const metrics = await page.evaluate(() => {
    const card = document.querySelector('[data-node-id="capability"]');
    const instr = card.querySelector('.rtc-instr');
    const inner = instr.querySelector('.rtc-instr-t');
    const slot = card.querySelector('.rtc-slot');
    const cs = getComputedStyle(inner);
    const instrBox = instr.getBoundingClientRect();
    const innerBox = inner.getBoundingClientRect();
    const instrStyle = getComputedStyle(instr);
    return {
      clamp: String(cs.webkitLineClamp || cs.lineClamp || ''),
      overflow: cs.overflow,
      title: instr.getAttribute('title') || '',
      innerH: innerBox.height,
      innerScrollH: inner.scrollHeight,
      lineH: parseFloat(instrStyle.lineHeight),
      instrBottom: instrBox.bottom,
      slotTop: slot ? slot.getBoundingClientRect().top : null
    };
  });
  assert.equal(metrics.clamp, '2', 'instruction clamp is not two lines');
  assert.equal(metrics.overflow, 'hidden');
  assert.ok(metrics.innerH <= metrics.lineH * 2 + 1,
    `instruction exceeds two lines (${metrics.innerH}px > ${metrics.lineH * 2}px)`);
  assert.ok(metrics.innerScrollH > metrics.innerH,
    'instruction was not actually truncated (no clipped content)');
  assert.ok(metrics.title.length > 66, 'full instruction is not reachable via the title tooltip');
  if (metrics.slotTop != null) {
    assert.ok(metrics.instrBottom <= metrics.slotTop + 0.5,
      'instruction text overlaps the slot row');
  }
});

test('review-empty-slots-hidden-until-drag', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const hidden = await page.evaluate(() => {
    const ghosts = Array.from(document.querySelectorAll('#cvNodes .rtc-slot--ghost'));
    return {
      count: ghosts.length,
      visible: ghosts.filter(row => getComputedStyle(row).display !== 'none').length
    };
  });
  assert.ok(hidden.count > 0, 'test precondition: expected at least one empty slot in the graph');
  assert.equal(hidden.visible, 0, 'empty slots must be hidden at rest');
  const from = await portPoint('capability', 'prob.easy', 'out');
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  await page.mouse.move(from.x + 24, from.y + 12, { steps: 4 });
  const during = await page.evaluate(() => {
    const ghosts = Array.from(document.querySelectorAll('#cvNodes .rtc-slot--ghost'));
    return {
      visible: ghosts.filter(row => getComputedStyle(row).display !== 'none').length,
      stage: document.getElementById('cvStage').classList.contains('is-edge-drag')
    };
  });
  assert.ok(during.stage, 'edge drag state was not set');
  assert.ok(during.visible > 0, 'empty slots were not revealed while dragging an edge');
  await page.mouse.up();
  const after = await page.evaluate(() => ({
    visible: Array.from(document.querySelectorAll('#cvNodes .rtc-slot--ghost'))
      .filter(row => getComputedStyle(row).display !== 'none').length,
    stage: document.getElementById('cvStage').classList.contains('is-edge-drag')
  }));
  assert.equal(after.visible, 0, 'empty slots stayed visible after the drag ended');
  assert.equal(after.stage, false, 'edge drag state was not cleared');
});

test('review-inspector-adds-input-connection', async () => {
  await ensureBaselineTree();
  await openCanvas();
  /* 画布不再提供「+ 槽位」入口；新增输入槽位（= 新增入边）在检查器完成。 */
  assert.equal(await page.locator('#cvNodes .rtc-slot-add').count(), 0,
    'canvas still renders a slot-add affordance');
  await clickNode('and-power');
  const picker = '[data-ins-branch="node"] [data-slot-wrap="in_3"]';
  await page.locator(picker + ' [data-act="ins-slot-add"]').click();
  const select = page.locator(picker + ' [data-slot-source]');
  assert.equal(await select.isVisible(), true, 'inspector slot picker did not open');
  await screenshot('inspector-slots-1440.png');
  await select.selectOption({ index: 0 });
  await page.locator(picker + ' [data-act="ins-slot-create"]').click();
  await page.waitForFunction(() => window.App.graph.edges.some(edge =>
    edge.target === 'and-power' && edge.target_port === 'in_3'));
  const edge = await page.evaluate(() => window.App.graph.edges.find(item =>
    item.target === 'and-power' && item.target_port === 'in_3'));
  assert.ok(edge, 'inspector did not create the input connection');
  assert.equal(await page.locator('#cvNodes [data-node-id="and-power"] .rtc-slot[data-slot="in_3"].is-empty').count(), 0,
    'canvas slot row did not become occupied');
  assert.match(await page.locator('#cvNodes [data-node-id="and-power"] .rtc-slot[data-slot="in_3"]').innerText(), /开始/,
    'canvas slot row does not show the new source');
  const badge = await dirtyBadge();
  assert.equal(badge.hidden, false, 'inspector connection did not mark the draft dirty');
  assert.equal(badge.appKind, 'behavior');
  assert.equal(await page.evaluate(() => window.App.kit.canUndo()), true,
    'inspector connection did not produce one history entry');
});

test('review-slot-limit-import-preserves-draft', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const before = await page.evaluate(() => ({
    nodes: window.App.graph.nodes.map(node => node.id),
    edges: window.App.graph.edges.map(edge => [edge.id, edge.target_port]),
    slots: document.querySelectorAll('#cvNodes .rtc-slot').length,
  }));
  const root = JSON.parse(JSON.stringify(EXAMPLE));
  root.edges[0].target_port = 'in_9999';
  await clearToasts();
  await setImportPayload({
    kind: 'sr-routing-graph', version: 3, name: '非法槽位',
    timezone: BASELINE.timezone, sticky: BASELINE.sticky, root,
  }, 'invalid-slot-in-9999.json');
  await waitToastIncludes(await page.evaluate(n => window.tf('cv.toastImportIssues', { n }), 1));
  const after = await page.evaluate(() => ({
    nodes: window.App.graph.nodes.map(node => node.id),
    edges: window.App.graph.edges.map(edge => [edge.id, edge.target_port]),
    slots: document.querySelectorAll('#cvNodes .rtc-slot').length,
  }));
  assert.deepEqual(after, before, 'invalid slot import replaced the draft or allocated new slot rows');
});

test('review-import-contract-rejects-before-draft-replacement', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const base = await page.evaluate(() => window.RTGraph.createMinimalTree('qwen3.8-flash-sft'));
  const kept = await page.evaluate(() => JSON.stringify(window.App.tree));
  const invalid = [];
  const badType = JSON.parse(JSON.stringify(base));
  badType.root.nodes.push({ id: 'q-review', type: 'question', question_type: 'unsupported', instructions: '非法类型' });
  badType.root.edges.push({
    id: 'e-q-review', source: 'start', source_port: 'out',
    target: 'q-review', target_port: 'in_1', condition: null,
  });
  invalid.push(badType);
  const unknown = JSON.parse(JSON.stringify(base));
  unknown.root.legacy = true;
  invalid.push(unknown);
  const badZone = JSON.parse(JSON.stringify(base));
  badZone.timezone = 'Mars/Olympus';
  invalid.push(badZone);

  for (const tree of invalid) {
    await clearToasts();
    await setImportPayload({
      kind: 'sr-routing-graph', version: 3, name: tree.name,
      timezone: tree.timezone, sticky: tree.sticky, root: tree.root,
    }, 'invalid-contract.json');
    await waitToastIncludes(await page.evaluate(n => window.tf('cv.toastImportIssues', { n }), 1));
    assert.equal(await page.evaluate(() => JSON.stringify(window.App.tree)), kept);
  }
});

test('review-import-envelope-types-preserve-draft', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const base = await page.evaluate(() => window.RTGraph.createMinimalTree('qwen3.8-flash-sft'));
  const kept = await page.evaluate(() => JSON.stringify(window.App.tree));
  const invalid = [];
  const badSticky = JSON.parse(JSON.stringify(base));
  badSticky.sticky.enabled = 'yes';
  invalid.push(badSticky);
  const badTimezone = JSON.parse(JSON.stringify(base));
  badTimezone.timezone = 123;
  invalid.push(badTimezone);

  for (const tree of invalid) {
    await clearToasts();
    await setImportPayload({
      kind: 'sr-routing-graph', version: 3, name: tree.name,
      timezone: tree.timezone, sticky: tree.sticky, root: tree.root,
    }, 'invalid-envelope-value.json');
    await waitToastIncludes(await page.evaluate(n => window.tf('cv.toastImportIssues', { n }), 1));
    assert.equal(await page.evaluate(() => JSON.stringify(window.App.tree)), kept);
  }
});

test('review-jev-window-save-error-uses-window-message', async () => {
  const detail = 'window save rejected by QA';
  await page.route('**/admin/jev/window', async route => {
    if (route.request().method() === 'PUT') {
      await route.fulfill({
        status: 422,
        contentType: 'application/json',
        body: JSON.stringify({ detail }),
      });
      return;
    }
    await route.continue();
  });
  expectHttpStatusError({ url: /\/admin\/jev\/window$/, status: /422/ });
  try {
    await page.goto(routerBase + '/console/jev.html');
    await page.waitForFunction(() =>
      document.getElementById('winContext').value === '4096' &&
      document.getElementById('winInput').value === '1024');
    await page.locator('#saveWindow').click();
    await page.waitForFunction(expected =>
      document.getElementById('windowMsg').textContent.includes(expected), detail);
    assert.equal(await page.locator('#saveMsg').textContent(), '');
    assert.equal(expectedHttpStatusErrors.length, 0,
      'the expected window PUT 422 was not observed');

    await page.unroute('**/admin/jev/window');
    const savedResponse = page.waitForResponse(response =>
      response.url().endsWith('/admin/jev/window') &&
      response.request().method() === 'PUT' && response.status() === 200);
    await page.locator('#saveWindow').click();
    assert.equal((await savedResponse).status(), 200);
    await page.waitForFunction(() => document.getElementById('windowMsg').textContent === '');
  } finally {
    await page.unroute('**/admin/jev/window');
    resetExpectedHttpStatusErrors();
  }
});

test('review-pointer-cancel-restores-node-without-history', async () => {
  await ensureBaselineTree();
  await openCanvas();
  const original = await page.evaluate(() => {
    const node = window.App.graph.nodes.find(item => item.id === 'start');
    return { x: node.position.x, y: node.position.y, kind: window.App.kind() };
  });
  const box = await page.locator('[data-node-id="start"]').boundingBox();
  const point = { x: box.x + box.width / 2, y: box.y + 11 };
  await page.mouse.move(point.x, point.y);
  await page.mouse.down();
  await page.mouse.move(point.x + 70, point.y + 50, { steps: 6 });
  await page.locator('#cvStage').dispatchEvent('pointercancel', {
    pointerId: 1, pointerType: 'mouse', clientX: point.x + 70, clientY: point.y + 50,
  });
  await page.mouse.up();
  await page.waitForFunction(expected => {
    const node = window.App.graph.nodes.find(item => item.id === 'start');
    return node.position.x === expected.x && node.position.y === expected.y;
  }, original);
  const after = await page.evaluate(() => ({
    kind: window.App.kind(), undo: window.App.kit.canUndo(), redo: window.App.kit.canRedo(),
  }));
  assert.equal(original.kind, 'clean');
  assert.deepEqual(after, { kind: 'clean', undo: false, redo: false });
});

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

async function main() {
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

const invokedDirectly = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedDirectly) {
  main().catch(error => {
    console.error(error && error.stack ? error.stack : error);
    process.exitCode = 1;
  });
}
