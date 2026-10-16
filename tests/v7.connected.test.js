/* console-v7 接线层验证（Task 12，spec §9.1-9.3）—— node tests/v7.connected.test.js
   jsdom + 全局 fetch mock，断言逐字采用 task-12-brief Step 1：
   - http(s) 打开且 /admin/tree 1.5s 内 200 → connected；
   - 引导序列七个 GET 全部发出；
   - 树页渲染 API 数据（规范键经 /admin/i18n/zh 映射为中文显示词）；
   - doTag 走 POST /admin/tree/try，信号条与信号轨按响应渲染；
   - 任一引导请求失败 / 非 http 打开 → 演示模态（window.SR_MODE === 'demo'，
     内置数据与交互不变 —— §9.3 演示模态不变式）。
   mock 形状 = sr/server.py 已实现口径（spec §9.2）：GET /admin/tree 的
   nodes[].branches[].cond/action 用规范键，POST /admin/tree/try 返回
   {tagger, tags, path, decision}。 */
const fs = require('fs');
const path = require('path');
let JSDOM;
try { ({ JSDOM } = require('jsdom')); }
catch { ({ JSDOM } = require(process.env.JSDOM_PATH ||
  '/Users/yuansu/.workbuddy/binaries/node/workspace/node_modules/jsdom')); }

const FILE = process.env.TARGET || path.join(__dirname, '..', 'console-v7.html');
const html = fs.readFileSync(FILE, 'utf8');

/* ── mock 数据（与 sr/i18n.py MESSAGES['zh']、sr/engine.py 种子树同源） ── */
const I18N_ZH = {"tag.domain":"学科","tag.complexity":"难度","tag.fact_check":"需核实","tag.similarity":"相似度","tag.keyword":"关键词","tag.modality":"要什么","tag.language":"语言","tag.structure":"结构","tag.context":"长度","tag.turns":"轮次","tag.jailbreak":"可疑","tag.pii":"敏感信息","tag.tenant":"身份","tag.time_window":"时段","tag.event":"事件","tag.user_feedback":"上轮反馈","tag.reask":"重复提问","domain.computer_science":"计算机","domain.math":"数学","domain.physics":"物理","domain.chemistry":"化学","domain.biology":"生物","domain.engineering":"工程","domain.economics":"经济","domain.business":"商科","domain.law":"法律","domain.philosophy":"哲学","domain.psychology":"心理","domain.health":"医学","domain.history":"历史","domain.other":"其他","band.complexity.0":"简单","band.complexity.1":"难","band.jailbreak.0":"很正常","band.jailbreak.1":"有点可疑","band.jailbreak.2":"很可疑","band.jailbreak.3":"非常可疑","band.context.0":"很短","band.context.1":"一般","band.context.2":"长","band.context.3":"超长","band.similarity.0":"不像","band.similarity.1":"有点像","band.similarity.2":"很像","band.turns.0":"首轮","band.turns.1":"聊了几轮","band.turns.2":"长对话","fact_check.needs_check":"需要核实","fact_check.no_check":"不用核实","modality.text":"文字","modality.image":"图片","modality.text_image":"文字加图片","language.zh":"中文","language.en":"英文","language.other":"其他","structure.code":"有代码","structure.json":"有 JSON","structure.multiline":"多行文字","structure.plain":"普通文本","time_window.weekday_day":"工作日白天","time_window.night":"夜间","time_window.weekend":"周末","event.incident":"故障报告","event.payment_issue":"支付异常","event.none":"没有","user_feedback.satisfied":"满意","user_feedback.need_clarification":"想让我说清楚","user_feedback.wrong_answer":"答错了","user_feedback.want_different":"想换个说法","reask.yes":"是","reask.no":"否","pii.phone_number":"手机号","pii.cn_id_card":"身份证号","pii.cn_bank_card":"银行卡号","pii.email_address":"邮箱","pii.password":"密码","conf.0":"很不确定","conf.1":"不太确定","conf.2":"比较确定","conf.3":"非常确定","caps.text":"文字","caps.image":"图片","caps.long_ctx":"长上下文","caps.very_long_ctx":"超长","caps.cheap":"便宜","caps.medium":"中等","caps.expensive":"贵","caps.fast":"快","caps.medium_speed":"中速","caps.slow":"慢","think.off":"关闭","think.light":"轻度","think.deep":"深度","op.is":"是","op.in":"属于","op.gte":"至少","op.lte":"至多","op.contains":"含有","ui.add_node":"新增节点","ui.save":"保存","ui.cancel":"取消","ui.delete":"删除","ui.demo_mode":"演示模式","ui.block_msg":"拦截消息","ui.block_msg_zh":"拦截消息（中文）","ui.block_msg_en":"拦截消息（英文）","block.default":"请求被安全策略拦截"};

const BLOCK_MSG = { zh: '请求被安全策略拦截', en: 'Blocked by safety policy' };
/* balanced 种子树（engine.py TEMPLATES.balanced 逐节点；desc 加「（来自 API）」
   后缀以证明树页渲染的是 API 数据而非内置演示数据） */
