/* RoutePilot 控制台 QA 基座（Task 8 起以 Policy v4 为面）
   ------------------------------------------------------------------
   本文件只保留：服务启动（Node mock + `python -m router` 子进程，策略目录走
   ROUTER_POLICY_DIR）、浏览器、管理面 API、用例注册器/执行器、共享断言助手
   与 teardown。页面用例位于 scripts/console_qa/{testchat,models,policy,
   tryrun,logs}.mjs（Task 9 注册表：试算与计算日志已按 Policy v4 重写注册；
   canvas / inspector 属画布时代，Task 10 随页面一并删除，保持退出注册），
   各模块只调用本文件的 test(name, fn) 注册用例，不复制启动逻辑、不持有页面状态。

   console_qa.mjs 顶部保留的基座自检（健康检查 / 原生 ESM / 单模块入口 /
   登录页语言开关 / mock wire shape / mock 故障注入 / mock 上游 OpenAI 格式 /
   bootstrap 目录与初始策略 / 真实 /admin/try）不属于任何页面模块。

   Usage: node scripts/console_qa.mjs            # 全量（= npm run check:console）
          QA_ONLY=<regex> node scripts/console_qa.mjs
          node scripts/console_qa/policy.mjs     # 只跑单个页面模块
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

export { HISTORY_LIMIT };

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const EXAMPLE_PATH = path.join(ROOT, 'router/console/routing/example.json');
const SCENARIO_PATH = path.join(ROOT, 'tests/fixtures/policy_scenarios.json');
const SHOT_DIR = path.join(ROOT, 'docs/od-design/routing-canvas-2026-11-02/implementation-qa');
/* Screenshot writes are opt-in: a plain `npm run check:console` must not dirty
   the committed evidence PNGs. Regenerate with QA_SCREENSHOTS=1. */
const WRITE_SHOTS = process.env.QA_SCREENSHOTS === '1';

/* 示例策略（= tests/fixtures/policy_v4.json，spec §4.1 完整文档）：问题/规则数、
   兜底目标都从这份文档派生，避免 QA 里散落字面量。 */
export const EXAMPLE = JSON.parse(fs.readFileSync(EXAMPLE_PATH, 'utf8'));
export const EXAMPLE_QUESTIONS = Object.keys(EXAMPLE.questions);
export const EXAMPLE_RULES = EXAMPLE.rules.length;
export const EXAMPLE_FALLBACK = EXAMPLE.fallback;
/* v4 场景 fixture（Task 8 Step 6）：fast/power/adversarial/jev_error 四例，
   mock 直接按 scenario.answers（问题ID → 概率/yes）出 wire 答案。 */
