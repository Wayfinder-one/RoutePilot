/* RoutePilot · 模型测试对话页（计划 Task 5–7）
   ------------------------------------------------------------------
   Task 5（壳）：模型目录选择器、Auto / 固定模型状态、新建对话、语言重绘。
   Task 6（附件）：选择 / 拖拽 / 粘贴都走同一条 FileReader 管线；chip 显示
   预览、名字与体积；Enter 发送、Shift+Enter 换行。
   Task 7（流式）：POST /admin/test/chat/completions，SSE 增量经
   createSseParser + appendDelta + renderParts 渲染；支持停止、重试、重新
   生成、复制、多模态答案、实际模型/直连标记，错误就地内联。
   线上唯一的历史格式是 message.content（OpenAI wire format）；message.parts
   只是渲染缓存，绝不序列化。
   页面不写任何全局：state 模块私有，事件监听在模块体内绑定。
   ------------------------------------------------------------------ */
import { api, apiErrText, esc, toast } from './api.js';
import { onLangChange, t } from './i18n.js';
import {
  MAX_REQUEST_BYTES, appendDelta, attachmentPart, normalizeContent, partsToWire, requestBytes,
} from './chat/protocol.js';
import { createSseParser } from './chat/stream.js';
import { renderParts } from './chat/render.js';
import './nav.js';

/* crypto.randomUUID / crypto.getRandomValues 只在安全上下文可用；明文 HTTP
   部署（http://<host>:8400/console/）也必须能跑。未知环境退回时间戳 + 随机数，
   与 router/console/routing/controller.js 的 id 兜底同一策略。 */
function newId() {
  let raw = '';
  try {
    raw = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID() : '';
  } catch (error) { raw = ''; }
  if (!raw) raw = Date.now().toString(16) + Math.random().toString(16).slice(2);
  return raw;
}

const state = {
  model: 'auto', models: [], messages: [], attachments: [],
  controller: null, sessionId: newId(), role: null,
  activeTree: '', bootError: '',
};
/* 发送 / 停止 / 复制 / 重试 / 重新生成与提示文案键，统一从这里取。 */
const COPY = Object.freeze({
  send: 'test.send', stop: 'test.stop', copy: 'test.copy', retry: 'test.retry',
  regenerate: 'test.regenerate', direct: 'test.direct', uploadFail: 'test.uploadFail',
  tooLarge: 'test.tooLarge', adminOnly: 'test.adminOnly', incomplete: 'test.incomplete',
  stopped: 'test.stopped',
});
const $ = selector => document.querySelector(selector);
const CHAT_ENDPOINT = '/admin/test/chat/completions';

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
  if (state.role !== null && state.role !== 'admin') { status.textContent = t(COPY.adminOnly); return; }
  status.textContent = state.model === 'auto'
    ? t('test.autoStatus') + (state.activeTree ? ` · ${state.activeTree}` : '')
    : t('test.directStatus');
}

/* ── 输入区：自动增高（≤180px）与发送/停止按钮状态 ── */
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

/* 组件状态（角色/在途请求/启动失败）与 send 按钮标签都由这里统一计算：
   * 未知角色按只读处理（fail closed）；
   * 生成中禁用输入区与附件，但按钮变成可点的「停止」；
   * 语言切换只重算文案，不会把生成中的输入区重新放开。 */