const TREE_JSON = { version: 3, nodes: [
  { idx: 0, kind: 'sticky', name: '会话沿用',
    desc: '同一个会话里，沿用第一次判断的结果（省算力，也省模型切换的开销）', enabled: true,
    branches: [
      { idx: 100, cond: [{ tag: 'user_feedback', op: 'in', values: ['satisfied', 'need_clarification'] }], action: { kind: 'special', special: 'sticky' }, trial: null, enabled: true, kids: [] },
      { idx: 101, cond: [{ tag: 'user_feedback', op: 'is', values: ['wrong_answer'] }], action: { kind: 'special', special: 'ladder_up' }, trial: null, enabled: true, kids: [] },
      { idx: 102, cond: [{ tag: 'user_feedback', op: 'is', values: ['want_different'] }], action: { kind: 'special', special: 'ladder_next' }, trial: null, enabled: true, kids: [] }] },
  { idx: 1, kind: 'normal', name: '安全', desc: '先拦危险请求', enabled: true,
    branches: [
      { idx: 10, cond: [{ tag: 'jailbreak', op: 'gte', values: [2] }], action: { kind: 'block', block_msg: BLOCK_MSG }, trial: null, enabled: true, kids: [] },
      { idx: 11, cond: [{ tag: 'pii', op: 'contains', values: ['cn_id_card', 'cn_bank_card', 'password'] }], action: { kind: 'block', block_msg: BLOCK_MSG }, trial: null, enabled: true, kids: [] }] },
  { idx: 2, kind: 'normal', name: '多媒体', desc: '要生成图片的请求（位于成本控制之前）（来自 API）', enabled: true,
    branches: [
      { idx: 20, cond: [{ tag: 'modality', op: 'is', values: ['image'] }], action: { kind: 'route', model: 'sdxl-image' }, trial: null, enabled: true, kids: [] },
      { idx: 21, cond: [{ tag: 'modality', op: 'is', values: ['text_image'] }], action: { kind: 'route', model: 'llava-omni' }, trial: null, enabled: true,
        kids: [
          { idx: 210, cond: [{ tag: 'complexity', op: 'is', values: [1] }], action: { kind: 'route', model: 'llava-omni', think: 'deep' }, trial: null, enabled: true, kids: [] },
          { idx: 211, cond: [{ not: { tag: 'complexity', op: 'is', values: [1] } }], action: { kind: 'route', model: 'llava-omni' }, trial: null, enabled: true, kids: [] }] }] },
  { idx: 3, kind: 'normal', name: '编程与推理', desc: '按难度换模型（来自 API）', enabled: true,
    branches: [
      { idx: 30, cond: [{ tag: 'domain', op: 'is', values: ['computer_science'] },
                        { tag: 'complexity', op: 'is', values: [1], conf_min: 2 }],
        action: { kind: 'route', model: 'qwen3-32b', think: 'deep', fallback: ['qwen3-8b'] }, trial: null, enabled: true, kids: [] },
      { idx: 31, cond: [{ tag: 'complexity', op: 'is', values: [1] }], action: { kind: 'route', model: 'qwen3-32b', think: 'deep' }, trial: null, enabled: true, kids: [] },
      { idx: 32, cond: [{ tag: 'complexity', op: 'is', values: [1] }], action: { kind: 'route', model: 'qwen3-32b', think: 'light' }, trial: null, enabled: true, kids: [] }] },
  { idx: 4, kind: 'normal', name: '法律与事实', desc: '需要严肃对待的问题（来自 API）', enabled: true,
    branches: [
      { idx: 40, cond: [{ or: [{ tag: 'domain', op: 'is', values: ['law'] },
                               { tag: 'fact_check', op: 'is', values: ['needs_check'] }] }],
        action: { kind: 'route', model: 'qwen3-32b', think: 'light', fallback: ['qwen3-8b'] }, trial: null, enabled: true, kids: [] }] },
  { idx: 5, kind: 'normal', name: '成本控制', desc: '简单请求走小模型（来自 API）', enabled: true,
    branches: [
      { idx: 50, cond: [{ tag: 'complexity', op: 'is', values: [0] },
                        { tag: 'context', op: 'in', values: [0, 1] }], action: { kind: 'route', model: 'qwen3-8b' }, trial: null, enabled: true, kids: [] },
      { idx: 51, cond: [{ tag: 'turns', op: 'is', values: [2] }], action: { kind: 'route', model: 'qwen3-8b' }, trial: null, enabled: true, kids: [] },
      { idx: 52, cond: [{ tag: 'time_window', op: 'is', values: ['night'] }], action: { kind: 'route', model: 'qwen3-8b' }, trial: null, enabled: true, kids: [] }] },
  { idx: 6, kind: 'normal', name: '专属', desc: '指定人群走指定模型（来自 API）', enabled: true,
    branches: [
      { idx: 60, cond: [{ tag: 'tenant', op: 'is', values: ['premium'] }], action: { kind: 'route', model: 'qwen3-32b' }, trial: null, enabled: true, kids: [] }] },
  { idx: 9, kind: 'fallback', name: '其他（兜底）', desc: '全部未命中时（来自 API）', enabled: true,
    branches: [
      { idx: 90, cond: [], action: { kind: 'route', model: 'qwen3-8b' }, trial: null, enabled: true, kids: [] }] }] };

const rule = (value, tag) => ({ tag, value, confidence: null, conf_band: null, is_rule: true });
const model_ = (tag, value, confidence, conf_band) => ({ tag, value, confidence, conf_band, is_rule: false });
/* POST /admin/tree/try 响应（§9.2 形状；学科=计算机、难度=难 → 命中 3.0） */
const TRY_JSON = {
  tagger: 'heuristic',
  tags: [
    model_('domain', 'computer_science', 0.61, 2),
    model_('complexity', 1, 0.55, 1),
    model_('fact_check', 'no_check', 0.72, 2),
    model_('similarity', 0, 0.70, 2),
    rule([], 'keyword'),
    model_('modality', 'text', 0.94, 3),
    rule('zh', 'language'),
    rule('plain', 'structure'),
    rule(1, 'context'),
    rule(0, 'turns'),
    model_('jailbreak', 0, 0.96, 3),
    model_('pii', [], 0.88, 2),
    rule('anonymous', 'tenant'),
    rule('weekday_day', 'time_window'),
    rule('none', 'event'),
    model_('user_feedback', 'satisfied', 0.90, 2),
    model_('reask', 'no', 0.88, 2)],
  path: [{ node: 0, branch: null, result: 'miss', reason: 'new_session' },
         { node: 1, branch: null, result: 'miss', reason: 'no_match' },
         { node: 2, branch: null, result: 'miss', reason: 'no_match' },
         { node: 3, branch: 0, result: 'hit' }],
  also_match: [{ node: 9, branch: 0 }],
  decision: { kind: 'route', model: 'qwen3-32b', think: 'deep', fallback: ['qwen3-8b'],
              source: 'tree', hit_path: [3, 0] },
  answer_ms: 12.3 };

