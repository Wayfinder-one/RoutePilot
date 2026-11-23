/* RTRuntime 纯函数契约测试（Task 9 / 规格「画布快照」「画布编辑」）
 *
 * 运行：node --test scripts/console_runtime_test.mjs
 * 被测模块是浏览器与 Node 共用的原生 ES module：
 *   router/console/routing/runtime.js
 *
 * 覆盖：catalog → 完整四字段 runtime；JEV 选择原子替换服务与窗口（深拷贝、不改入参、
 * JSON 里没有密钥）；模型补入同 ID 不覆盖已有快照；目录非法/为空时给出明确错误；
 * 刷新时条件节点引用的时段必须仍在目录里（否则点名拒绝）；错误文案随界面语言。
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  DEFAULT_WINDOW,
  addRuntimeModel,
  createRuntime,
  publicModel,
  publicProvider,
  refreshRuntime,
  selectJev
} from '../router/console/routing/runtime.js';
import { RUNTIME_FIELDS, validateRuntime } from '../router/console/routing/graph.js';

const SERVICE_A = {
  id: 'jev-a',
  base_url: 'http://a.test',
  model: 'd1-a',
  key_env: '',
  timeout_seconds: 10,
  instructions: '选择',
  state_template: '{request}',
  input_tokens: 1024,
  context_tokens: 4096,
  key_present: true,
  key_source: 'stored'
};
const SERVICE_B = Object.assign({}, SERVICE_A, {
  id: 'jev-b',
  base_url: 'http://b.test',
  model: 'd1-b',
  input_tokens: 2048,
  context_tokens: 8192,
  api_key: 'sk-must-not-leak'
});
const LOCAL = {
  name: 'local', base_url: 'http://json.test/v1', key_env: '',
  reasoning_format: 'openai', key_present: true, key_source: 'env'
};
const BACKUP_PROVIDER = Object.assign({}, LOCAL, {
  name: 'backup-provider',
  base_url: 'http://backup.test/v1'
});
const FAST = { id: 'fast', upstream: 'json-upstream', provider: 'local', profile: { tier: 'small' } };
const BACKUP = {
  id: 'backup', upstream: 'backup-upstream', provider: 'backup-provider', profile: {}
};
const PERIODS = [
  { name: 'peak', days: [0, 1, 2, 3, 4], start: '09:00', end: '18:00' },
  { name: 'night', days: [5, 6], start: '22:00', end: '06:00' }
];
const CATALOG = {
  providers: [LOCAL, BACKUP_PROVIDER],
  models: [FAST, BACKUP],
  services: [SERVICE_A, SERVICE_B],
  periods: PERIODS
};

/* 条件节点 source=periods 引用的时段必须仍在数据库目录里（服务端 enable 会报
   unknown_period，因此刷新不能静默丢弃）。 */
function periodGraph(names) {
  return {
    nodes: names.map((list, index) => ({
      id: 'cond-' + index, type: 'condition', source: 'periods', periods: list
    })),
    edges: []
  };
}

const PERIOD_GRAPH = periodGraph([['peak']]);

function runtimeKeys(runtime) {
  return Object.keys(runtime).sort();
}

test('createRuntime builds a complete four-field snapshot from the database catalog', () => {
  const runtime = createRuntime(CATALOG, { modelId: 'fast', jevId: 'jev-a' });
  assert.deepEqual(runtimeKeys(runtime), RUNTIME_FIELDS.slice().sort());
  assert.deepEqual(runtime.backends.providers, [{
    name: 'local', base_url: 'http://json.test/v1', key_env: '', reasoning_format: 'openai'
  }]);
  assert.deepEqual(runtime.backends.models, [{
    id: 'fast', upstream: 'json-upstream', provider: 'local', profile: { tier: 'small' }
  }]);
  assert.deepEqual(runtime.jev, {
    id: 'jev-a',
    base_url: 'http://a.test',
    model: 'd1-a',
    key_env: '',
    timeout_seconds: 10,
    instructions: '选择',
    state_template: '{request}'
  });
  assert.deepEqual(runtime.jev_window, { input_tokens: 1024, context_tokens: 4096 });
  assert.deepEqual(runtime.time_periods, PERIODS);
  /* 目录里的密钥派生字段不得进入快照。 */
  const text = JSON.stringify(runtime);
  for (const secret of ['api_key', 'key_present', 'key_source']) {
    assert.equal(text.includes(secret), false, 'runtime leaked ' + secret);
  }
  assert.deepEqual(validateRuntime(runtime), [], 'createRuntime output must pass validateRuntime');
});

