/* RTRuntime · 画布自包含运行配置的纯函数层
   ------------------------------------------------------------------
   ES module（浏览器 <script type="module"> / Node import 同一份源码）。

   本模块只做纯函数：不读 DOM、不读 window、不发请求，也不读数据库；
   数据库目录由 app.js 通过 /admin/providers、/admin/models、/admin/jev、
   /admin/time-periods 读出后作为 catalog 传进来。

   事实源（Python）：router/routing/runtime.py、router/jev/config.py、
   router/jev/service.py、router/jev/window.py、router/routing/conditions.py。
   画布的四个运行字段 backends / jev / jev_window / time_periods 必须成组出现：
   createRuntime() 只会从完整目录生成完整 runtime；目录为空时抛错而不是造一个
   空 runtime（服务端会拒绝空快照，画布也不应产生「待保存再物化」的中间态）。

   窗口策略：jev_window 属于所选 JEV 服务。selectJev(runtime, service) 原子地
   同时替换 jev 与 jev_window；选择「不使用 JEV」（service = null）时只把 jev
   置为 null、保留当前 jev_window（Python 同样要求 jev_window 恒为对象，窗口在
   jev:null 时不参与执行）。selectJev()/addRuntimeModel()/refreshRuntime() 都返回
   深拷贝，绝不修改入参。

   错误文案：用户可见的目录缺失错误走 ../i18n.js 的共享键（zh/en 对称）；
   本模块自身仍不读 DOM、不发请求。
   ------------------------------------------------------------------ */
import { RUNTIME_DEFAULTS } from './graph.js';
import { tf } from '../i18n.js';

/* 与 router/jev/window.py 的默认值一致：没有 JEV 服务时的合法窗口。 */
export const DEFAULT_WINDOW = Object.freeze({ input_tokens: 1024, context_tokens: 4096 });

/* 与 Python snapshot_dict() 的白名单一致：服务快照永不含真实密钥。 */
const SERVICE_SNAPSHOT_FIELDS = [
  'id', 'base_url', 'model', 'key_env', 'timeout_seconds', 'instructions', 'state_template'
];

/* 只接受 JSON 兼容数据：structuredClone 保真度高于 JSON 往返（不把 undefined
   键静默丢掉），也不存在 Date/Map 等需要特殊语义的输入。 */
function clone(value) {
  return value === undefined ? undefined : structuredClone(value);
}

/* 用户可见的目录缺失错误：message 按当前界面语言渲染，同时保留 key/params
   方便测试与调用方定位（不把 zh 文案写死在纯函数层）。 */
function catalogError(key, params) {
  const error = new Error(tf(key, params || {}));
  error.key = key;
  error.params = params || {};
  return error;
}

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function hasValue(value) {
  return value !== undefined && value !== null;
}

/* 只复制模型目录的非密钥字段（Python _models 的投影）。 */
export function publicModel(model) {
  const source = isRecord(model) ? model : {};
  return {
    id: source.id,
    upstream: source.upstream,
    provider: source.provider,
    profile: isRecord(source.profile) ? clone(source.profile) : {}
  };
}

/* 只复制 provider 目录的非密钥字段（Python _providers 的投影）。 */
export function publicProvider(provider) {
  const source = isRecord(provider) ? provider : {};
  return {
    name: source.name,
    base_url: source.base_url,
    key_env: typeof source.key_env === 'string' ? source.key_env : '',
    reasoning_format: source.reasoning_format || RUNTIME_DEFAULTS.reasoning_format
  };
}

/* 服务目录项 → 画布 jev 快照：白名单字段 + 超时默认值，绝不带 api_key/
   key_present/key_source 等密钥或派生密钥状态。 */
