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