test('createRuntime defaults to the first catalog model and allows a null JEV', () => {
  const runtime = createRuntime(CATALOG, {});
  assert.equal(runtime.backends.models[0].id, 'fast');
  assert.equal(runtime.jev, null);
  assert.deepEqual(runtime.jev_window, DEFAULT_WINDOW);
  const explicitNull = createRuntime(CATALOG, { modelId: 'backup', jevId: null });
  assert.equal(explicitNull.backends.models[0].id, 'backup');
  assert.equal(explicitNull.backends.providers[0].name, 'backup-provider');
  assert.equal(explicitNull.jev, null);
});

test('createRuntime surfaces clear errors for empty or inconsistent catalogs', () => {
  assert.throws(() => createRuntime({ models: [], providers: [], services: [], periods: [] }),
    /模型目录为空/);
  assert.throws(() => createRuntime({}, {}), /模型目录为空/);
  assert.throws(() => createRuntime(CATALOG, { modelId: 'missing-model' }), /missing-model/);
  const danglingProvider = {
    providers: [],
    models: [FAST],
    services: [],
    periods: []
  };
  assert.throws(() => createRuntime(danglingProvider, {}), /provider local/);
  assert.throws(() => createRuntime(CATALOG, { jevId: 'missing-jev' }), /missing-jev/);
  /* 没有模型的目录绝不生成空 runtime。 */
  let built = null;
  try { built = createRuntime({ providers: [LOCAL], models: [], services: [], periods: [] }); } catch (error) { /* expected */ }
  assert.equal(built, null, 'an empty catalog must not produce a runtime');
});

test('JEV selection replaces service and window atomically without mutating the input', () => {
  const runtime = createRuntime(CATALOG, { modelId: 'fast', jevId: 'jev-a' });
  const before = JSON.stringify(runtime);
  const next = selectJev(runtime, SERVICE_B);
  assert.equal(next.jev.id, 'jev-b');
  assert.deepEqual(next.jev_window, { input_tokens: 2048, context_tokens: 8192 });
  assert.equal(next.jev.timeout_seconds, SERVICE_B.timeout_seconds);
  assert.equal(JSON.stringify(runtime), before, 'selectJev mutated its input runtime');
  assert.equal(JSON.stringify(next).includes('api_key'), false);
  assert.equal(JSON.stringify(next).includes('sk-must-not-leak'), false);
  assert.equal(next.backends.models[0].id, 'fast');
  /* 返回值是深拷贝：改返回值不影响入参。 */
  next.backends.models[0].upstream = 'mutated';
  next.jev.base_url = 'http://mutated.test';
  assert.equal(runtime.backends.models[0].upstream, 'json-upstream');
  assert.equal(runtime.jev.base_url, 'http://a.test');
  assert.deepEqual(validateRuntime(next), []);
});

test('deselecting JEV keeps a valid window and never mutates the input', () => {
  const runtime = createRuntime(CATALOG, { modelId: 'fast', jevId: 'jev-a' });
  const before = JSON.stringify(runtime);
  const next = selectJev(runtime, null);
  assert.equal(next.jev, null);
  /* 窗口策略（见 runtime.js 文件头）：取消选择只清 jev，保留窗口对象。 */
  assert.deepEqual(next.jev_window, { input_tokens: 1024, context_tokens: 4096 });
  assert.equal(JSON.stringify(runtime), before, 'selectJev(null) mutated its input runtime');
  assert.deepEqual(validateRuntime(next), []);
});