const SETTINGS_JSON = { session_sticky: '1', session_idle: '1800',
  sticky_detect_feedback: '0', budget_protect: '1', budget_water: '0.8',
  budget_route: 'qwen3-8b', max_retries: '2', batch_window_ms: '5',
  retention_days: '30', ladder: 'qwen3-8b,qwen3-32b', tree_version: '3' };
const BANDS_JSON = { conf_thresholds: [0.30, 0.60, 0.85],
  tag_thresholds: [{ tag: 'complexity', thresholds: [0.7] },
                   { tag: 'jailbreak', thresholds: [0.1, 0.4, 0.8] }] };
const PROVIDERS_JSON = [
  { name: '本地', proto: 'openai', base_url: 'http://127.0.0.1:8123/v1', key_env: '',
    extra: {}, ok: true, break_reason: null },
  { name: ' SiliconFlow', proto: 'openai', base_url: 'https://api.siliconflow.cn/v1',
    key_env: 'SILICONFLOW_API_KEY', extra: {}, ok: true, break_reason: null }];
const MODELS_JSON = [
  { id: 'qwen3-8b', provider: '本地', proto: 'openai', upstream: 'Qwen/Qwen3-8B',
    caps: ['text', 'cheap', 'fast'], aliases: ['small'], step: 0,
    price_in: 0.5, price_out: 2, ok: true, break_reason: null, enabled: true, on_ladder: true },
  { id: 'qwen3-32b', provider: '本地', proto: 'openai', upstream: 'Qwen/Qwen3-32B',
    caps: ['text', 'medium'], aliases: ['auto', 'big'], step: 1,
    price_in: 2, price_out: 8, ok: true, break_reason: null, enabled: true, on_ladder: true }];
const TRAFFIC_JSON = { total: 1, tree_version: 7, rows: [
  { id: 'r_01J8K9ZZZ', ts: '2026-10-15T14:01:22+08:00', proto: 'openai',
    model_asked: 'auto',
    tags_brief: [{ tag: 'domain', value: 'computer_science' }],
    hit_branch: '3.0', routed_to: 'qwen3-32b', session_tag: 'new',
    outcome: 'upstream', status: 200, answer_ms: 12.1, upstream_ms: 1800,
    tree_version: 7 }] };
const TRAFFIC_DETAIL_JSON = { id: 'r_01J8K9ZZZ', ts: '2026-10-15T14:01:22+08:00',
  proto: 'openai', model_asked: 'auto', hit_branch: '3.0', routed_to: 'qwen3-32b',
  session_id: 's1', session_tag: 'new', outcome: 'upstream', status: 200,
  answer_ms: 12.1, upstream_ms: 1800, cost: 0.001,
  tags: TRY_JSON.tags };
const REPLAY_JSON = { id: 'r_01J8K9ZZZ', tags: TRY_JSON.tags,
  was: { hit_branch: '3.0', routed_to: 'qwen3-32b' },
  now: { hit_branch: '3.0', routed_to: 'qwen3-32b' }, diff: {},
  verdict: '与当时一致：hit_branch 3.0，routed_to qwen3-32b',
  decision: TRY_JSON.decision };
const TREE_SAVED_JSON = JSON.parse(JSON.stringify(TREE_JSON)); TREE_SAVED_JSON.version = 4;

/* ── mock fetch + JSDOM 装配 ── */
const jsonResponse = (data, status = 200) => Promise.resolve(
  ({ ok: status >= 200 && status < 300, status, json: async () => data }));

function makeDom({ url = 'http://127.0.0.1:8899/', routes = {} } = {}) {
  const calls = [];
  const dom = new JSDOM(html, {
    runScripts: 'dangerously', pretendToBeVisual: true, url,
    beforeParse(window) {
      window.fetch = (u, opts = {}) => {
        const p = String(u);
        calls.push([p, { method: (opts && opts.method) || 'GET',
                         body: opts && opts.body != null ? String(opts.body) : undefined }]);
        const h = routes[p];
        if (h) return h((opts && opts.method) || 'GET', opts);
        return jsonResponse({});
      };
      window.__calls = calls;
      window.Element.prototype.scrollIntoView = function () {};
      window.Element.prototype.getBoundingClientRect = function () {
        return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
      };
    },
  });
  return { dom, window: dom.window, calls };
}

const CONNECT_ROUTES = {
  '/admin/i18n/zh':   () => jsonResponse(I18N_ZH),
  '/admin/providers': () => jsonResponse(PROVIDERS_JSON),
  '/admin/models':    () => jsonResponse(MODELS_JSON),
  '/admin/tree':      (m) => m === 'GET' ? jsonResponse(TREE_JSON) : jsonResponse(TREE_JSON),
  '/admin/bands':     () => jsonResponse(BANDS_JSON),
  '/admin/settings':  () => jsonResponse(SETTINGS_JSON),
  '/admin/traffic':   () => jsonResponse(TRAFFIC_JSON),
  '/admin/tree/try':  () => jsonResponse(TRY_JSON),
  '/admin/traffic/r_01J8K9ZZZ': () => jsonResponse(TRAFFIC_DETAIL_JSON),
  '/admin/traffic/r_01J8K9ZZZ/replay': () => jsonResponse(REPLAY_JSON),
  '/admin/tree/estimate': () => jsonResponse({ sample_days: 7, daily: 780, ratio: 0.018,
    from_fallback: 640, from_other: 140, cost_delta: -0.03 }),
  '/admin/tree/nodes': () => jsonResponse(TREE_SAVED_JSON),
};

const SEVEN = ['/admin/bands', '/admin/i18n/zh', '/admin/models', '/admin/providers',
               '/admin/settings', '/admin/traffic', '/admin/tree'];
const deepEqual = (a, b) => JSON.stringify(a) === JSON.stringify(b);
async function until(f, ms = 5000, what = 'condition') {
  const t0 = Date.now();
  while (!f()) {
    if (Date.now() - t0 > ms) throw new Error('等待超时：' + what);
    await new Promise(r => setTimeout(r, 25));
  }
}

