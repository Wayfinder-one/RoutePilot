/* RoutePilot · 模型测试对话页（计划 Task 5–8，修订版）
   ------------------------------------------------------------------
   Task 5（壳）：Auto 状态行（页面只走智能路由，不提供模型选择）、
   内联会话错误（#testError）、角色门控（renderAccessState）、新建对话、
   语言重绘。
   Task 6（附件）：选择 / 拖拽 / 粘贴同一管线；预检在读取之前按
   estimateAttachmentBytes 拒绝预估超限；文本文件只读 file.text()，
   非文本走 Data URL；单文件失败就地内联「文件名: 错误」。
   Task 7（流式）：guarded 流式生命周期 —— 本地 controller + 会话代次
   守卫（New Chat 换代后旧请求无痕退场），raw 无损历史回放，SSE 增量
   appendMessageDelta + parser.end() 终态布尔，停止/未完成/错误三态区分。
   Task 8（动作）：Copy 只复制归一化 text part，整链失败按 render.js
   契约写进按钮标签；多模态消息级 images/audio 只渲染不进历史；
   New Chat 清场并回收预览 blob（已发送内容用 Data URL，互不相干）。
   消息形状（修订版）：assistant 携带 raw（历史回放的唯一事实源）与
   parts 渲染缓存，字段名是 status（v1 叫 state）；user 消息的 parts 由
   normalizeMessage({content}) 一次性派生。parts 绝不序列化。
   页面不写任何全局：state 模块私有，事件监听在模块体内绑定。
   ------------------------------------------------------------------ */
import { api, apiErrText, esc, toast } from './api.js';
import { onLangChange, t } from './i18n.js';
import {
  MAX_REQUEST_BYTES, appendMessageDelta, attachmentPart, chatHeaders, estimateAttachmentBytes,
  historyMessages, isTextFile, normalizeMessage, requestBytes,
} from './chat/protocol.js';
import { createSseParser } from './chat/stream.js';
import { renderParts } from './chat/render.js';
import './nav.js';

/* crypto.randomUUID / crypto.getRandomValues 只在安全上下文可用；明文 HTTP
   部署（http://<host>:8400/console/）也必须能跑。未知环境退回时间戳 + 随机数，
   与 router/console/routing/controller.js 的 id 兜底同一策略（评审决议 2：
   保留 v1 的守卫版 newId，禁止裸 crypto.randomUUID()）。 */
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
  messages: [], attachments: [],
  controller: null, conversationId: 0, sessionId: newId(), role: null,
  activeTree: '', bootError: '', uploadEpoch: 0,
  /* 测试对话的可信部门 Header（Task 9）：只影响 /admin/test/chat/completions 的
     选图，绝不进入 OpenAI 请求体；每次发送前从 #testDepartment 读取并 trim。 */
  department: '',
};
/* 发送 / 停止 / 复制 / 重试 / 重新生成与提示文案键，统一从这里取。 */
const COPY = Object.freeze({
  send: 'test.send', stop: 'test.stop', copy: 'test.copy', retry: 'test.retry',
  regenerate: 'test.regenerate', uploadFail: 'test.uploadFail',
  tooLarge: 'test.tooLarge', adminOnly: 'test.adminOnly', incomplete: 'test.incomplete',
  stopped: 'test.stopped',
});
const $ = selector => document.querySelector(selector);
const CHAT_ENDPOINT = '/admin/test/chat/completions';
/* 输入区锁定/清空的唯一控件清单：textarea、可信部门 Header 输入、附件与文件入口。 */
const COMPOSER_CONTROLS = ['#testInput', '#testDepartment', '#testAttach', '#testFileInput'];
/* 页面只演示线上智能路由：请求模型恒为 auto，不给用户旁路决策图的机会。 */
const CHAT_MODEL = 'auto';