test('model selection embeds new entries without overwriting an existing snapshot', () => {
  const runtime = createRuntime(CATALOG, { modelId: 'fast', jevId: 'jev-a' });
  const before = JSON.stringify(runtime);
  let next = addRuntimeModel(runtime,
    Object.assign({}, FAST, { upstream: 'changed', profile: { tier: 'large' } }),
    Object.assign({}, LOCAL, { base_url: 'http://changed.test/v1' }));
  /* 同 ID 的模型/provider 已在快照里：普通 target 编辑绝不静默覆盖（DB 漂移只做检查提示）。 */
  assert.equal(next.backends.models.length, 1);
  assert.equal(next.backends.models[0].upstream, 'json-upstream');
  assert.deepEqual(next.backends.models[0].profile, { tier: 'small' });
  assert.equal(next.backends.providers[0].base_url, 'http://json.test/v1');
  assert.equal(JSON.stringify(runtime), before, 'addRuntimeModel mutated its input runtime');

  next = addRuntimeModel(next, BACKUP, BACKUP_PROVIDER);
  assert.deepEqual(next.backends.models.map(model => model.id), ['fast', 'backup']);
  assert.equal(next.backends.models[1].upstream, 'backup-upstream');
  assert.deepEqual(next.backends.providers.map(provider => provider.name),
    ['local', 'backup-provider']);
  assert.deepEqual(validateRuntime(next), []);
  /* 返回值是深拷贝：改返回值不影响入参。 */
  next.backends.models[1].upstream = 'mutated';
  assert.equal(runtime.backends.models.length, 1);
});

test('projections copy only the non-secret projection fields', () => {
  assert.deepEqual(publicModel(Object.assign({}, FAST, {
    key_present: true, key_source: 'stored', api_key: 'sk-must-not-leak', created_at: 'x'
  })), { id: 'fast', upstream: 'json-upstream', provider: 'local', profile: { tier: 'small' } });
  assert.deepEqual(publicProvider(Object.assign({}, LOCAL, {
    api_key: 'sk-must-not-leak', created_at: 'x'
  })), { name: 'local', base_url: 'http://json.test/v1', key_env: '', reasoning_format: 'openai' });
  /* profile 是自由 JSON 元数据：投影仍是深拷贝。 */
  const model = publicModel(FAST);
  model.profile.tier = 'mutated';
  assert.equal(FAST.profile.tier, 'small');
});

test('refreshRuntime rebuilds the snapshot from the DB catalog for declared resources', () => {
  const runtime = createRuntime(CATALOG, { modelId: 'fast', jevId: 'jev-a' });
  const next = refreshRuntime(runtime, CATALOG, PERIOD_GRAPH);
  assert.deepEqual(next, runtime, 'refresh with an unchanged catalog must be a no-op');
  assert.equal(JSON.stringify(runtime).includes('key_present'), false);

  const updated = {
    providers: [Object.assign({}, LOCAL, { base_url: 'http://fresh.test/v1' }), BACKUP_PROVIDER],
    models: [Object.assign({}, FAST, { upstream: 'fresh-upstream' }), BACKUP],
    services: [SERVICE_A, Object.assign({}, SERVICE_B, { input_tokens: 3072, context_tokens: 8192 })],
    periods: PERIODS
  };
  const refreshed = refreshRuntime(runtime, updated, PERIOD_GRAPH);
  assert.equal(refreshed.backends.providers[0].base_url, 'http://fresh.test/v1');
  assert.equal(refreshed.backends.models[0].upstream, 'fresh-upstream');

  /* 切换后的 JEV 同样从目录刷新，服务与窗口保持同源。 */
  const switched = selectJev(runtime, SERVICE_B);
  const refreshedJev = refreshRuntime(switched, updated, PERIOD_GRAPH);
  assert.equal(refreshedJev.jev.id, 'jev-b');
  assert.deepEqual(refreshedJev.jev_window, { input_tokens: 3072, context_tokens: 8192 });

  /* 目录缺少画布声明的模型/服务：抛明确错误，不静默删除引用。 */
  const missingModel = {
    providers: [LOCAL], models: [BACKUP], services: [SERVICE_A], periods: PERIODS
  };
  assert.throws(() => refreshRuntime(runtime, missingModel, PERIOD_GRAPH),
    /缺少画布声明的模型.*fast/);
  const missingJev = {
    providers: [LOCAL], models: [FAST], services: [SERVICE_B], periods: PERIODS
  };
  assert.throws(() => refreshRuntime(runtime, missingJev, PERIOD_GRAPH),
    /缺少画布声明的服务.*jev-a/);
});

