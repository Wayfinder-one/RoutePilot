/* RoutePilot · 模型测试对话页（计划 Task 5）
   ------------------------------------------------------------------
   本文件负责「壳」：页面状态、模型目录选择器、Auto / 固定模型状态文案、
   新建对话与样例按钮。真正的发送、附件与流式渲染按计划落在 Task 6–7（仍写
   本文件），因此这里只准备状态结构与待用文案键，不保留 inert 的假处理器。
   页面不写任何全局：state 模块私有，事件监听在模块体内绑定。
   ------------------------------------------------------------------ */
import { api, apiErrText, esc } from './api.js';
import { onLangChange, t } from './i18n.js';
import './nav.js';

const state = {
  model: 'auto', models: [], messages: [], attachments: [],
  controller: null, sessionId: crypto.randomUUID(), role: null,
  activeTree: '', bootError: '',
};
/* Task 6–7 的发送 / 停止 / 附件 / 流式处理器共用这批文案键：在壳体阶段就与
   zh、en 词典一起落地，后面只加处理器，不再重复动 i18n。 */
const COPY = Object.freeze({
  send: 'test.send', stop: 'test.stop', copy: 'test.copy', retry: 'test.retry',
  regenerate: 'test.regenerate', direct: 'test.direct', uploadFail: 'test.uploadFail',
  tooLarge: 'test.tooLarge', adminOnly: 'test.adminOnly', incomplete: 'test.incomplete',
  stopped: 'test.stopped',
});
const $ = selector => document.querySelector(selector);

/* ── 模型目录：Auto + 按提供方分组的已配置模型 ── */
function renderModelOptions() {
  const groups = new Map();
  state.models.forEach(model => {
    const provider = model.provider || '';
    if (!groups.has(provider)) groups.set(provider, []);
    groups.get(provider).push(model);
  });
  $('#testModel').innerHTML = `<option value="auto">${esc(t('test.auto'))}</option>` +
    Array.from(groups, ([provider, models]) => `<optgroup label="${esc(provider)}">${models.map(model =>
      `<option value="${esc(model.id)}">${esc(model.id)} · ${esc(model.upstream)}</option>`).join('')}</optgroup>`).join('');
  $('#testModel').value = state.model;
}

/* ── Auto / 固定模型状态：Auto 附上当前启用的决策组名 ── */
function renderStatus() {
  const status = $('#testStatus');
  if (state.bootError) { status.textContent = state.bootError; return; }
  if (state.role && state.role !== 'admin') { status.textContent = t(COPY.adminOnly); return; }
  status.textContent = state.model === 'auto'
    ? t('test.autoStatus') + (state.activeTree ? ` · ${state.activeTree}` : '')
    : t('test.directStatus');
}

/* 生成中按钮显示「停止」（Task 7 切换 state.controller 时复用）。 */
function renderSendLabel() {
  $('#testSend').textContent = t(state.controller ? COPY.stop : COPY.send);
}

/* 只有管理员能发测试请求：只读账号禁用输入区并说明原因。 */
function applyRole() {
  const admin = state.role === 'admin';
  ['#testInput', '#testAttach', '#testSend', '#testFileInput'].forEach(selector => {
    const element = $(selector);
    if (element) element.disabled = !admin;
  });
  if (!admin) $('#testInput').placeholder = t(COPY.adminOnly);
}

function clearAttachments() {
  state.attachments.forEach(attachment => {
    if (attachment.previewUrl) URL.revokeObjectURL(attachment.previewUrl);
  });
  state.attachments = [];
  $('#testFiles').replaceChildren();
}

/* 消息区回到空态：移除已渲染气泡，只保留 #testEmpty 节点。 */
function clearMessages() {
  $('#testMessages').replaceChildren($('#testEmpty'));
  $('#testEmpty').hidden = false;
  state.messages = [];
}

/* 新建对话：中止在途请求、清空附件与消息、换一个会话 UUID、恢复空态。 */
function newChat() {
  if (state.controller) state.controller.abort();
  state.controller = null;
  clearAttachments();
  clearMessages();
  state.sessionId = crypto.randomUUID();
  $('#testInput').value = '';
  $('#testFileInput').value = '';
  renderSendLabel();
  $('#testInput').focus();
}

$('#testModel').addEventListener('change', event => {
  state.model = event.target.value;
  renderStatus();
});
$('#testNew').addEventListener('click', newChat);
document.querySelectorAll('.tc-sample').forEach(button => {
  button.addEventListener('click', () => {
    const input = $('#testInput');
    if (input.disabled) return;
    input.value = button.textContent;
    input.focus();
  });
});

async function boot() {
  const [models, me, trees] = await Promise.all([
    api.get('/admin/models'), api.me(), api.get('/admin/trees'),
  ]);
  state.models = models.models || [];
  state.role = me.role || 'admin';
  const active = (trees.trees || []).find(tree => tree.enabled);
  state.activeTree = active ? (active.name || active.id) : '';
  renderModelOptions();
  applyRole();
  renderStatus();
  renderSendLabel();
}

onLangChange(() => {
  renderModelOptions();
  applyRole();
  renderStatus();
  renderSendLabel();
});
boot().catch(error => {
  state.bootError = apiErrText(error);
  renderStatus();
});
