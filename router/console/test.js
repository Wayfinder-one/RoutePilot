/* RoutePilot · 模型测试对话页（计划 Task 5–6）
   ------------------------------------------------------------------
   Task 5（壳）：页面状态、模型目录选择器、Auto / 固定模型状态文案、新建对话。
   Task 6（本步骤）：输入区与内存附件——选择 / 拖拽 / 粘贴都走同一条 FileReader
   管线，chip 显示预览、名字与体积，移除时回收 object URL；Enter 发送、
   Shift+Enter 换行，空输入且无附件时禁用发送。真正的网络发送与流式渲染由
   Task 7 接管（本步骤的 sendCurrent 只做本地提交，不发起任何请求）。
   页面不写任何全局：state 模块私有，事件监听在模块体内绑定。
   ------------------------------------------------------------------ */
import { api, apiErrText, esc, toast } from './api.js';
import { onLangChange, t } from './i18n.js';
import { attachmentPart } from './chat/protocol.js';
import './nav.js';

const state = {
  model: 'auto', models: [], messages: [], attachments: [],
  controller: null, sessionId: crypto.randomUUID(), role: null,
  activeTree: '', bootError: '',
};
/* 发送 / 停止 / 附件 / 流式处理器共用这批文案键（Task 6–7 逐步消费）。 */
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