function serviceSnapshot(service) {
  const source = isRecord(service) ? service : {};
  const snapshot = {};
  SERVICE_SNAPSHOT_FIELDS.forEach(field => {
    if (field === 'key_env') {
      snapshot.key_env = typeof source.key_env === 'string' ? source.key_env : '';
      return;
    }
    if (field === 'timeout_seconds') {
      snapshot.timeout_seconds = hasValue(source.timeout_seconds)
        ? source.timeout_seconds : RUNTIME_DEFAULTS.timeout_seconds;
      return;
    }
    if (field === 'instructions') {
      snapshot.instructions = hasValue(source.instructions)
        ? source.instructions : RUNTIME_DEFAULTS.instructions;
      return;
    }
    if (field === 'state_template') {
      snapshot.state_template = hasValue(source.state_template)
        ? source.state_template : RUNTIME_DEFAULTS.state_template;
      return;
    }
    snapshot[field] = source[field];
  });
  return snapshot;
}

function serviceWindow(service) {
  const source = isRecord(service) ? service : {};
  return {
    input_tokens: hasValue(source.input_tokens) ? source.input_tokens : DEFAULT_WINDOW.input_tokens,
    context_tokens: hasValue(source.context_tokens) ? source.context_tokens : DEFAULT_WINDOW.context_tokens
  };
}

function catalogList(catalog, field) {
  const source = isRecord(catalog) ? catalog : {};
  return Array.isArray(source[field]) ? source[field].filter(isRecord) : [];
}

/* 原子替换所选 JEV：service 与 jev_window 必须来自同一个服务，绝不混用。
   service = null → jev:null（窗口保留，见文件头策略说明）。返回深拷贝。 */
export function selectJev(runtime, service) {
  const next = clone(isRecord(runtime) ? runtime : {});
  if (service === null || service === undefined) {
    next.jev = null;
    return next;
  }
  next.jev = serviceSnapshot(service);
  next.jev_window = serviceWindow(service);
  return next;
}

/* 把数据库目录里的模型（及 provider）补进画布快照；同 ID 已存在时不覆盖快照，
   数据库差异只作为检查提示（/admin/try 的 runtime_mismatch），绝不静默改写。
   返回深拷贝。 */
export function addRuntimeModel(runtime, model, provider) {
  const next = clone(isRecord(runtime) ? runtime : {});
  if (!isRecord(next.backends)) next.backends = { providers: [], models: [] };
  if (!Array.isArray(next.backends.providers)) next.backends.providers = [];
  if (!Array.isArray(next.backends.models)) next.backends.models = [];
  const target = isRecord(model) ? model : {};
  if (target.id && !next.backends.models.some(item => item && item.id === target.id)) {
    next.backends.models.push(publicModel(target));
  }
  const sourceProvider = isRecord(provider) ? provider : null;
  if (sourceProvider && sourceProvider.name &&
      !next.backends.providers.some(item => item && item.name === sourceProvider.name)) {
    next.backends.providers.push(publicProvider(sourceProvider));
  }
  return next;
}

/* 新建画布的初始快照：从当前数据库目录选一个模型（指定 modelId 或目录第一个）
   和指定 JEV 服务（或 null），生成完整四字段 runtime。目录没有模型时抛明确错误，
   绝不创建空 runtime。 */
export function createRuntime(catalog, options) {
  const opts = isRecord(options) ? options : {};
  const models = catalogList(catalog, 'models');
  if (!models.length) {
    throw catalogError('cv.runtime.errNoModels');
  }
  const model = hasValue(opts.modelId)
    ? models.find(item => item && item.id === opts.modelId)
    : models[0];
  if (!model) {
    throw catalogError('cv.runtime.errModelUnknown', { id: String(opts.modelId) });
  }
  const providers = catalogList(catalog, 'providers');
  const provider = providers.find(item => item && item.name === model.provider);
  if (!provider) {
    throw catalogError('cv.runtime.errProviderMissing', {
      model: String(model.id), provider: String(model.provider)
    });
  }
  const runtime = {
    backends: {
      providers: [publicProvider(provider)],
      models: [publicModel(model)]
    },
    jev: null,
    jev_window: clone(DEFAULT_WINDOW),
    time_periods: clone(Array.isArray(catalog && catalog.periods)
      ? catalog.periods.filter(isRecord) : [])
  };
  if (!hasValue(opts.jevId) || opts.jevId === '') return runtime;
  const service = catalogList(catalog, 'services').find(item => item && item.id === opts.jevId);
  if (!service) {
    throw catalogError('cv.runtime.errJevUnknown', { id: String(opts.jevId) });
  }
  return selectJev(runtime, service);
}