/* ── Auto 状态行：附带默认图名（公开 /v1 使用的那张） ── */
function renderStatus() {
  const status = $('#testStatus');
  status.replaceChildren();
  if (state.bootError) { status.textContent = state.bootError; return; }
  if (state.role !== null && state.role !== 'admin') { status.textContent = t(COPY.adminOnly); return; }
  status.textContent = t('test.autoStatus') + (state.activeTree ? ` · ${state.activeTree}` : '');
}

/* ── 角色门控（计划 Task 5 独立成函数）：非管理员锁死整个输入区 ──
   * 未知角色按只读处理（fail closed）；
   * 启动失败同样锁死输入区/附件；
   * updateSendState 只负责在途/内容驱动的发送按钮状态。 */
function renderAccessState() {
  const admin = state.role === 'admin';
  const locked = Boolean(state.bootError) || !admin;
  COMPOSER_CONTROLS.forEach(selector => {
    const element = $(selector);
    if (element) element.disabled = locked;
  });
  /* 角色门控同时清掉可信部门：viewer / 启动失败时既不能发送也不能留下会误导
     下一次发送的部门状态。busy 锁只禁用、不清除（发送期间值属于在途请求）。 */
  if (locked) {
    const department = $('#testDepartment');
    if (department) department.value = '';
    state.department = '';
  }
  const input = $('#testInput');
  if (state.bootError) input.placeholder = t('test.placeholder');
  else if (state.role !== null && !admin) input.placeholder = t(COPY.adminOnly);
}