/* ── 输入区：自动增高（≤180px）与发送按钮状态 ── */
function autoResize() {
  const input = $('#testInput');
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight || 0, 180)}px`;
}

/* Task 7 的请求体会用同一个构造函数：单段文本直接给字符串，否则给 part 数组。 */
function buildUserContent(text, attachments) {
  const parts = [];
  if (text.trim()) parts.push({ type: 'text', text: text.trim() });
  parts.push(...attachments.map(attachment => attachment.part));
  return parts.length === 1 && parts[0].type === 'text' ? parts[0].text : parts;
}

function hasComposerContent() {
  const content = buildUserContent($('#testInput').value, state.attachments);
  return Array.isArray(content) ? content.length > 0 : Boolean(String(content || '').trim());
}

function updateSendState() {
  const busy = Boolean(state.controller);
  const viewer = state.role !== null && state.role !== 'admin';
  const send = $('#testSend');
  send.textContent = t(busy ? COPY.stop : COPY.send);
  /* 生成中按钮是「停止」：必须可点，因此只禁「没内容时的发送」和只读账号。 */
  send.disabled = viewer || (!busy && !hasComposerContent());
}

/* 只有管理员能发测试请求：只读账号禁用输入区并说明原因。 */
function applyRole() {
  const admin = state.role === 'admin';
  ['#testInput', '#testAttach', '#testFileInput'].forEach(selector => {
    const element = $(selector);
    if (element) element.disabled = !admin;
  });
  if (!admin) $('#testInput').placeholder = t(COPY.adminOnly);
  updateSendState();
}

/* ── 附件：只留在内存。FileReader → data URL，文本类顺带读出正文 ── */
function readDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(reader.error || new Error('file_read_failed'));
    reader.readAsDataURL(file);
  });
}

function isTextLike(file) {
  return String(file.type || '').startsWith('text/') ||
    /\.(?:txt|md|markdown|csv|json|jsonl|py|js|mjs|ts|tsx|html|css|xml|ya?ml|log)$/i.test(file.name);
}

function formatBytes(bytes) {
  const size = Number(bytes) || 0;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function renderAttachments() {
  $('#testFiles').innerHTML = state.attachments.map(attachment => {
    const name = esc(attachment.file.name);
    const preview = attachment.previewUrl
      ? `<img src="${esc(attachment.previewUrl)}" alt="${name}">` : '';
    return `<div class="tc-chip" data-attachment-id="${esc(attachment.id)}">${preview}` +
      `<div class="tc-chip-body"><span class="tc-chip-name" title="${name}">${name}</span>` +
      `<span class="tc-chip-meta">${esc(formatBytes(attachment.file.size))}</span></div>` +
      `<button type="button" class="tc-chip-remove" data-remove-attachment="${esc(attachment.id)}"` +
      ` aria-label="${name}">×</button></div>`;
  }).join('');
}

async function addFiles(files) {
  for (const file of Array.from(files || [])) {
    try {
      const [dataUrl, text] = await Promise.all([
        readDataUrl(file),
        isTextLike(file) ? file.text() : Promise.resolve(''),
      ]);
      state.attachments.push({
        id: crypto.randomUUID(),
        file,
        part: attachmentPart({ name: file.name, type: file.type, dataUrl, text }),
        previewUrl: String(file.type || '').startsWith('image/') ? URL.createObjectURL(file) : '',
      });
    } catch (error) {
      toast(t(COPY.uploadFail), 3200);
    }
  }
  renderAttachments();
  updateSendState();
}

function removeAttachment(id) {
  const index = state.attachments.findIndex(attachment => attachment.id === id);
  if (index < 0) return;
  const [attachment] = state.attachments.splice(index, 1);
  if (attachment.previewUrl) URL.revokeObjectURL(attachment.previewUrl);
  renderAttachments();
  updateSendState();
}

function clearAttachments() {
  state.attachments.forEach(attachment => {
    if (attachment.previewUrl) URL.revokeObjectURL(attachment.previewUrl);
  });
  state.attachments = [];
  renderAttachments();
}

/* 本地回显：Task 7 会用 renderParts 重建真正的消息视图，这里先保证 Enter /
   发送不会丢掉用户刚写的内容（不产生任何网络请求）。 */
function appendLocalUserMessage(content) {
  $('#testEmpty').hidden = true;
  const view = document.createElement('article');
  view.className = 'tc-message user';
  const bubble = document.createElement('div');
  bubble.className = 'tc-bubble';
  if (typeof content === 'string') {
    bubble.textContent = content;
  } else {
    content.forEach(part => {
      if (part.type === 'text') {
        bubble.append(document.createTextNode(part.text));
        return;
      }
      const tag = document.createElement('span');
      tag.className = 'tc-chip';
      tag.textContent = part.type === 'image_url' ? 'image' : part.type;
      bubble.append(tag);
    });
  }
  view.append(bubble);
  $('#testMessages').append(view);
}

/* Task 7 接管真正的发送与流式渲染：本步骤只清空输入区与附件，绝不发请求。 */
function sendCurrent() {
  if (state.controller || state.role !== 'admin') return;
  const content = buildUserContent($('#testInput').value, state.attachments);
  const empty = Array.isArray(content) ? content.length === 0 : !String(content || '').trim();
  if (empty) return;
  state.messages.push({ id: crypto.randomUUID(), role: 'user', content, view: null });
  appendLocalUserMessage(content);
  $('#testInput').value = '';
  clearAttachments();
  autoResize();
  updateSendState();
}

/* ── 消息区回到空态：移除已渲染气泡，只保留 #testEmpty 节点 ── */
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
  autoResize();
  updateSendState();
  $('#testInput').focus();
}

/* ── 事件绑定 ── */
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
    autoResize();
    updateSendState();
    input.focus();
  });
});

$('#testInput').addEventListener('input', () => {
  autoResize();
  updateSendState();
});
$('#testInput').addEventListener('keydown', event => {
  /* 输入法组字中的 Enter 不能发送。 */
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  sendCurrent();
});
$('#testInput').addEventListener('paste', event => {
  if (event.clipboardData && event.clipboardData.files.length) {
    event.preventDefault();
    addFiles(event.clipboardData.files);
  }
});
$('#testSend').addEventListener('click', () => sendCurrent());
$('#testAttach').addEventListener('click', () => $('#testFileInput').click());
$('#testFileInput').addEventListener('change', async event => {
  const files = Array.from(event.target.files || []);
  try {
    await addFiles(files);
  } finally {
    /* 读完就清空，同一个文件才能被再次选择。 */
    event.target.value = '';
  }
});
$('#testFiles').addEventListener('click', event => {
  const button = event.target.closest('[data-remove-attachment]');
  if (button) removeAttachment(button.dataset.removeAttachment);
});

/* 拖拽：整个面板接受文件，悬停时给虚线提示。 */
const panel = $('.tc-panel');
panel.addEventListener('dragover', event => {
  event.preventDefault();
  panel.classList.add('is-dragover');
});
panel.addEventListener('dragleave', event => {
  if (!panel.contains(event.relatedTarget)) panel.classList.remove('is-dragover');
});
panel.addEventListener('drop', event => {
  event.preventDefault();
  panel.classList.remove('is-dragover');
  const files = event.dataTransfer ? Array.from(event.dataTransfer.files) : [];
  if (files.length) addFiles(files);
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
  autoResize();
}

onLangChange(() => {
  renderModelOptions();
  applyRole();
  renderStatus();
});
/* 首屏先按「空输入 + 未知角色」落一次按钮状态，避免闪出可点发送。 */
updateSendState();
autoResize();
boot().catch(error => {
  state.bootError = apiErrText(error);
  renderStatus();
});