/* 条件节点 source=periods 引用的时段名（去重、保持出现顺序）。刷新运行配置时
   必须确认这些名字仍在数据库目录里：否则刷新后草稿能过本地校验，却会在服务端
   enable 时以 unknown_period 失败。 */
function referencedPeriodNames(graph) {
  const nodes = isRecord(graph) && Array.isArray(graph.nodes) ? graph.nodes : [];
  const names = [];
  nodes.forEach(node => {
    if (!isRecord(node) || node.type !== 'condition' || node.source !== 'periods') return;
    const periods = Array.isArray(node.periods) ? node.periods : [];
    periods.forEach(name => {
      if (typeof name === 'string' && name.trim() !== '' && names.indexOf(name) < 0) names.push(name);
    });
  });
  return names;
}

function catalogPeriodNames(catalog) {
  return catalogList(catalog, 'periods')
    .map(period => (period && typeof period.name === 'string' ? period.name : ''))
    .filter(name => name !== '');
}

/* 用户显式「从数据库刷新运行配置」：按画布声明的模型/provider/JEV 重建快照。
   目录缺少画布声明的模型、provider、JEV 或条件节点引用的时段时抛明确错误并
   保持草稿不变，不静默删除引用（删除 target 引用应由用户显式编辑）。graph 是
   当前画布 root（含条件节点），用于收集时段引用；缺失时视为无引用节点。
   返回深拷贝。 */
export function refreshRuntime(runtime, catalog, graph) {
  const source = isRecord(runtime) ? runtime : {};
  const declaredModels = (isRecord(source.backends) && Array.isArray(source.backends.models))
    ? source.backends.models.filter(isRecord) : [];
  const providers = catalogList(catalog, 'providers');
  const models = catalogList(catalog, 'models');
  const missing = declaredModels
    .filter(item => !models.some(candidate => candidate && candidate.id === item.id))
    .map(item => String(item.id));
  if (missing.length) {
    throw catalogError('cv.runtime.errModelsMissing', { models: missing.join(', ') });
  }
  const nextProviders = [];
  const nextModels = [];
  declaredModels.forEach(declared => {
    const model = models.find(candidate => candidate && candidate.id === declared.id);
    const provider = providers.find(candidate => candidate && candidate.name === model.provider);
    if (!provider) {
      throw catalogError('cv.runtime.errProviderMissing', {
        model: String(model.id), provider: String(model.provider)
      });
    }
    if (!nextModels.some(item => item.id === model.id)) nextModels.push(publicModel(model));
    if (!nextProviders.some(item => item.name === provider.name)) {
      nextProviders.push(publicProvider(provider));
    }
  });
  /* 刷新会整组替换 time_periods：被条件节点引用、但目录里已不存在的时段必须
     点名拒绝，不能静默丢失（服务端 enable 会报 unknown_period）。 */
  const declaredPeriods = referencedPeriodNames(graph);
  const knownPeriods = catalogPeriodNames(catalog);
  const missingPeriods = declaredPeriods.filter(name => knownPeriods.indexOf(name) < 0);
  if (missingPeriods.length) {
    throw catalogError('cv.runtime.errPeriodsMissing', { periods: missingPeriods.join(', ') });
  }
  const next = {
    backends: { providers: nextProviders, models: nextModels },
    jev: null,
    jev_window: clone(isRecord(source.jev_window) ? source.jev_window : DEFAULT_WINDOW),
    time_periods: clone(Array.isArray(catalog && catalog.periods)
      ? catalog.periods.filter(isRecord) : [])
  };
  const jevId = isRecord(source.jev) ? source.jev.id : null;
  if (jevId) {
    const service = catalogList(catalog, 'services').find(item => item && item.id === jevId);
    if (!service) {
      throw catalogError('cv.runtime.errJevMissing', { id: String(jevId) });
    }
    return selectJev(next, service);
  }
  return next;
}