function updateSendState() {
  const busy = Boolean(state.controller);
  const admin = state.role === 'admin';
  const locked = Boolean(state.bootError) || !admin || busy;
  ['#testInput', '#testAttach', '#testFileInput'].forEach(selector => {
    const element = $(selector);
    if (element) element.disabled = locked;
  });
  const modelSelect = $('#testModel');
  if (modelSelect) modelSelect.disabled = Boolean(state.bootError);
  const input = $('#testInput');
  if (state.bootError) input.placeholder = t('test.placeholder');
  else if (state.role !== null && !admin) input.placeholder = t(COPY.adminOnly);
  const send = $('#testSend');
  send.textContent = t(busy ? COPY.stop : COPY.send);
  send.disabled = Boolean(state.bootError) || !admin || (!busy && !hasComposerContent());
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
        id: newId(),
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

/* ── 消息渲染 ── */
function scrollMessages() {
  const host = $('#testMessages');
  host.scrollTop = host.scrollHeight;
}

function copyMessage(message) {
  const text = (message.parts || [])
    .filter(part => part.type === 'text')
    .map(part => part.text)
    .join('\n');
  const clipboard = typeof navigator !== 'undefined' ? navigator.clipboard : null;
  if (clipboard && typeof clipboard.writeText === 'function') {
    clipboard.writeText(text).catch(() => copyFallback(text));
    return;
  }
  copyFallback(text);
}

/* 非安全上下文 / 无剪贴板权限时的兜底：临时 textarea + execCommand，失败也静默。 */
function copyFallback(text) {
  const helper = document.createElement('textarea');
  helper.value = text;
  helper.setAttribute('readonly', '');
  helper.style.position = 'fixed';
  helper.style.top = '-1000px';
  helper.style.opacity = '0';
  document.body.append(helper);
  helper.select();
  try { document.execCommand('copy'); } catch (error) { /* 无权限：静默 */ }
  helper.remove();
}

function appendAssistantView(message) {
  $('#testEmpty').hidden = true;
  const view = document.createElement('article');
  view.className = 'tc-message assistant';
  view.dataset.messageId = message.id;
  const meta = document.createElement('div');
  meta.className = 'tc-message-meta';
  const body = document.createElement('div');
  body.className = 'tc-bubble';
  const actions = document.createElement('div');
  actions.className = 'tc-message-actions';
  view.append(meta, body, actions);
  message.view = view;
  $('#testMessages').append(view);
  renderAssistant(message);
}

function renderAssistant(message) {
  if (!message.view) return;
  const model = message.actualModel || message.requestedModel;
  message.view.querySelector('.tc-message-meta').textContent =
    message.requestedModel === 'auto' ? `Auto → ${model}` : `${model} · ${t(COPY.direct)}`;
  renderParts(message.view.querySelector('.tc-bubble'), message.parts || []);
  const actions = message.view.querySelector('.tc-message-actions');
  actions.replaceChildren();
  const addAction = (label, handler) => {
    const button = document.createElement('button');
    button.className = 'btn ghost sm';
    button.textContent = label;
    button.addEventListener('click', handler);
    actions.append(button);
  };
  if (message.state === 'complete') {
    addAction(t(COPY.copy), () => copyMessage(message));
    addAction(t(COPY.regenerate), regenerateLast);
  }
  if (message.state === 'error') {
    const error = document.createElement('span');
    error.className = 'tc-error';
    error.textContent = message.error;
    actions.append(error);
    addAction(t(COPY.retry), retryLast);
  }
  if (message.state === 'stopped') {
    const stopped = document.createElement('span');
    stopped.className = 'mut';
    stopped.textContent = t(COPY.stopped);
    actions.append(stopped);
  }
  scrollMessages();
}

function renderConversation() {
  const empty = $('#testEmpty');
  $('#testMessages').replaceChildren(empty);
  state.messages.forEach(message => {
    if (message.role === 'assistant') {
      appendAssistantView(message);
      return;
    }
    const view = document.createElement('article');
    view.className = 'tc-message user';
    const body = document.createElement('div');
    body.className = 'tc-bubble';
    renderParts(body, normalizeContent(message.content));
    view.append(body);
    message.view = view;
    $('#testMessages').append(view);
  });
  empty.hidden = state.messages.length > 0;
  scrollMessages();
}

/* ── 请求编排：wire-format 历史 → /admin/test/chat/completions → SSE ── */
function requestBody() {
  return {
    model: state.model,
    messages: state.messages.map(message => ({ role: message.role, content: message.content })),
    stream: true,
  };
}

async function requestAssistant() {
  const body = requestBody();
  if (requestBytes(body) > MAX_REQUEST_BYTES) {
    toast(t(COPY.tooLarge), 4000);
    return;
  }
  const assistant = {
    id: newId(), role: 'assistant', content: '', parts: [],
    state: 'streaming', requestedModel: state.model,
    actualModel: '', selection: '', error: '', view: null,
  };
  state.messages.push(assistant);
  appendAssistantView(assistant);
  const controller = new AbortController();
  state.controller = controller;
  updateSendState();
  try {
    const response = await api.stream(CHAT_ENDPOINT, body, {
      signal: controller.signal,
      headers: { 'x-sr-session-id': state.sessionId },
    });
    assistant.actualModel = response.headers.get('x-sr-model') || state.model;
    assistant.selection = response.headers.get('x-sr-selection') || '';
    const contentType = response.headers.get('content-type') || '';
    if (contentType.includes('text/event-stream') && response.body) {
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      const parser = createSseParser(payload => {
        const choice = payload && Array.isArray(payload.choices) ? payload.choices[0] : null;
        const delta = choice && choice.delta;
        if (!delta) return;
        assistant.parts = appendDelta(assistant.parts, delta);
        renderAssistant(assistant);
      });
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        parser.push(decoder.decode(value, { stream: true }));
      }
      parser.push(decoder.decode());
      parser.end();
      assistant.content = partsToWire(assistant.parts);
    } else {
      const data = await response.json();
      const message = data && data.choices && data.choices[0] && data.choices[0].message;
      assistant.content = message && message.content != null ? message.content : '';
      assistant.parts = normalizeContent(assistant.content);
    }
    assistant.state = 'complete';
  } catch (error) {
    assistant.content = partsToWire(assistant.parts || []);
    const aborted = error && error.name === 'AbortError';
    assistant.state = aborted ? 'stopped' : 'error';
    assistant.error = aborted ? '' : apiErrText(error);
  } finally {
    if (state.controller === controller) state.controller = null;
    if (state.messages.includes(assistant)) renderAssistant(assistant);
    updateSendState();
  }
}