/* ── 输入区：自动增高（≤180px）与发送/停止按钮状态 ── */
function autoResize() {
  const input = $('#testInput');
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight || 0, 180)}px`;
}

/* 单段文本直接给字符串，否则给 part 数组；Task 7 的请求体复用同一构造。 */
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

/* 发送按钮 + 生成中的输入区锁定（R-T5 评审回补 a：busy 期间 textarea/
   附件/文件入口全部禁用，v1 行为）。解锁路径把角色门控交还 renderAccessState
   重算，避免 busy=false 时误放开 viewer。 */
function updateSendState() {
  const busy = Boolean(state.controller);
  if (busy) {
    COMPOSER_CONTROLS.forEach(selector => {
      const element = $(selector);
      if (element) element.disabled = true;
    });
  } else {
    renderAccessState();
  }
  const send = $('#testSend');
  send.textContent = t(busy ? COPY.stop : COPY.send);
  send.disabled = Boolean(state.bootError) || state.role !== 'admin' ||
    (!busy && !hasComposerContent());
}

/* ── 附件：只留在内存；预检先于任何读取 ────────────────────────────
   体积三段式（计划 Task 6）：历史 JSON 实测 + 已选附件估算 + 本文件估算
   超过 10MB 就地内联拒绝，绝不碰 FileReader。文本文件只读 file.text()，
   不再额外转 Data URL（协议分类的唯一判定是 isTextFile）；非文本走
   readDataUrl。单文件读取失败只内联「文件名: 错误」，其余文件照常处理。 */
function readDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(reader.error || new Error('file_read_failed'));
    reader.readAsDataURL(file);
  });
}

function selectedAttachmentEstimate() {
  return state.attachments.reduce((sum, item) => sum + item.estimatedBytes, 0);
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
  /* 读取是异步的：New Chat / 发送清空附件后，旧的读取结果必须整批丢弃。 */
  const epoch = state.uploadEpoch;
  for (const file of Array.from(files || [])) {
    const estimatedBytes = estimateAttachmentBytes(file);
    const historyBytes = requestBytes({
      model: CHAT_MODEL,
      messages: historyMessages(state.messages),
      stream: true,
    });
    /* 预检先于读取：预估超限就地报错并跳过该文件，不做任何 FileReader 转换。 */
    if (historyBytes + selectedAttachmentEstimate() + estimatedBytes > MAX_REQUEST_BYTES) {
      showConversationError(t(COPY.tooLarge));
      continue;
    }
    try {
      const text = isTextFile(file) ? await file.text() : '';
      const dataUrl = isTextFile(file) ? '' : await readDataUrl(file);
      if (epoch !== state.uploadEpoch) return;
      state.attachments.push({
        id: newId(),
        file,
        part: attachmentPart({ name: file.name, type: file.type, dataUrl, text }),
        previewUrl: String(file.type || '').startsWith('image/') ? URL.createObjectURL(file) : '',
        estimatedBytes,
      });
    } catch (error) {
      if (epoch !== state.uploadEpoch) return;
      /* 单文件失败：内联该文件的名字与错误（计划 Task 6）；toast 保留作
         短时提示（i18n 基线要求 test.uploadFail 键保持被引用）。 */
      showConversationError(`${file.name}: ${error.message}`);
      toast(t(COPY.uploadFail), 3200);
    }
  }
  if (epoch !== state.uploadEpoch) return;
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
  /* 令所有在途读取失效：它们的结果属于上一轮输入。 */
  state.uploadEpoch += 1;
  state.attachments.forEach(attachment => {
    if (attachment.previewUrl) URL.revokeObjectURL(attachment.previewUrl);
  });
  state.attachments = [];
  renderAttachments();
}

/* 只有管理员、且没有在途请求/启动错误时才接受新的附件输入。 */
function canAddAttachments() {
  return state.role === 'admin' && !state.controller && !state.bootError;
}

/* ── 消息渲染 ── */
function scrollMessages() {
  const host = $('#testMessages');
  host.scrollTop = host.scrollHeight;
}

/* 内联会话错误（#testError）：体积/附件读取/流错误就地展示在消息区，
   不再只靠 toast（计划 Task 5；Task 6/7 的调用方陆续接入）。 */
function showConversationError(message) {
  const host = $('#testError');
  host.textContent = String(message || '');
  host.hidden = !host.textContent;
  scrollMessages();
}

function hideConversationError() {
  const host = $('#testError');
  host.textContent = '';
  host.hidden = true;
}

/* 消息级 Copy（计划 Task 8）：只复制归一化 text part；与 render.js 的
   copyText 同一契约 —— clipboard 优先、兜底 textarea+execCommand，整链
   失败时把失败写进按钮标签（Copy failed，与渲染器围栏代码的英文一致），
   兜底成功后恢复；全程不产生未捕获页面错误。 */
function copyMessage(message, button) {
  const text = (message.parts || [])
    .filter(part => part.type === 'text')
    .map(part => part.text)
    .join('\n');
  const clipboard = typeof navigator !== 'undefined' ? navigator.clipboard : null;
  const finish = copied => {
    if (button) button.textContent = copied ? t(COPY.copy) : 'Copy failed';
  };
  if (clipboard && typeof clipboard.writeText === 'function') {
    clipboard.writeText(text).then(
      () => finish(true),
      () => copyFallback(text).then(finish),
    );
    return;
  }
  copyFallback(text).then(finish);
}

/* 非安全上下文 / 无剪贴板权限时的兜底：临时 textarea + execCommand；
   永远 resolve（true=已复制，false=整链失败），绝不 reject。 */
function copyFallback(text) {
  return new Promise(resolve => {
    const helper = document.createElement('textarea');
    helper.value = text;
    helper.setAttribute('readonly', '');
    helper.style.position = 'fixed';
    helper.style.top = '-1000px';
    helper.style.opacity = '0';
    document.body.append(helper);
    helper.select();
    try {
      resolve(Boolean(document.execCommand('copy')));
    } catch (error) {
      /* 无权限 / 无 execCommand：整链失败 */
      resolve(false);
    } finally {
      helper.remove();
    }
  });
}

/* assistant 气泡骨架：meta / bubble / actions 三段；只构造不挂载，
   挂载位置由调用方决定（流式插在 #testError 前，重绘进节点数组）。 */
function buildAssistantView(message) {
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
  return view;
}

function appendAssistantView(message) {
  $('#testEmpty').hidden = true;
  /* #testError 永远保持消息区最后一个节点：流式新气泡插到它前面。 */
  $('#testError').before(buildAssistantView(message));
  renderAssistant(message);
}

function renderAssistant(message) {
  if (!message.view) return;
  const model = message.actualModel || CHAT_MODEL;
  message.view.querySelector('.tc-message-meta').textContent = `Auto → ${model}`;
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
  if (message.status === 'complete') {
    const copy = document.createElement('button');
    copy.className = 'btn ghost sm';
    copy.textContent = t(COPY.copy);
    /* 失败标签要写回按钮自身：handler 拿到按钮引用。 */
    copy.addEventListener('click', () => copyMessage(message, copy));
    actions.append(copy);
  }
  if (message.status === 'error') {
    const error = document.createElement('span');
    error.className = 'tc-error';
    error.textContent = message.error;
    actions.append(error);
  }
  if (message.status === 'stopped') {
    const stopped = document.createElement('span');
    stopped.className = 'mut';
    stopped.textContent = t(COPY.stopped);
    actions.append(stopped);
  }
  if (message.status === 'incomplete') {
    const incomplete = document.createElement('span');
    incomplete.className = 'mut';
    incomplete.textContent = t(COPY.incomplete);
    actions.append(incomplete);
  }
  /* 重试/重新生成只作用于最后一条 assistant：只给最后一条渲染按钮，避免
      旧消息上的按钮操作到新消息（或成为无操作）。Copy 对每条完成消息都可用。 */
  if (state.messages[state.messages.length - 1] === message) {
    if (message.status === 'complete') addAction(t(COPY.regenerate), regenerateLast);
    if (message.status === 'error') addAction(t(COPY.retry), retryLast);
  }
  scrollMessages();
}

function renderConversation() {
  const empty = $('#testEmpty');
  const errorElement = $('#testError');
  const nodes = [empty];
  state.messages.forEach(message => {
    if (message.role === 'assistant') { nodes.push(buildAssistantView(message)); return; }
    const view = document.createElement('article');
    view.className = 'tc-message user';
    const body = document.createElement('div');
    body.className = 'tc-bubble';
    /* user 消息在入列时就派生好 parts 渲染缓存，重绘/换语言直接复用。 */
    renderParts(body, message.parts || []);
    view.append(body);
    message.view = view;
    nodes.push(view);
  });
  /* #testError 归位到消息区末尾：它必须永远是最后一个节点（流式气泡、
     会话错误都围绕这个不变量排布）。 */
  nodes.push(errorElement);
  $('#testMessages').replaceChildren(...nodes);
  empty.hidden = state.messages.length > 0;
  state.messages.forEach(message => {
    if (message.role === 'assistant') renderAssistant(message);
  });
  scrollMessages();
}

/* ── 请求编排：guarded 流式生命周期（计划 Task 7，修订版）────────────
   * 本地 controller + generation = state.conversationId：isCurrent() 守卫
     每一个回调与 finally —— New Chat 换代后，旧请求的任何回调/finally 都
     无痕退场，绝不清掉新请求的 controller、绝不重绘旧气泡；
   * 请求体用 historyMessages(state.messages)（raw 无损回放，不做渲染往返）；
   * 精确 requestBytes 检查先于任何状态变更，超限就地内联；
   * SSE 增量 appendMessageDelta 进 raw、parts = normalizeMessage(raw)；
     parser.end() 返回的布尔区分 complete / incomplete（提前 EOF）；
   * R-T23 评审约束：payload 抛错（provider error）时绝不追加 parser.end()
     —— 抛错已中断读循环，end() 会把残留 buffer 再解析一次；
   * 发送开始先清掉上一轮的 #testError（R-T5 评审回补 b）。 */
async function requestAssistant() {
  const body = {
    model: CHAT_MODEL,
    messages: historyMessages(state.messages),
    stream: true,
  };
  if (requestBytes(body) > MAX_REQUEST_BYTES) {
    showConversationError(t(COPY.tooLarge));
    return;
  }
  const generation = state.conversationId;
  const controller = new AbortController();
  const assistant = {
    id: newId(), role: 'assistant',
    raw: { role: 'assistant', content: '' },
    parts: [], status: 'streaming',
    actualModel: '', error: '', view: null,
  };
  /* 发送真正开始：清掉上一轮遗留的内联错误（体积/流错误不跨轮存活）。 */
  hideConversationError();
  state.messages.push(assistant);
  state.controller = controller;
  appendAssistantView(assistant);
  updateSendState();
  const isCurrent = () => state.conversationId === generation && state.controller === controller;
  let reader = null;
  try {
    const response = await api.stream(CHAT_ENDPOINT, body, {
      signal: controller.signal,
      /* 部门是管理端可信 Header：百分号编码后随请求发送，绝不进入 body。 */
      headers: chatHeaders({ sessionId: state.sessionId, department: state.department }),
    });
    if (!isCurrent()) return;
    assistant.actualModel = response.headers.get('x-sr-model') || CHAT_MODEL;
    if ((response.headers.get('content-type') || '').includes('text/event-stream') && response.body) {
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      const parser = createSseParser(payload => {
        if (!isCurrent()) return;
        const delta = payload.choices && payload.choices[0] && payload.choices[0].delta;
        if (!delta) return;
        assistant.raw = appendMessageDelta(assistant.raw, delta);
        assistant.parts = normalizeMessage(assistant.raw);
        renderAssistant(assistant);
      });
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        parser.push(decoder.decode(value, { stream: true }));
      }
      parser.push(decoder.decode());
      const terminal = parser.end();
      if (!isCurrent()) return;
      assistant.status = terminal ? 'complete' : 'incomplete';
    } else {
      const data = await response.json();
      if (!isCurrent()) return;
      const message = data.choices && data.choices[0] && data.choices[0].message;
      assistant.raw = {
        role: 'assistant',
        ...(message || {}),
        content: (message && message.content) ?? null,
      };
      assistant.parts = normalizeMessage(assistant.raw);
      assistant.status = 'complete';
    }
  } catch (error) {
    if (!isCurrent()) return;
    /* 非中止的失败（parser 抛错 / 读流中断）显式取消 reader，及时释放上游
       连接与租约；中止路径的 reader 已由 abort 关闭。 */
    if (reader && error.name !== 'AbortError') {
      try { await reader.cancel(); } catch (cancelError) { /* 连接已断开 */ }
    }
    assistant.status = error.name === 'AbortError' ? 'stopped' : 'error';
    assistant.error = error.name === 'AbortError' ? '' : apiErrText(error);
  } finally {
    /* 只有 generation 与 controller 身份都对得上才收尾：旧代次的 finally
       不得清掉新对话的 controller，也不得重绘已被清场的消息区。 */
    if (state.conversationId === generation && state.controller === controller) {
      state.controller = null;
      renderAssistant(assistant);
      updateSendState();
    }
  }
}

function retryLast() {
  const last = state.messages[state.messages.length - 1];
  if (state.controller || !last || last.role !== 'assistant' || last.status !== 'error') return;
  state.messages.pop();
  renderConversation();
  requestAssistant();
}

function regenerateLast() {
  const last = state.messages[state.messages.length - 1];
  if (state.controller || !last || last.role !== 'assistant' || last.status !== 'complete') return;
  state.messages.pop();
  renderConversation();
  requestAssistant();
}

async function sendCurrent() {
  /* 同时最多一个在途请求；viewer/未知角色 fail closed。 */
  if (state.controller || state.role !== 'admin') return;
  /* 发送前读取可信部门（Task 9）：它只影响服务端按部门选图，与对话内容无关。 */
  state.department = ($('#testDepartment').value || '').trim();
  const input = $('#testInput');
  const text = input.value;
  if (!text.trim() && !state.attachments.length) return;
  const content = buildUserContent(text, state.attachments);
  const user = {
    id: newId(), role: 'user', content, parts: normalizeMessage({ content }), view: null,
  };
  const candidateBody = {
    model: CHAT_MODEL,
    messages: historyMessages(state.messages.concat(user)),
    stream: true,
  };
  /* 体积上限先于任何状态变更检查；超限就地内联，不再只靠 toast。 */
  if (requestBytes(candidateBody) > MAX_REQUEST_BYTES) {
    showConversationError(t(COPY.tooLarge));
    return;
  }
  state.messages.push(user);
  clearAttachments();
  input.value = '';
  autoResize();
  renderConversation();
  await requestAssistant();
}

/* ── 消息区回到空态：移除已渲染气泡与内联错误，只保留 #testEmpty 节点 ── */
function clearMessages() {
  hideConversationError();
  $('#testMessages').replaceChildren($('#testEmpty'), $('#testError'));
  $('#testEmpty').hidden = false;
  state.messages = [];
}

/* 新建对话：先递增会话代次（Task 7 的 stale-request 守卫用它判定「结果属于
   哪一轮对话」），摘下再中止在途控制器，清空附件与消息、换会话 id、恢复空态。 */
function newChat() {
  state.conversationId += 1;
  const controller = state.controller;
  state.controller = null;
  if (controller) controller.abort();
  clearAttachments();
  clearMessages();
  state.sessionId = newId();
  state.department = '';
  $('#testInput').value = '';
  const department = $('#testDepartment');
  if (department) department.value = '';
  $('#testFileInput').value = '';
  autoResize();
  updateSendState();
  $('#testInput').focus();
}

/* ── 事件绑定 ── */
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
    if (!canAddAttachments()) return;
    addFiles(event.clipboardData.files);
  }
});
$('#testSend').addEventListener('click', () => {
  if (state.controller) { state.controller.abort(); return; }
  sendCurrent();
});
$('#testAttach').addEventListener('click', () => {
  if (canAddAttachments()) $('#testFileInput').click();
});
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
  if (canAddAttachments()) panel.classList.add('is-dragover');
});
panel.addEventListener('dragleave', event => {
  if (!panel.contains(event.relatedTarget)) panel.classList.remove('is-dragover');
});
panel.addEventListener('drop', event => {
  event.preventDefault();
  panel.classList.remove('is-dragover');
  if (!canAddAttachments()) return;
  const files = event.dataTransfer ? Array.from(event.dataTransfer.files) : [];
  if (files.length) addFiles(files);
});

async function boot() {
  const [me, trees] = await Promise.all([api.me(), api.get('/admin/trees')]);
  /* 未知角色 fail closed：只有明确的 admin 才能发测试请求。 */
  state.role = me.role || 'viewer';
  /* 状态行展示默认图（公开 /v1 使用的那张），而不是列表里第一张启用的图：
     多图同时启用时顺序不代表默认关系（Task 8）。 */
  const preferred = (trees.trees || []).find(tree => tree.default);
  state.activeTree = preferred ? (preferred.name || preferred.id) : '';
  renderAccessState();
  renderStatus();
  updateSendState();
  autoResize();
}

onLangChange(() => {
  renderStatus();
  renderAccessState();
  renderAttachments();
  /* 文案是就地替换的：消息区的 meta/动作按钮也要按新语言重建。 */
  renderConversation();
  updateSendState();
});
/* 首屏先按「空输入 + 未知角色」落一次状态，避免闪出可点的输入区。 */
renderAccessState();
updateSendState();
autoResize();
boot().catch(error => {
  /* 启动失败：原始错误留在状态行，同时锁死输入区/附件，页面不装活。 */
  state.bootError = apiErrText(error);
  state.role = null;
  renderStatus();
  renderAccessState();
  updateSendState();
});