/* ── 断言工具（沿用既有套件风格） ── */
let pass = 0, fail = 0;
const bad = [];
function t(name, cond, detail) {
  if (cond) { pass++; console.log('  ✓ ' + name); }
  else { fail++; bad.push(name + (detail ? ' → ' + detail : '')); console.log('  ✗ ' + name + (detail ? ' → ' + detail : '')); }
}
function group(g) { console.log('\n── ' + g); }
const pend = (ms) => new Promise(r => setTimeout(r, ms || 600));

(async () => {
  console.log('══ console-v7 接线层验证 ══\n文件：' + FILE);

  /* ════ A. 已连接模态 ════ */
  group('已连接模态 · bootDetect 与引导序列');
  const A = makeDom({ routes: CONNECT_ROUTES });
  const w = A.window, doc = w.document;
  await until(() => w.SR_MODE === 'connected', 5000, 'connected');
  t('http(s) 打开且 /admin/tree 1.5s 内 200 → connected', w.SR_MODE === 'connected', w.SR_MODE);
  t('接线层 Produces 全局函数就位（bootDetect/apiTreeToLocal/pathToRows/doTagRemote/renderAllRemote）',
    [w.bootDetect, w.apiTreeToLocal, w.pathToRows, w.doTagRemote, w.renderAllRemote]
      .every(f => typeof f === 'function'));
  const gets = A.calls.filter(c => c[1].method === 'GET').map(c => c[0]);
  t('引导序列七个 GET 全部发出', deepEqual(gets.slice().sort(), SEVEN.slice().sort()),
    JSON.stringify(gets));

  group('已连接模态 · 树页渲染 API 数据');
  t('树页渲染 API 数据（规范键经 i18n 映射为中文）',
    /编程与推理/.test(doc.getElementById('treeWrap').textContent));
  t('渲染的确实是 API 数据（desc 来自 GET /admin/tree，内置数据无此文案）',
    /按难度换模型（来自 API）/.test(doc.getElementById('treeWrap').textContent));
  t('规范键经 i18n 映射为中文显示词（domain.computer_science → 计算机）',
    /计算机/.test(doc.getElementById('treeWrap').textContent));
  t('两档制难度落中文显示词（band.complexity.1 → 难）',
    /难/.test(doc.getElementById('treeWrap').textContent));
  t('特殊去向保留语义（sticky → 沿用上次的模型）',
    /沿用上次的模型/.test(doc.getElementById('treeWrap').textContent));
  t('兜底分支保留（什么情况都算）',
    /什么情况都算/.test(doc.getElementById('treeWrap').textContent));
  t('树统计行按 API 树重算', /8 个节点 · \d+ 条分支/.test(doc.getElementById('statline').textContent),
    doc.getElementById('statline').textContent);
  const xget = await new Promise(res => {
    const s = doc.createElement('script');
    s.textContent = 'window.__XTREE = TREE; window.__CONF_CUTS = CONF.cuts;';
    doc.body.appendChild(s);
    res(true);
  });
  const treeNow = w.__XTREE;
  t('apiTreeToLocal 写回本地 TREE 常量（原地替换，8 节点）',
    Array.isArray(treeNow) && treeNow.length === 8, treeNow && treeNow.length);
  t('写回的节点带 API idx（供 pathToRows 定位）',
    treeNow.some(n => n._idx === 3 && n.title === '编程与推理'));

  group('已连接模态 · 其余四页来自 API');
  t('模型页统计来自 API（2 个模型）',
    /2 个模型 · 2 正常/.test(doc.getElementById('mstat').textContent),
    doc.getElementById('mstat').textContent);
  t('能力标签经 i18n 映射（caps.cheap → 便宜）',
    /便宜/.test(doc.getElementById('modbody').textContent));
  t('流量页行来自 API（data-rid 为真实请求 id）',
    doc.querySelector('#p-traffic tbody tr') &&
    doc.querySelector('#p-traffic tbody tr').getAttribute('data-rid') === 'r_01J8K9ZZZ');
  t('把握度分档来自 API（conf_thresholds 写入 CONF.cuts）',
    JSON.stringify(w.__CONF_CUTS) === JSON.stringify([0.30, 0.60, 0.85]),
    JSON.stringify(w.__CONF_CUTS));

  group('已连接模态 · doTag 走 POST /admin/tree/try');
  doc.getElementById('reqtext').value = '帮我设计一个订单系统分片方案';
  w.setSess('new');
  w.doTag();
  await until(() => A.calls.some(c => c[0] === '/admin/tree/try' && c[1].method === 'POST'),
    5000, 'POST /admin/tree/try');
  await until(() => doc.querySelectorAll('#railbody .stn').length > 0, 5000, '信号轨渲染');
  await pend(500);
  t('doTag 走 POST /admin/tree/try，信号条与信号轨按响应渲染',
    A.calls.some(c => c[0] === '/admin/tree/try' && c[1].method === 'POST') &&
    doc.querySelectorAll('#railbody .stn.hit').length === 1,
    'hit 站点 ' + doc.querySelectorAll('#railbody .stn.hit').length);
  t('信号轨恰有一个通电站点', doc.querySelectorAll('#railbody .stn.hit').length === 1);
  t('信号轨命中站点带 chip 条件与去向',
    doc.querySelectorAll('#railbody .stn.hit .chip').length > 0 &&
    /qwen3-32b/.test(doc.querySelector('#railbody .stn.hit').textContent));
  t('miss 站点不列条件芯片（沿用 demo 口径）',
    doc.querySelectorAll('#railbody .stn.miss .chip').length === 0,
    '实际 ' + doc.querySelectorAll('#railbody .stn.miss .chip').length);
  t('本地 inferTags/evalTree 不再执行（响应 path 直render）',
    /这是新会话，起点不适用/.test(doc.getElementById('railbody').textContent));
  t('判定横幅按响应 decision 渲染（模型 + 回退）',
    /qwen3-32b/.test(doc.getElementById('verdict').textContent) &&
    /qwen3-8b/.test(doc.getElementById('verdict').textContent),
    doc.getElementById('verdict').textContent.slice(0, 90));
  t('思考档位经 i18n 映射（think.deep → 深度）',
    /深度/.test(doc.getElementById('verdict').textContent));
  t('打标耗时来自响应 answer_ms', /12\.3 ms/.test(doc.getElementById('tagtime').textContent),
    doc.getElementById('tagtime').textContent);
  t('信号条只列本次判定用到的信号',
    doc.querySelectorAll('#sigline .sigchip').length > 0 &&
    doc.querySelectorAll('#sigline .sigchip').length < 17,
    '实际 ' + doc.querySelectorAll('#sigline .sigchip').length);
  t('信号墙 17 卡按响应 tags 渲染', doc.querySelectorAll('#tags .gauge').length === 17,
    '实际 ' + doc.querySelectorAll('#tags .gauge').length);
  t('tagger=heuristic → 顶栏显示「启发式打标」（§2.5）',
    /启发式打标/.test(doc.querySelector('.strip').textContent),
    doc.querySelector('.strip').textContent);

  group('已连接模态 · 写路径');
  w.openAddNode();
  w.setName('客服工单');
  w.setDesc('工单类走便宜模型');
  w.pickTag('学科');
  w.toggleVal('计算机');
  w.addAsDraft();
  await until(() => A.calls.some(c => c[0] === '/admin/tree/estimate' && c[1].method === 'POST'),
    5000, 'POST /admin/tree/estimate');
  await pend(400);
  t('新增节点草稿 → 先 POST /admin/tree/estimate（影响预估卡）',
    A.calls.some(c => c[0] === '/admin/tree/estimate' && c[1].method === 'POST'));
  t('影响预估卡按响应渲染（每日 780 / 占比 1.8%）',
    /780/.test(doc.getElementById('impactBox').textContent) &&
    /1\.8/.test(doc.getElementById('impactBox').textContent),
    doc.getElementById('impactBox').textContent.slice(0, 120));
  const tryCallsBefore = A.calls.filter(c => c[0] === '/admin/tree/try').length;
  doc.querySelector('#impactBox .btn.pri').click();          // 确认保存
  await until(() => A.calls.some(c => c[0] === '/admin/tree/nodes' && c[1].method === 'POST'),
    5000, 'POST /admin/tree/nodes');
  await pend(500);
  t('确认保存 → POST /admin/tree/nodes', A.calls.some(c => c[0] === '/admin/tree/nodes' && c[1].method === 'POST'));
  t('保存后重取树（GET /admin/tree）并保持 connected',
    A.calls.filter(c => c[0] === '/admin/tree' && c[1].method === 'GET').length >= 2 &&
    w.SR_MODE === 'connected');
  t('试算改走新树仍可用', (w.setSess('new'), w.doTag(), await until(() =>
    A.calls.filter(c => c[0] === '/admin/tree/try').length > tryCallsBefore, 5000, 'try'),
    true));

  group('已连接模态 · 流量详情与重放');
  w.showReq(0);
  await until(() => A.calls.some(c => c[0] === '/admin/traffic/r_01J8K9ZZZ' && c[1].method === 'GET'),
    5000, 'GET detail');
  await pend(500);
  t('详情拉取 GET /admin/traffic/{id} + POST /admin/tree/try（按 request_id）',
    A.calls.some(c => c[0] === '/admin/traffic/r_01J8K9ZZZ' && c[1].method === 'GET') &&
    A.calls.some(c => c[0] === '/admin/tree/try' && c[1].method === 'POST' &&
      String(c[1].body || '').indexOf('"request_id":"r_01J8K9ZZZ"') >= 0));
  t('详情信号轨按响应渲染', doc.querySelectorAll('#dt-railbody .stn.hit').length === 1,
    '实际 ' + doc.querySelectorAll('#dt-railbody .stn.hit').length);
  w.replay();
  await until(() => A.calls.some(c => c[0] === '/admin/traffic/r_01J8K9ZZZ/replay'),
    5000, 'POST replay');
  await pend(300);
  t('重放走 POST /admin/traffic/{id}/replay 并渲染结论',
    /与当时一致/.test(doc.getElementById('replaybox').textContent),
    doc.getElementById('replaybox').textContent.slice(0, 80));
  w.closeReq();

  /* ════ B. 引导请求失败 → 演示模态（§9.3 不变式） ════ */
  group('演示模态 · 引导失败回退');
  const B = makeDom({ routes: {
    '/admin/providers': () => Promise.reject(new Error('boom')) } });
  await until(() => B.window.SR_MODE === 'demo', 5000, 'demo (providers 失败)');
  t('任一引导请求失败 → 演示模态（window.SR_MODE==="demo"）',
    B.window.SR_MODE === 'demo', B.window.SR_MODE);
  t('演示模态用内置数据渲染（8 节点）',
    B.window.document.querySelectorAll('.tnode').length === 8);
  t('演示模态树仍是内置文案（未被 API 数据污染）',
    /先拦危险请求/.test(B.window.document.getElementById('treeWrap').textContent) &&
    !/（来自 API）/.test(B.window.document.getElementById('treeWrap').textContent));

  const C = makeDom({ routes: { '/admin/tree': () => jsonResponse({ detail: 'x' }, 503) } });
  await until(() => C.window.SR_MODE === 'demo', 5000, 'demo (tree 503)');
  t('引导请求非 200 → 演示模态', C.window.SR_MODE === 'demo', C.window.SR_MODE);

  group('演示模态 · 非 http 打开（file:// 语义）');
  const D = makeDom({ url: 'file:///srv/console-v7.html', routes: {} });
  await pend(300);
  t('file:// 打开 → 演示模态且不发任何请求',
    D.window.SR_MODE === 'demo' && D.calls.length === 0,
    'SR_MODE=' + D.window.SR_MODE + ' calls=' + D.calls.length);

  group('演示模态 · 原行为抽测（信号条 chip 数、熄灯折叠行）');
  const ddoc = D.window.document;
  D.window.setSess('new');
  D.window.doTag();
  await pend(800);
  t('演示 doTag 信号条有 chip（本地 inferTags 仍执行）',
    ddoc.querySelectorAll('#sigline .sigchip').length > 0);
  t('演示信号轨恰有一个通电站点',
    ddoc.querySelectorAll('#railbody .stn.hit').length === 1);
  const dFold = ddoc.querySelector('#railbody .deadfold-btn');
  t('演示熄灯折叠行存在且默认折叠',
    !!dFold && /不再评估/.test(dFold.textContent) &&
    ddoc.querySelector('#railbody .deadlist').style.display === 'none');
  t('演示顶栏无「启发式打标」、无「演示模式」以外的接线痕迹',
    !/启发式打标/.test(ddoc.querySelector('.strip').textContent));

  /* ════ C. 缺陷修复回归（task-12-fix）：详情标题派生 / or 保真回写 / 引导守卫 ════ */
  group('缺陷修复 · connected 详情标题派生（SRW_rowTitle，行无 title 字段）');
  w.showReq(0);
  await until(() => doc.getElementById('dt-title') &&
    doc.getElementById('dt-title').textContent.trim() !== '', 5000, 'dt-title');
  await pend(400);
  const dtTitle = doc.getElementById('dt-title').textContent;
  t('详情页 dt-title 不含 "undefined"（GET /admin/traffic 行没有 title 字段）',
    dtTitle.length > 0 && !/undefined/.test(dtTitle), dtTitle);
  t('标题派生自 tags_brief「标签 · 取值」（学科 · 计算机）',
    dtTitle === '学科 · 计算机', dtTitle);
  t('SRW_rowTitle：tags_brief 缺失退 hit_branch + routed_to',
    w.SRW_rowTitle({ id: 'rX', hit_branch: '3.0', routed_to: 'qwen3-8b' }) === '3.0 → qwen3-8b',
    w.SRW_rowTitle({ id: 'rX', hit_branch: '3.0', routed_to: 'qwen3-8b' }));
  t('SRW_rowTitle：再退 row.id，tags_brief 串截 40 字符',
    w.SRW_rowTitle({ id: 'r9' }) === 'r9' &&
    w.SRW_rowTitle({ tags_brief: [{ tag: 'keyword', value: 'x'.repeat(60) }], id: 'rY' }).length === 40,
    JSON.stringify(w.SRW_rowTitle({ tags_brief: [{ tag: 'keyword', value: 'x'.repeat(60) }], id: 'rY' })));
  w.closeReq();

  group('缺陷修复 · or 子句保真回写（chips 往返结构全等）');
  const OR_TREE_JSON = { version: 9, nodes: [
    { idx: 4, kind: 'normal', name: '法律与事实（or 回归）', desc: 'or 往返不变式（来自 API）', enabled: true,
      branches: [
        { idx: 40, cond: [{ or: [{ tag: 'domain', op: 'is', values: ['law'] },
                                 { tag: 'fact_check', op: 'is', values: ['needs_check'] }] }],
          action: { kind: 'route', model: 'qwen3-32b' }, trial: null, enabled: true, kids: [] },
        { idx: 41, cond: [{ tag: 'domain', op: 'is', values: ['law'] },
                          { or: [{ tag: 'complexity', op: 'is', values: [1] },
                                 { tag: 'fact_check', op: 'is', values: ['needs_check'] }] }],
          action: { kind: 'route', model: 'qwen3-8b' }, trial: null, enabled: true, kids: [] },
        { idx: 42, cond: [{ or: [{ tag: 'domain', op: 'is', values: ['law'] },
                                 { tag: 'domain', op: 'is', values: ['history'] }] },
                          { or: [{ tag: 'complexity', op: 'is', values: [1] },
                                 { tag: 'complexity', op: 'is', values: [0] }] }],
          action: { kind: 'route', model: 'qwen3-8b' }, trial: null, enabled: true, kids: [] },
        { idx: 43, cond: [{ tag: 'complexity', op: 'is', values: [1], conf_min: 2 },
                          { or: [{ tag: 'pii', op: 'contains', values: ['email_address'] },
                                 { tag: 'fact_check', op: 'is', values: ['no_check'] }] }],
          action: { kind: 'route', model: 'qwen3-8b' }, trial: null, enabled: true, kids: [] }] },
    { idx: 9, kind: 'fallback', name: '其他（兜底）', desc: '兜底（来自 API）', enabled: true,
      branches: [{ idx: 90, cond: [], action: { kind: 'route', model: 'qwen3-8b' },
                   trial: null, enabled: true, kids: [] }] }] };
  const E_PUTS = [];
  const E = makeDom({ routes: Object.assign({}, CONNECT_ROUTES, {
    '/admin/tree': (m, opts) => {
      if (m === 'PUT') E_PUTS.push(String((opts && opts.body) || ''));
      return jsonResponse(OR_TREE_JSON);
    } }) });
  await until(() => E.window.SR_MODE === 'connected', 5000, 'connected (or tree)');
  const edoc = E.window.document;
  await pend(400);
  await new Promise(res => { const s = edoc.createElement('script');
    s.textContent = 'window.__RT = TREE.map(n => ({ idx: n._idx, branches: (n.branches||[])' +
      '.map(b => ({ k: b.k, rt: SRW_chipsToCond(b.cond), api: b._apiCond, chips: b.cond })) }));';
    edoc.body.appendChild(s); res(true); });
  const rt = E.window.__RT;
  const orNode = rt.find(n => n.idx === 4);
  const rtOf = i => orNode.branches[i];
  t('apiTreeToLocal 留存原始 cond（_apiCond 就位）',
    rt.every(n => n.branches.every(b => !!b.api)), 'api 字段缺失');
  t('单 or 子句结构全等：{or:[{domain is law},{fact_check is needs_check}]}（此前回写成 A∧B）',
    deepEqual(rtOf(0).rt, rtOf(0).api) &&
    deepEqual(rtOf(0).rt, OR_TREE_JSON.nodes[0].branches[0].cond),
    JSON.stringify(rtOf(0).rt));
  t('atom 与 or 混排结构全等：[atom, {or:…}]（引擎 AND 语义不被摊平成 or）',
    deepEqual(rtOf(1).rt, rtOf(1).api),
    JSON.stringify(rtOf(1).rt));
  t('连续两个 or 子句结构全等：[{or:…},{or:…}]',
    deepEqual(rtOf(2).rt, rtOf(2).api),
    JSON.stringify(rtOf(2).rt));
  t('把握度 + or 混排结构全等（conf_min 回到原原子身上）',
    deepEqual(rtOf(3).rt, rtOf(3).api),
    JSON.stringify(rtOf(3).rt));
  t('兜底空条件往返仍为 []', deepEqual(rt.find(n => n.idx === 9).branches[0].rt, []));
  t('子句边界以「而且」chip 显式占位（树页可见 AND 结构）',
    /而且/.test(edoc.getElementById('treeWrap').textContent));
  E.window.saveTreeRemote();
  await until(() => E_PUTS.length > 0, 5000, 'PUT /admin/tree (or tree)');
  await pend(300);
  const savedOr = JSON.parse(E_PUTS[0]);
  t('保存放行：PUT /admin/tree 载荷里 or 分支 cond 结构全等',
    deepEqual(savedOr.nodes[0].branches[0].cond, OR_TREE_JSON.nodes[0].branches[0].cond) &&
    deepEqual(savedOr.nodes[0].branches[3].cond, OR_TREE_JSON.nodes[0].branches[3].cond),
    E_PUTS[0].slice(0, 240));
  t('可编辑树保存不被拦截（体检面板保持为空）',
    edoc.getElementById('checkup').innerHTML === '',
    edoc.getElementById('checkup').textContent.slice(0, 80));

  group('缺陷修复 · 不可编辑条件（not 子句）保存前拒绝，绝不静默改语义');
  t('前置：A 树确实含 not 子句分支（c211）',
    (() => { const n2 = treeNow.find(n => n._idx === 2);
      return n2.branches.some(b => (b.kids || []).some(kb => kb.k === 'c211')); })());
  w.saveTreeRemote();
  await pend(400);
  t('含 not 子句 → 整树保存被拒（不发 PUT /admin/tree）',
    !A.calls.some(c => c[0] === '/admin/tree' && c[1].method === 'PUT'));
  t('拒绝提示进体检面板（逐字提示语）',
    /该分支含控制台暂不可编辑的条件，请勿修改此分支/.test(doc.getElementById('checkup').textContent),
    doc.getElementById('checkup').textContent.slice(0, 120));
  t('提示定位到分支（node 2 · branch 211 · code cond_readonly）',
    /node 2/.test(doc.getElementById('checkup').textContent) &&
    /branch 211/.test(doc.getElementById('checkup').textContent) &&
    /cond_readonly/.test(doc.getElementById('checkup').textContent));

  group('缺陷修复 · Minor 守卫（SRW_TM / providers 形状）');
  t('doTag 已跑过：不再产生隐式全局 SRW_TM（已删除）', w.SRW_TM === undefined, String(w.SRW_TM));
  const F = makeDom({ routes: Object.assign({}, CONNECT_ROUTES, {
    '/admin/providers': () => jsonResponse({ detail: 'not an array' }) }) });
  await until(() => F.window.SR_MODE === 'demo', 5000, 'demo (providers 非数组)');
  t('providers 响应非数组 → 直接落演示模态', F.window.SR_MODE === 'demo', F.window.SR_MODE);
  t('落演示时 TREE 未被覆写（仍是内置数据，无 API 文案）',
    /先拦危险请求/.test(F.window.document.getElementById('treeWrap').textContent) &&
    !/（来自 API）/.test(F.window.document.getElementById('treeWrap').textContent));
  t('顶栏出现「演示模式」标注', /演示模式/.test(F.window.document.querySelector('.strip').textContent));

  /* ════ D. I5（终审 Important）：不可信流量 → DOM 的存储型 XSS ════
     链路：x-sr-tenant 原值（未归一）→ tags.value → GET /admin/traffic 回吐 →
     接线层 apiTagsToLocal/SRW_valueWord → 基线 renderTags/gaugeCard/renderSigline
     拼 innerHTML。约束：基线（前 ~3532 行）零改动，转义只在接线层边界做。 */
  group('I5 · 不可信流量 → DOM 转义（tenant 含 <img onerror>）');
  const XSS = '<img src=x onerror=alert(1)>';
  /* 期望的转义形态 = 接线层 SRW_esc 的输出。判据用 innerHTML：jsdom 的
     textContent 会把 &lt; 解码回 <，不能用于区分「文本」与「标签」；配合 img
     元素计数断言，二者共同证明载荷被消解为文本而非元素。 */
  const XSS_ESCAPED = XSS.replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const xTags = TRY_JSON.tags.map(t =>
    t.tag === 'tenant' ? Object.assign({}, t, { value: XSS }) : t);
  const xBrief = TRAFFIC_JSON.rows.map(r => Object.assign({}, r, {
    tags_brief: [{ tag: 'tenant', value: XSS }] }));
  const G = makeDom({ routes: Object.assign({}, CONNECT_ROUTES, {
    '/admin/tree/try': () => jsonResponse(Object.assign({}, TRY_JSON, { tags: xTags })),
    '/admin/traffic': () => jsonResponse({ total: 1, rows: xBrief }),
    '/admin/traffic/r_01J8K9ZZZ': () => jsonResponse(
      Object.assign({}, TRAFFIC_DETAIL_JSON, { tags: xTags })),
  }) });
  await until(() => G.window.SR_MODE === 'connected', 5000, 'connected (xss tenant)');
  const gdoc = G.window.document;
  G.window.setSess('new');
  G.window.doTag();
  await until(() => gdoc.querySelectorAll('#tags .gauge').length === 17, 5000, '仪表墙');
  await pend(400);
  t('SRW_esc 就位（接线层导出转义函数）', typeof G.window.SRW_esc === 'function');
  t('仪表墙不产生 tenant 注入的 <img> 元素', gdoc.querySelectorAll('#tags img').length === 0,
    '实际 ' + gdoc.querySelectorAll('#tags img').length);
  t('信号条不产生 tenant 注入的 <img> 元素', gdoc.querySelectorAll('#sigline img').length === 0,
    '实际 ' + gdoc.querySelectorAll('#sigline img').length);
  t('注入载荷以文本原样呈现（无 HTML 标签进入 DOM）',
    /&lt;img src=x onerror=alert\(1\)&gt;/.test(gdoc.getElementById('tags').innerHTML) &&
    !/<img/i.test(gdoc.getElementById('tags').innerHTML));
  t('仪表墙 innerHTML 里是转义后的实体（未被双重转义）',
    gdoc.getElementById('tags').innerHTML.indexOf(XSS_ESCAPED) >= 0 &&
    gdoc.getElementById('tags').innerHTML.indexOf('&amp;lt;') < 0);
  G.window.showReq(0);
  await until(() => gdoc.querySelector('#p-traffic tbody tr'), 5000, '流量行');
  await pend(400);
  t('流量表 tags_brief 芯片同样转义（不产生 <img>）',
    gdoc.querySelectorAll('#p-traffic img').length === 0,
    '实际 ' + gdoc.querySelectorAll('#p-traffic img').length);
  t('流量表 chip innerHTML 里是转义后的实体（未被双重转义）',
    gdoc.querySelector('#p-traffic tbody').innerHTML.indexOf(XSS_ESCAPED) >= 0 &&
    gdoc.querySelector('#p-traffic tbody').innerHTML.indexOf('&amp;lt;') < 0);
  await until(() => gdoc.querySelector('#dt-tags .gauge'), 5000, '详情标签墙');
  await pend(300);
  t('详情标签墙不产生注入元素（apiTagsToLocal 同一路径）',
    gdoc.querySelectorAll('#dt-tags img, #dt-title img, #dt-id img').length === 0);
  t('整个文档无注入的 <img>（含标题/表格/详情）', gdoc.querySelectorAll('img').length === 0,
    '实际 ' + gdoc.querySelectorAll('img').length);

  /* ════ E. backlog 修：renderModelForm 属性注入 + 设置开关失败回滚 ════ */
  group('backlog 修 · 模型表单属性转义 + 设置开关失败回滚');
  const INJ = "x')} <img src=y onerror=alert(1)>";
  const H = makeDom({ routes: Object.assign({}, CONNECT_ROUTES, {
    '/admin/providers': () => jsonResponse([
      { name: INJ, proto: 'openai', base_url: 'http://x/v1', key_env: '',
        extra: {}, ok: true, break_reason: null }, PROVIDERS_JSON[0]]),
    '/admin/settings': (m) => m === 'PUT' ? jsonResponse({}, 500)
      : jsonResponse(SETTINGS_JSON) } ) });
  await until(() => H.window.SR_MODE === 'connected', 5000, 'connected (inject)');
  const hdoc = H.window.document;
  t('SRW_jsAttr 就位（接线层属性转义）', typeof H.window.SRW_jsAttr === 'function');
  H.window.openAddModel();
  await pend(200);
  const mbox = hdoc.getElementById('addModelBox');
  t('接入表单不产生 provider 名注入的 <img>', mbox.querySelectorAll('img').length === 0,
    '实际 ' + mbox.querySelectorAll('img').length);
  let threw = null;
  try { const pseg = mbox.querySelectorAll('.seg')[1];
        pseg && pseg.querySelector('button') && pseg.querySelector('button').click(); }
  catch (e) { threw = e; }
  t('点击含引号的供应商按钮不抛异常（转义后是合法 JS 字面量）', !threw,
    threw && threw.message);
  await pend(150);
  const onBtn = hdoc.getElementById('addModelBox')
    .querySelectorAll('.seg')[1].querySelector('button.on');
  t('选中的供应商名原样回显（转义往返无损）', onBtn && onBtn.textContent === INJ,
    onBtn && onBtn.textContent);
  H.window.closeAddModel();

  const swBoxes = hdoc.querySelectorAll('#p-settings .sw input');
  t('设置页开关已接线', swBoxes.length >= 2, '实际 ' + swBoxes.length);
  const first = swBoxes[0];
  const before = first.checked;                 // session_sticky='1' → true
  first.checked = !before;
  first.dispatchEvent(new H.window.Event('change', { bubbles: true }));
  await until(() => H.calls.some(c => c[0] === '/admin/settings' && c[1].method === 'PUT'),
    5000, 'PUT /admin/settings');
  await pend(300);
  t('设置保存失败 → 勾选态回滚到原值', first.checked === before,
    first.checked + ' vs ' + before);

  /* ════ F. M2：历史行 tree_version 失效标记 ════ */
  group('M2 · 历史行 tree_version 失效标记');
  const staleRow = Object.assign({}, TRAFFIC_JSON.rows[0], { tree_version: 7 });
  const J = makeDom({ routes: Object.assign({}, CONNECT_ROUTES, {
    '/admin/traffic': () => jsonResponse({ total: 1, tree_version: 9, rows: [staleRow] }) }) });
  await until(() => J.window.SR_MODE === 'connected', 5000, 'connected (stale)');
  await until(() => J.window.document.querySelector('#p-traffic tbody tr'), 5000, 'traffic row');
  await pend(300);
  const jtr = J.window.document.querySelectorAll('#p-traffic tbody tr')[0];
  t('记录版本 ≠ 当前版本 → 命中分支列出现失效标记',
    jtr.innerHTML.indexOf('⚠') >= 0 && jtr.innerHTML.indexOf('分支归属可能已失效') >= 0,
    jtr.innerHTML.slice(0, 220));
  const K = makeDom({ routes: CONNECT_ROUTES });
  await until(() => K.window.SR_MODE === 'connected', 5000, 'connected (ok)');
  await until(() => K.window.document.querySelector('#p-traffic tbody tr'), 5000, 'traffic row');
  await pend(300);
  const ktr = K.window.document.querySelectorAll('#p-traffic tbody tr')[0];
  t('版本一致 → 无失效标记', ktr.innerHTML.indexOf('⚠') < 0, ktr.innerHTML.slice(0, 220));

  console.log('\n══ 结果 ══');
  console.log('通过 ' + pass + ' · 失败 ' + fail);
  if (fail) { console.log('\n失败项：'); bad.forEach(b => console.log('  · ' + b)); process.exit(1); }
  console.log('全部通过 ✅');
  process.exit(0);
})().catch(e => { console.error('异常：', e); process.exit(1); });