test('refreshRuntime refuses to drop a period still referenced by a condition node', () => {
  const runtime = createRuntime(CATALOG, { modelId: 'fast', jevId: 'jev-a' });
  const before = JSON.stringify(runtime);
  /* 目录不再有 peak：点名拒绝（只刷成 [] 会在服务端 enable 时变成 unknown_period）。 */
  const withoutPeak = {
    providers: [LOCAL], models: [FAST], services: [SERVICE_A], periods: [PERIODS[1]]
  };
  assert.throws(() => refreshRuntime(runtime, withoutPeak, PERIOD_GRAPH),
    /时段目录缺少画布条件引用的时段：peak/);
  assert.equal(JSON.stringify(runtime), before, 'a refused refresh mutated its input runtime');

  /* 目录缺少未被条件引用的时段不算错误：time_periods 仍按目录全量重建。 */
  const withoutNight = {
    providers: [LOCAL], models: [FAST], services: [SERVICE_A], periods: [PERIODS[0]]
  };
  const refreshed = refreshRuntime(runtime, withoutNight, PERIOD_GRAPH);
  assert.deepEqual(refreshed.time_periods.map(period => period.name), ['peak']);
  assert.deepEqual(validateRuntime(refreshed), []);

  /* 多个缺失时段全部点名：去重并保持条件节点出现顺序。 */
  const graphBoth = periodGraph([['peak', 'night', 'peak']]);
  const emptyPeriods = {
    providers: [LOCAL], models: [FAST], services: [SERVICE_A], periods: []
  };
  assert.throws(() => refreshRuntime(runtime, emptyPeriods, graphBoth),
    /时段目录缺少画布条件引用的时段：peak, night/);

  /* 没有条件节点（或没有 graph）时刷新不引入时段错误：契约要求显式 graph 才能
     证明引用，调用方（检查器刷新按钮）总是传入当前画布 root。 */
  assert.doesNotThrow(() => refreshRuntime(runtime, emptyPeriods, { nodes: [] }));
});

test('runtime catalog errors are localized through the shared dictionary', async () => {
  const { setLang } = await import('../router/console/i18n.js');
  setLang('en');
  try {
    assert.throws(() => createRuntime({ models: [], providers: [], services: [], periods: [] }),
      /model catalog is empty/i);
    const runtime = createRuntime(CATALOG, { modelId: 'fast', jevId: 'jev-a' });
    assert.throws(() => refreshRuntime(runtime,
      { providers: [LOCAL], models: [FAST], services: [SERVICE_A], periods: [] },
      PERIOD_GRAPH),
      /time-period catalog is missing periods referenced by canvas conditions: peak/i);
    assert.throws(() => refreshRuntime(runtime,
      { providers: [LOCAL], models: [BACKUP], services: [SERVICE_A], periods: PERIODS },
      PERIOD_GRAPH),
      /missing canvas-declared models: fast/i);
  } finally {
    setLang('zh');
  }
  /* 默认中文界面保留既有文案（调用方/提示依赖）。 */
  assert.throws(() => createRuntime({ models: [], providers: [], services: [], periods: [] }),
    /模型目录为空/);
});
