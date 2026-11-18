/* RTTryRun · Policy v4 试算面板（Task 9 · 设计 §8）
   ------------------------------------------------------------------
   唯一数据源：POST /admin/try 对「当前草稿」的一次真实试算（不落盘、不
   发布、不调用上游）。结果区只读展示后端返回的 target/models/
   matched_rule/fallback/reason，随后复用 ../trace.js 的 JEV / 各问题答案 /
   逐条规则视图——前端绝不依据 answers 重算命中规则，所有结论以
   result.trace 与 result.matched_rule 为唯一来源（§8）。

   失败语义：保留已有草稿、不清空上一次输入、保留上一次成功轨迹，只把
   错误记为提示行 + toast。请求发出前记录序号与草稿身份，回包时两者任一
   过期即整份丢弃——慢的旧响应绝不能覆盖新草稿（Task 8 的 trySeq 约定）。

   ES module：app.js 装配时调用 initTryRun({store, toast, updateToolbar})；
   app.tryResult 只读暴露 currentTryResult()。
   ------------------------------------------------------------------ */
import { api, apiErrText, esc } from '../api.js';
import { t, tf } from '../i18n.js';
import { clone } from './policy.js';
import { build, jevHtml, questionsHtml, rulesHtml } from '../trace.js';

let deps = null;        /* {store, toast, updateToolbar} */
let current = null;     /* 最近一次成功试算 {text, result}（驱动结果视图） */
let lastError = null;   /* 最近一次失败 {text, error}（驱动错误提示行） */
let runSeq = 0;         /* 单调递增的运行序号：旧响应过期判定 */
let bound = false;

const byId = id => document.getElementById(id);
const storeNow = () => (deps && deps.store ? deps.store : null);
const toastNow = (message, tone) => {
  if (deps && typeof deps.toast === 'function') deps.toast(message, tone);
};
const updateToolbarNow = () => {
  if (deps && typeof deps.updateToolbar === 'function') deps.updateToolbar();
};

/* ── 运行：一次 /admin/try，只发草稿，不二次求值 ─────────────────── */
async function runTry(input, fixedNow) {
  const store = storeNow();
  if (input === undefined || input === null) return false;
  if (!store || !store.policy || store.tryRunning || store.saving) return false;
  const text = String(input == null ? '' : input).trim();
  if (!text) {
    toastNow(t('pv.tryEmpty'), 'warn');
    return false;
  }
  const targetDraft = store.draft;
  const seq = ++runSeq;
  store.setTryRunning(true);
  updateToolbarNow();
  try {
    const result = await api.post('/admin/try', {
      policy: clone(store.policy),
      text,
      now: fixedNow || undefined
    }, { timeoutMs: 130000 });
    /* 过期响应：期间发生了新的装载/运行，或草稿已被替换。 */
    if (seq !== runSeq || store.draft !== targetDraft) return false;
    current = { text, result };
    lastError = null;
    renderTryResult();
    return true;
  } catch (error) {
    if (seq !== runSeq || store.draft !== targetDraft) return false;
    /* 失败不清结果：current（上一次成功轨迹）原样保留。 */
    lastError = { text, error: apiErrText(error) };
    renderTryResult();
    toastNow(tf('pv.tryFailed', { err: lastError.error }), 'error');
    return false;
  } finally {
    if (seq === runSeq) {
      store.setTryRunning(false);
      updateToolbarNow();
    }
  }
}

function runFromInput() {
  const input = byId('cvTryInput');
  return runTry(input ? input.value : '');
}

/* ── 结果区：结论头（后端字段）+ trace.js 的 JEV/答案/规则视图 ──── */
function reasonText(result) {
  if (result.matched_rule != null) return t('pv.tryMatched') + ' ' + result.matched_rule;
  if (result.reason === 'jev_error' || result.reason === 'no_rule_matched') {
    return t('pv.tryReason.' + result.reason);
  }
  return result.reason == null ? '' : String(result.reason);
}

function resultHtml(result) {
  const model = build(Array.isArray(result.trace) ? result.trace : []);
  return '<div class="pv-try-head">' +
    '<span class="pill ' + (result.fallback ? 'warn' : 'ok') + '">' +
    esc(t(result.fallback ? 'pv.tryFallback' : 'pv.tryTarget')) + ' <b class="mono">' +
    esc(result.target == null ? '—' : result.target) + '</b></span>' +
    '<span class="pv-try-models mono">' + esc((result.models || []).join(' · ') || '—') + '</span>' +
    '<span class="pv-try-rule-name">' + esc(reasonText(result)) + '</span>' +
    '</div>' +
    jevHtml(model) + questionsHtml(model) + rulesHtml(model);
}

function renderTryResult() {
  const box = byId('cvTryResult');
  if (!box) return;
  const parts = [];
  if (lastError) {
    parts.push('<p class="pv-try-error">' +
      esc(tf('pv.tryFailed', { err: lastError.error })) + '</p>');
  }
  if (current) parts.push(resultHtml(current.result));
  const html = parts.join('');
  box.hidden = !html;
  box.innerHTML = html;
}

/* 换策略/新草稿：序号 +1 让在途响应过期，结果区整体清空。
   在途请求的 finally 只在 seq === runSeq 时释放锁；序号已前进就意味着
   那个释放路径永远不会触发——锁必须在这里一并放下，否则 DraftStore
   永远拒绝编辑/保存/试算（旧版只能靠刷新页面脱困，未保存编辑随之丢失）。 */
function resetTryRun() {
  runSeq += 1;
  current = null;
  lastError = null;
  const store = storeNow();
  if (store) store.setTryRunning(false);
  updateToolbarNow();
  renderTryResult();
}

function currentTryResult() {
  return current;
}

/* ── 绑定：输入 Enter 与运行按钮（装配层只负责调用 initTryRun） ──── */
function initTryRun(next) {
  deps = next || null;
  if (bound) return;
  bound = true;
  const input = byId('cvTryInput');
  if (input) {
    input.addEventListener('keydown', event => {
      if (event.key !== 'Enter' || event.isComposing) return;
      event.preventDefault();
      runFromInput();
    });
  }
  const button = byId('cvTryRun');
  if (button) button.addEventListener('click', runFromInput);
}

export { initTryRun, runTry, runFromInput, renderTryResult, resetTryRun, currentTryResult };
