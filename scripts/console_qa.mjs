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

/* ── 用例注册器 ─────────────────────────────────────────────────────── */

const checks = [];

export function test(name, fn) {
  checks.push({ name, fn });
}

export async function runChecks() {
  const failures = [];
  for (const check of checks) {
    try {
      await check.fn();
      console.log(`PASS ${check.name}`);
    } catch (error) {
      failures.push(`${check.name}: ${error.stack || error}`);
    }
  }
  if (pageErrors.length) {
    failures.push('browser pageerror/console error:\n' + pageErrors.join('\n'));
  }
  if (externalRequests.length) {
    failures.push('external requests:\n' + externalRequests.join('\n'));
  }
  if (failures.length) throw new Error(failures.join('\n\n'));
  console.log(`ALL PASS (${checks.length})`);
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
const mockState = { systemoneCalls: [], chatCalls: [], scenarioCalls: {} };

export async function screenshot(name) {
  await fsp.mkdir(SHOT_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SHOT_DIR, name) });
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
  const example = JSON.parse(fs.readFileSync(EXAMPLE_PATH, 'utf8'));
  const optionKeys = {};
  (example.nodes || []).forEach(node => {
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
  const example = JSON.parse(fs.readFileSync(EXAMPLE_PATH, 'utf8'));
  await api('POST', '/admin/trees', {
    id: TREE_ID,
    name: '电力优先决策组',
    timezone: 'Asia/Shanghai',
    sticky: { enabled: true, ttl_seconds: 1800 },
    root: example
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
    if (message.type() === 'error') pageErrors.push('console: ' + message.text());
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
  const repoDb = path.join(ROOT, 'router.db');
  assert.equal(fs.existsSync(repoDb) ? fs.statSync(repoDb).size + ':' + fs.statSync(repoDb).mtimeMs : null, repoDbFingerprint);
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
  await page.mouse.move(from.x + 26, from.y + 18, { steps: 4 });
  await page.mouse.move(target.x, target.y, { steps: 10 });
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
    const add = row.querySelector('.rtc-slot-add');
    return {
      id: id ? id.textContent.trim() : '',
      add: add ? add.textContent.trim() : '',
      border: getComputedStyle(row).borderTopStyle,
      title: row.getAttribute('title') || ''
    };
  });
  assert.ok(emptySlot, 'empty slot placeholder missing');
  assert.match(emptySlot.id, /^in_\d+$/);
  assert.equal(emptySlot.add, await page.evaluate(() => window.t('cv.slot.add')));
  assert.equal(emptySlot.border, 'dashed');
  assert.ok(emptySlot.title.length > 0, 'empty slot placeholder lacks a next-slot tooltip');
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

test('Task 10 · wheel zoom anchors at the pointer; zoom buttons, 100% reset and fit work', async () => {
  await openCanvas();
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

test('Task 10 · node drag redraws edges live and pushes one layout history entry (buttons + keyboard)', async () => {
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

test('Task 10 · node library creates every group by click and by drag-in', async () => {
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

test('Task 10 · a number port dropped on an empty slot creates gt 0.5 and opens the condition editor', async () => {
  await openCanvas();
  const before = await appSnapshot();
  assert.equal(await page.locator('#cvCondPop').isHidden(), true);
  const target = await portPoint('and-power', 'in_3', 'in');
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
});

test('Task 10 · a boolean edge defaults to condition null and does not open the condition editor', async () => {
  await openCanvas();
  const before = await appSnapshot();
  await dragPort('start', 'out', await portPoint('and-power', 'in_3', 'in'));
  const after = await appSnapshot();
  assert.equal(after.edges.length, before.edges.length + 1);
  const edge = after.edges.find(item => item.target === 'and-power' && item.target_port === 'in_3');
  assert.ok(edge, 'boolean edge not created');
  assert.equal(edge.condition, null);
  const chipText = await page.locator(`#cvNodes [data-cond-edge="${edge.id}"]`).textContent();
  assert.equal(chipText.trim(), '= true');
  assert.equal(await page.locator('#cvCondPop').isHidden(), true);
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

test('Task 10 · occupied/self/cycle/duplicate drops toast specific reasons and are not highlighted compatible', async () => {
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
    await dragPort(item.source, item.port, await portPoint(item.node, item.slot, 'in'), {
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

test('Task 10 · delete confirmation lists affected edges; slots are not renumbered and reuse the smallest hole', async () => {
  await openCanvas();
  await clickNode('and-power');
  let selection = await page.evaluate(() => window.App.kit.getSelection());
  assert.deepEqual(selection, { kind: 'node', id: 'and-power' });
  await page.keyboard.press('Delete');
  await page.waitForFunction(() => !document.getElementById('cvDeleteModal').hidden, null, { timeout: 3000 });
  assert.equal(await page.locator('#cvDeleteBody li').count(), 3);
  const dialogText = await page.locator('#cvDeleteBody').innerText();
  assert.match(dialogText, /capability/);
  assert.match(dialogText, /domain/);
  assert.match(dialogText, /power-target/);
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

  await dragPort('cond-peak', 'out', await portPoint('or-ctx', 'in_1', 'in'));
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
  assert.match(dialogText, /capability/);
  assert.match(dialogText, /selected\.strong/);
  assert.match(dialogText, /strong-target/);
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
    const repoDb = path.join(ROOT, 'router.db');
    repoDbFingerprint = fs.existsSync(repoDb) ? fs.statSync(repoDb).size + ':' + fs.statSync(repoDb).mtimeMs : null;
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