function retryLast() {
  const last = state.messages[state.messages.length - 1];
  if (state.controller || !last || last.role !== 'assistant' || last.state !== 'error') return;
  state.messages.pop();
  renderConversation();
  requestAssistant();
}

function regenerateLast() {
  const last = state.messages[state.messages.length - 1];
  if (state.controller || !last || last.role !== 'assistant' || last.state !== 'complete') return;
  state.messages.pop();
  renderConversation();
  requestAssistant();
}

async function sendCurrent() {
  if (state.controller || state.role !== 'admin') return;
  const input = $('#testInput');
  const text = input.value;
  if (!text.trim() && !state.attachments.length) return;
  const content = buildUserContent(text, state.attachments);
  const candidate = state.messages.concat({ role: 'user', content });
  const candidateBody = {
    model: state.model,
    messages: candidate.map(message => ({ role: message.role, content: message.content })),
    stream: true,
  };
  if (requestBytes(candidateBody) > MAX_REQUEST_BYTES) {
    toast(t(COPY.tooLarge), 4000);
    return;
  }
  state.messages.push({ id: newId(), role: 'user', content, view: null });
  clearAttachments();
  input.value = '';
  autoResize();
  renderConversation();
  await requestAssistant();
}

/* ── 消息区回到空态：移除已渲染气泡，只保留 #testEmpty 节点 ── */
function clearMessages() {
  $('#testMessages').replaceChildren($('#testEmpty'));
  $('#testEmpty').hidden = false;
  state.messages = [];
}

/* 新建对话：中止在途请求、清空附件与消息、换一个会话 id、恢复空态。 */
function newChat() {
  if (state.controller) state.controller.abort();
  state.controller = null;
  clearAttachments();
  clearMessages();
  state.sessionId = newId();
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
$('#testSend').addEventListener('click', () => {
  if (state.controller) { state.controller.abort(); return; }
  sendCurrent();
});
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
  /* 未知角色 fail closed：只有明确的 admin 才能发测试请求。 */
  state.role = me.role || 'viewer';
  const active = (trees.trees || []).find(tree => tree.enabled);
  state.activeTree = active ? (active.name || active.id) : '';
  renderModelOptions();
  renderStatus();
  updateSendState();
  autoResize();
}

onLangChange(() => {
  renderModelOptions();
  renderStatus();
  updateSendState();
});
/* 首屏先按「空输入 + 未知角色」落一次按钮状态，避免闪出可点发送。 */
updateSendState();
autoResize();
boot().catch(error => {
  /* 启动失败：原始错误留在状态行，同时锁死输入区/附件/选择器，页面不装活。 */
  state.bootError = apiErrText(error);
  state.role = null;
  renderStatus();
  updateSendState();
});