export const FIXTURE = JSON.parse(fs.readFileSync(SCENARIO_PATH, 'utf8'));
export const POLICY_ID = 'power-policy';
export const JEV_MODEL = 'd1-2B';
export const S1_TEXT = FIXTURE.scenarios.find(item => item.id === 'power').text;
export const S1_NOW = FIXTURE.scenarios.find(item => item.id === 'power').now;
const API_TIMEOUT_MS = 15000;
/* envelope 元数据是 QA 基座约定，集中一处供 bootstrap / 基线 / 断言复用。 */
export const BASELINE = {
  name: EXAMPLE.name,
  timezone: EXAMPLE.timezone,
  sticky: EXAMPLE.sticky,
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
  /* 跑 0 条绝不能算通过：QA_ONLY 拼错或注册器为空时，过去会打印
     ALL PASS (0) 并以 0 退出，看起来像绿灯。这里显式计为失败。 */
  if (ran === 0) {
    failures.push(filter
      ? `QA_ONLY=${JSON.stringify(process.env.QA_ONLY)} matched 0 of ${checks.length} registered cases`
      : `0 cases ran (registry holds ${checks.length})`);
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

function pickScenario(state) {
  const matched = FIXTURE.scenarios.find(scenario => state.indexOf(scenario.text) >= 0);
  return matched || { id: 'manual', text: '', answers: {}, fail: null };
}

function normalizeProbabilities(values) {
  const total = values.reduce((sum, value) => sum + value, 0);
  if (!(total > 0)) return values.map(() => 1 / values.length);
  return values.map(value => value / total);
}

/* v4 场景答案按「问题ID → 选项 key 概率」描述；JEV wire 的 choice criteria
   键是 label。option key ↔ label 的映射直接取自示例策略文档本身，
   不再经过任何旧场景键（capability/domain/easy/standard/strong）垫片。 */
function choiceAnswer(questionId, question, scenario) {
  const options = ((EXAMPLE.questions || {})[questionId] || {}).options || [];
  const keyOfLabel = {};
  options.forEach(option => { keyOfLabel[option.label] = option.key; });
  const probsByKey = (((scenario || {}).answers || {})[questionId] || {}).probs || {};
  const labels = Object.keys(question.criteria || {});
  let values = labels.map(label => {
    const key = keyOfLabel[label];
    const value = key ? probsByKey[key] : null;
    return typeof value === 'number' ? value : null;
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

function noulAnswer(questionId, scenario) {
  const answer = ((scenario || {}).answers || {})[questionId];
  if (answer && typeof answer.yes === 'number') return { type: 'noul', noul: answer.yes };
  return { type: 'noul', noul: 0.5 };
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

function answerFor(questionId, question, scenario) {
  const type = question && question.type;
  const fixture = ((scenario || {}).answers || {})[questionId] || null;
  if (type === 'choice') return choiceAnswer(questionId, question, scenario);
  if (type === 'noul') return noulAnswer(questionId, scenario);
  if (type === 'score') return scoreAnswer(question, fixture);
  throw new Error(`mock does not support question type ${type} (${questionId})`);
}

/* ── Task 7 · 测试对话（/v1/chat/completions）的确定性 mock 触发词 ──────
   默认路径仍然是「固定回复」，只有最后一条 user 文本命中触发词时才切换：
   * 返回多模态 → 先一个文本 delta，再一个内容 part 数组（图片/音频/视频/文件/
     未知 part），最后 stop + [DONE]；
   * 延迟流 → 立刻发「第一段」，1500ms 后发「第二段」+ stop + [DONE]；浏览器
     中止时上游连接会被 router 掐断，所有补写都带 writableEnded/destroyed 守卫
     并吞掉 EPIPE，绝不因为 QA 的 Stop/New Chat 而崩溃。
   浏览器 QA 不加载外网媒体：Chromium 会真的为 <video src="https://…"> 发起请求
   （实测），而 QA 的外部请求守卫是硬失败；视频用 data: URL，与 renderer 用例
   「只挂 data: 媒体源」的约定一致（http(s) 白名单由 renderer 用例覆盖）。 */
const QA_PIXEL = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=';
const QA_MEDIA_PARTS = [
  { type: 'image_url', image_url: { url: `data:image/png;base64,${QA_PIXEL}` } },
  { type: 'audio', audio: { data: 'AA==', format: 'wav' } },
  { type: 'video_url', video_url: { url: 'data:video/mp4;base64,AAAA' } },
  { type: 'file', file: { filename: 'result.bin', file_data: 'data:application/octet-stream;base64,AA==' } },
  { type: 'custom', value: { source: 'qa' } },
];
const QA_DELAYED_STREAM_MS = 1500;

/* 与 router 的 _last_user_text 保持一致：字符串 content 或 part 数组里的 text。 */
function lastUserText(messages) {
  if (!Array.isArray(messages)) return '';
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (!message || message.role !== 'user') continue;
    const content = message.content;
    if (typeof content === 'string') return content;
    if (Array.isArray(content)) {
      return content.map(part => (part && typeof part.text === 'string' ? part.text : '')).join(' ');
    }
  }
  return '';
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

          /* jev_error 场景：v4 一次 JEV 覆盖全部问题，场景标记 fail 时整次调用
             直接按其配置的 HTTP status 失败——没有「第几波」要判断。 */
          if (scenario.fail) {
            sendJson(response, scenario.fail.status || 504, {
              detail: scenario.fail.message || 'mock decision failure',
              scenario: scenario.id,
              message: scenario.fail.message || ''
});
            return;
}
          const answers = {};
          questionKeys.forEach(questionId => {
            answers[questionId] = answerFor(questionId, body.questions[questionId], scenario);
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
            /* 客户端中止后 socket 写失败不能冒泡成未捕获异常。 */
            response.on('error', () => {});
            const chunk = (delta, finishReason = null) => 'data: ' + JSON.stringify({
              id: 'chatcmpl-qa', object: 'chat.completion.chunk', created, model,
              choices: [{ index: 0, delta, finish_reason: finishReason }],
}) + '\n\n';
            const trigger = lastUserText(body.messages);
            if (trigger.includes('返回多模态')) {
              response.write(chunk({ role: 'assistant', content: '多模态回复：' }));
              response.write(chunk({ content: QA_MEDIA_PARTS }));
              response.write(chunk({}, 'stop'));
              response.write('data: [DONE]\n\n');
              response.end();
              return;
}
            if (trigger.includes('延迟流')) {
              response.write(chunk({ role: 'assistant', content: '第一段' }));
              const timer = setTimeout(() => {
                if (response.writableEnded || response.destroyed) return;
                try {
                  response.write(chunk({ content: '第二段' }));
                  response.write(chunk({}, 'stop'));
                  response.write('data: [DONE]\n\n');
                  response.end();
} catch (error) {
                  /* EPIPE / 已中止：浏览器已经停止读取，补写失败无需处理。 */
}
}, QA_DELAYED_STREAM_MS);
              response.on('close', () => clearTimeout(timer));
              return;
}
            if (trigger.includes('截断流')) {
              /* 只写一段文本就 EOF：没有 finish_reason、没有 [DONE]，模拟上游提前断流。 */
              response.write(chunk({ role: 'assistant', content: '截断回复' }));
              response.end();
              return;
}
            /* 默认行为保持原样：固定回复。 */
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
      ROUTER_POLICY_DIR: path.join(tempDir, 'routing-policy'),
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

export async function api(method, apiPath, body, headers) {
  if (!routerBase) throw new Error('router is not ready');
  const init = { method, headers: Object.assign({}, headers || {}) };
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

  /* 2. 示例策略与用例引用的模型，均声明 low/medium/high 推理档位。 */
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
  /* 未声明推理档位的模型：用于目标检查器空态提示的回归覆盖。 */
  await api('POST', '/admin/models', {
    id: 'tierless-model',
    upstream: 'tierless-model',
    provider: 'local',
    profile: { domains: [], tier: 'small' }
});

  /* 3. peak / normal / offpeak 三个固定时段（与 policy_scenarios.json 一致）。 */
  await api('PUT', '/admin/time-periods', { periods: FIXTURE.periods });

  /* 4. 决策服务指向 mock。 */
  await api('PUT', '/admin/jev', {
    base_url: mockBase,
    model: JEV_MODEL,
    timeout_seconds: 5
});

  /* 5. 建策略并启用（policy 原样使用 example.json = spec §4.1 完整文档）。 */
  await api('POST', '/admin/policies', {
    id: POLICY_ID,
    policy: EXAMPLE
});
  await api('POST', '/admin/policies/' + POLICY_ID + '/enable');
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

test('policy.js is served as a native ES module with a single module entry', async () => {
  const response = await fetch(routerBase + '/console/routing/policy.js');
  assert.equal(response.status, 200);
  const text = await response.text();
  assert.match(text, /export function/, 'policy.js is not an ES module');
  assert.doesNotMatch(text, /module\.exports/, 'policy.js still carries the CommonJS export');
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
    /* 只有模块 boot 注入选项后 select 才非空：证明 index.html 的模块真的跑过。 */
    'index.html': '#testModel:not(:empty)',
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
  await page.waitForSelector('#testModel');
  await page.locator('.langtog button[data-lang="en"]').click();
  await waitFor(() => document.documentElement.lang === 'en');
  const english = await page.evaluate(() => document.querySelector('.top nav a').textContent.trim());
  assert.equal(english, 'Test', 'language toggle did not repaint the nav');
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
  /* v4 问题集直接来自示例策略：route（choice，criteria 键是 option label）、
     power_business（noul）；complexity 是合成 score 问题，覆盖第三种 wire。 */
  const route = EXAMPLE.questions.route;
  const routeCriteria = {};
  route.options.forEach(option => { routeCriteria[option.label] = option.capability; });
  const state = '【待判断的用户请求】\n' + S1_TEXT + '\n\n【任务】请选择最合适处理这条请求的模型。';
  const response = await fetch(mockBase + '/v1/systemone', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      model: JEV_MODEL,
      state,
      questions: {
        route: { type: 'choice', instructions: route.instructions, criteria: routeCriteria },
        power_business: {
          type: 'noul', instructions: EXAMPLE.questions.power_business.instructions
        },
        complexity: { type: 'score', instructions: '复杂度评分。', criteria: [1, 5, 7.5, 10] }
}
})
});
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.deepEqual(Object.keys(body.answers).sort(), ['complexity', 'power_business', 'route']);

  const choice = body.answers.route;
  assert.equal(choice.type, 'choice');
  assert.equal(typeof choice.choice, 'string');
  assert.deepEqual(
    Object.keys(choice.probabilities).sort(),
    route.options.map(option => option.label).sort()
  );
  const total = Object.values(choice.probabilities).reduce((sum, value) => sum + value, 0);
  assert.ok(Math.abs(total - 1) < 1e-9);
  /* S1_TEXT 命中 power 场景：电力专用模型 0.8。 */
  assert.ok(Math.abs(choice.probabilities['电力专用模型'] - 0.8) < 1e-9);
  assert.ok(Math.abs(choice.confidence - 0.8) < 1e-9);

  const noul = body.answers.power_business;
  assert.equal(noul.type, 'noul');
  assert.equal(typeof noul.noul, 'number');
  assert.ok(noul.noul >= 0 && noul.noul <= 1);
  assert.ok(Math.abs(noul.noul - 0.9) < 1e-9);
  assert.equal('yes' in noul, false);

  const score = body.answers.complexity;
  assert.equal(score.type, 'score');
  assert.equal(Number.isInteger(score.score), true);
  assert.equal(typeof score.legend[String(score.score)], 'number');
  assert.equal(score.legend[String(score.score)], 5);
  assert.ok(typeof score.probabilities[String(score.score)] === 'number');
});

test('mock fails the jev_error scenario with its configured status', async () => {
  const failing = FIXTURE.scenarios.find(scenario => scenario.id === 'jev_error');
  assert.ok(failing, 'jev_error scenario fixture missing');
  assert.ok(failing.fail && failing.fail.status,
    'the jev_error scenario must carry a configured fail status');
  const route = EXAMPLE.questions.route;
  const routeCriteria = {};
  route.options.forEach(option => { routeCriteria[option.label] = option.capability; });
  const state = '【待判断的用户请求】\n' + failing.text + '\n\n【任务】请选择最合适处理这条请求的模型。';
  const response = await fetch(mockBase + '/v1/systemone', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      model: JEV_MODEL,
      state,
      questions: {
        route: { type: 'choice', instructions: route.instructions, criteria: routeCriteria },
        power_business: {
          type: 'noul', instructions: EXAMPLE.questions.power_business.instructions
        },
        adversarial: {
          type: 'noul', instructions: EXAMPLE.questions.adversarial.instructions
        }
}
})
});
  /* v4 一次 JEV 覆盖全部问题：场景 fail 直接以其 HTTP status 失败整次调用。 */
  assert.equal(response.status, failing.fail.status);
  const responseBody = await response.json();
  assert.equal(responseBody.scenario, 'jev_error');
  assert.equal(responseBody.message, failing.fail.message);
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

/* bootstrap 自检紧跟 bootstrap()：目录/时段/决策服务/初始策略一旦被后面的用例
   改动，这条用例就该在基座阶段立刻失败，而不是在 100 多条页面用例之后才报。 */
test('bootstrap registers catalog, periods, decision service and the enabled policy', async () => {
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

  const policies = await api('GET', '/admin/policies');
  const resource = policies.policies.find(item => item.id === POLICY_ID);
  assert.ok(resource, 'example policy resource missing');
  assert.equal(resource.enabled, true);
  assert.equal(resource.policy.name, EXAMPLE.name);
  assert.deepEqual(Object.keys(resource.policy.questions).sort(), EXAMPLE_QUESTIONS.slice().sort());
  assert.equal(resource.policy.rules.length, EXAMPLE_RULES);
  assert.equal(resource.policy.fallback, EXAMPLE_FALLBACK);
  assert.match(resource.policy_revision, /^[0-9a-f]{64}$/);
  assert.match(resource.document_sha256, /^[0-9a-f]{64}$/);
});


test('admin try routes the sample policy through the live decision mock', async () => {
  const result = await api('POST', '/admin/try', {
    policy_id: POLICY_ID, text: S1_TEXT, now: S1_NOW,
  });
  assert.equal(result.target, 'power');
  assert.deepEqual(result.models, ['qwen3.8-flash-sft']);
  assert.equal(result.matched_rule, '电力业务优先');
  assert.equal(result.matched_rule_index, 1);
  assert.equal(result.fallback, false);
  assert.equal(result.reason, null);
  /* 一次 JEV 调用；answer 只展示后端返回，前端不重新求值。 */
  const jevEvents = result.trace.filter(event => event.event === 'jev');
  assert.equal(jevEvents.length, 1);
  assert.deepEqual(jevEvents[0].questions.slice().sort(), ['adversarial', 'power_business', 'route']);
  assert.equal(jevEvents[0].failed, false);
  /* 规则轨迹：已求值规则带逐条件结果，命中后剩余规则 matched=null 未求值。 */
  const ruleEvents = result.trace.filter(event => event.event === 'rule');
  assert.deepEqual(ruleEvents.map(row => row.matched), [false, true, null, null]);
  assert.equal(ruleEvents[0].conditions[0].path, 'when');
  assert.deepEqual(ruleEvents[1].conditions.map(row => row.path),
    ['when.any.0', 'when.any.1', 'when.any']);
  assert.ok(ruleEvents.every(row => Array.isArray(row.conditions)));
  assert.equal(result.trace.filter(event => event.event === 'final').length, 1);
  assert.ok(Math.abs(result.answers.route.probabilities.power - 0.8) < 1e-9);
  assert.ok(Math.abs(result.answers.route.p_top - 0.8) < 1e-9);
  assert.ok(Math.abs(result.answers.power_business.yes - 0.9) < 1e-9);
  assert.ok(Math.abs(result.answers.adversarial.yes - 0.1) < 1e-9);

  const calls = mockState.systemoneCalls.filter(call => call.scenario === 'power');
  assert.equal(calls.length >= 1, true);
  assert.deepEqual(calls[calls.length - 1].keys.slice().sort(), ['adversarial', 'power_business', 'route']);
});

/* ── Task 8 · 策略编辑器页面助手（替换画布时代 helper） ────────────── */

export async function openEditor() {
  await page.goto(routerBase + '/console/routing.html');
  await waitFor(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return (app && app.booted && app.policy);
});
  await page.waitForSelector('#policyQuestions .pv-card', { timeout: 20000 });
  await page.waitForTimeout(150);
}

export async function appSnapshot() {
  return page.evaluate(async () => {
    const { app, store } = await import('/console/routing/app.js');
    return ({
    booted: app.booted,
    policyId: app.policyId,
    kind: app.kind(),
    name: app.policy ? app.policy.name : null,
    questions: Object.keys(app.policy ? app.policy.questions : {}),
    targets: Object.keys(app.policy ? app.policy.targets : {}),
    rules: (app.policy ? app.policy.rules : []).map(rule => ({
      name: rule.name, target: rule.target
})),
    layout: app.layout,
    history: { undo: store.canUndo, redo: store.canRedo }
});
});
}

export async function clearToasts() {
  await page.evaluate(() => { document.getElementById('cvToast').innerHTML = ''; });
}

export async function toastText() {
  return page.evaluate(() => document.getElementById('cvToast').textContent.replace(/\s+/g, ' ').trim());
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

/* 兼容导出：models.mjs 的 import 语句仍列出该名称（画布时代用例已删除，
   注册在案的健康用例不再调用）。 */
export async function waitInspectorBranch() {
  return false;
}

/* ── Task 8 · 策略资源基线 / dirty / 保存 / 布局 ───────────────────── */

export async function serverPolicyResource(id) {
  return (await api('GET', '/admin/policies/' + encodeURIComponent(id || POLICY_ID))).policy_resource;
}

export async function putPolicy(id, policy) {
  const current = await serverPolicyResource(id);
  return api('PUT', '/admin/policies/' + encodeURIComponent(id), policy, {
    'if-match': current.document_sha256,
  }).then(result => result.policy_resource);
}

/* 每个策略用例都从同一份服务端基线开始：power-policy 恢复为示例文档并启用。 */
export async function ensureBaselinePolicy() {
  const restored = await putPolicy(POLICY_ID, EXAMPLE);
  if (!restored.enabled) {
    await api('POST', '/admin/policies/' + POLICY_ID + '/enable');
  }
  return serverPolicyResource(POLICY_ID);
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

export function assertIncludes(haystack, needle, message) {
  assert.ok(String(haystack).indexOf(needle) >= 0,
    `${message || 'missing text'} (wanted ${JSON.stringify(needle)} in ${JSON.stringify(String(haystack).slice(0, 400))})`);
}

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
  console.log(`[console-qa] registered cases: ${checks.length}`);
  /* 独立执行时注册器为空 = 这个模块什么都没跑；此时启动服务再打印
     ALL PASS (0) 只会伪装成通过，直接报错并以非 0 退出。 */
  if (!checks.length) {
    console.error(`[console-qa] no cases registered for ${path.basename(fileURLToPath(metaUrl))};` +
      ' refusing to report ALL PASS (0) for an empty run');
    process.exitCode = 1;
    return;
  }
  try {
    await runRegisteredCases();
} catch (error) {
    console.error(error && error.stack ? error.stack : error);
    process.exitCode = 1;
}
}

async function main() {
  /* 静态 import 会先于本文件执行（页面模块又反向 import 本文件的注册器），
     因此页面模块在 main 里动态加载，确保基座初始化完成后再注册用例。
     Task 9 注册表：testchat / models / policy / tryrun / logs；canvas /
     inspector（画布时代）保持退出注册，Task 10 随页面一并删除。 */
  await import('./console_qa/testchat.mjs');
  await import('./console_qa/models.mjs');
  await import('./console_qa/policy.mjs');
  await import('./console_qa/tryrun.mjs');
  await import('./console_qa/logs.mjs');
  console.log(`[console-qa] registered cases: ${checks.length}`);
  await runRegisteredCases();
}

const invokedDirectly = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedDirectly) {
  main().catch(error => {
    console.error(error && error.stack ? error.stack : error);
    process.exitCode = 1;
});
}
