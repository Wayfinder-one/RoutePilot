/* Chat 附件协议、体积守卫与无损消息归一 · 纯函数模块
   ------------------------------------------------------------------
   浏览器与 Node 共用的无副作用工具：不读 DOM、不发请求、不读文件。
   isTextFile 是附件分类的唯一判定（text/* MIME 或文本扩展名），attachmentPart
   的分类与「读文本还是读 Data URL」共用它；estimateAttachmentBytes 在读取之前
   按 base64 膨胀估算附件体积，供预检提前拒绝超大文件；normalizeMessage 把 raw
   消息（content + 消息级 images/audio）归一成渲染 parts；appendMessageDelta 把
   流式 delta 无损归并进 raw（字符串拼接、消息级 images/audio 累加、index 内容
   段合并），raw 始终是历史回放的唯一事实源；historyMessages 直接取 raw.content
   构造请求历史，不做渲染往返转换；requestBytes 用完整 JSON 信封的 UTF-8 字节
   数做前端体积上限判断。
   ------------------------------------------------------------------ */

export const MAX_REQUEST_BYTES = 10 * 1024 * 1024;

/* 管理端测试对话的请求头：会话 id 恒在；部门是可信管理端 Header，只有非空时
   才发送，并按百分号编码（服务端 decode_department 严格解码）。部门绝不进入
   OpenAI 请求体 —— 选图是控制台的 Header 能力，不是模型输入。 */
export function chatHeaders({ sessionId, department }) {
  const headers = { 'x-sr-session-id': sessionId };
  if (department) headers['x-sr-department'] = encodeURIComponent(department);
  return headers;
}

const TEXT_EXTENSION = /\.(?:txt|md|markdown|csv|json|jsonl|py|js|mjs|ts|tsx|html|css|xml|ya?ml|log)$/i;

export function requestBytes(body) {
  return new TextEncoder().encode(JSON.stringify(body)).byteLength;
}

export function isTextFile(file) {
  return String(file.type || '').toLowerCase().startsWith('text/') || TEXT_EXTENSION.test(file.name || '');
}

export function estimateAttachmentBytes(file) {
  const nameBytes = new TextEncoder().encode(file.name || 'attachment').byteLength;
  const bodyBytes = isTextFile(file) ? file.size : 4 * Math.ceil(file.size / 3);
  return bodyBytes + nameBytes + 256;
}

export function attachmentPart({ name, type = '', dataUrl = '', text = '' }) {
  const file = { name, type };
  const filename = String(name || 'attachment');
  const mime = String(type || '').toLowerCase();
  if (mime.startsWith('image/')) return { type: 'image_url', image_url: { url: dataUrl } };
  if (isTextFile(file)) return { type: 'text', text: `\n\n[File: ${filename}]\n${text}` };
  if (['audio/mpeg', 'audio/wav', 'audio/x-wav'].includes(mime)) {
    return {
      type: 'input_audio',
      input_audio: {
        data: dataUrl.slice(dataUrl.indexOf(',') + 1),
        format: mime === 'audio/mpeg' ? 'mp3' : 'wav',
      },
    };
  }
  return { type: 'file', file: { filename, file_data: dataUrl } };
}

export function normalizeContent(content) {
  if (typeof content === 'string') return content ? [{ type: 'text', text: content }] : [];
  if (!Array.isArray(content)) return [];
  return content.map(part => {
    if (!part || typeof part !== 'object') return { type: 'unknown', value: part };
    if (part.type === 'text' || part.type === 'output_text') {
      return { type: 'text', text: String(part.text || '') };
    }
    if (part.type === 'image_url' || part.type === 'image') {
      const source = part.image_url || part.image || part;
      return { type: 'image', url: typeof source === 'string' ? source : source.url || '' };
    }
    if (part.type === 'video_url' || part.type === 'video') {
      const source = part.video_url || part.video || part;
      return { type: 'video', url: typeof source === 'string' ? source : source.url || '' };
    }
    if (part.type === 'audio' || part.type === 'input_audio') {
      const source = part.audio || part.input_audio || part;
      const url = source.url || (source.data ? `data:audio/${source.format || 'mpeg'};base64,${source.data}` : '');
      return { type: 'audio', url };
    }
    if (part.type === 'file') {
      const file = part.file || part;
      return { type: 'file', url: file.file_data || file.url || '', name: file.filename || file.name || 'attachment' };
    }
    return { type: 'unknown', value: part };
  });
}

export function normalizeMessage(message) {
  const raw = message && typeof message === 'object' ? message : {};
  const parts = normalizeContent(raw.content);
  if (Array.isArray(raw.images)) parts.push(...normalizeContent(raw.images));
  if (raw.audio) parts.push(...normalizeContent([{ type: 'audio', audio: raw.audio }]));
  return parts;
}

function appendContent(current, incoming) {
  if (incoming == null) return current ?? '';
  if (typeof incoming === 'string') {
    if (Array.isArray(current)) return current.concat({ type: 'text', text: incoming });
    return String(current || '') + incoming;
  }
  if (!Array.isArray(incoming)) return current ?? '';
  const next = Array.isArray(current)
    ? current.map(part => ({ ...part }))
    : current ? [{ type: 'text', text: String(current) }] : [];
  incoming.forEach(part => {
    if (!part || typeof part !== 'object') { next.push(part); return; }
    const clean = { ...part };
    const index = Number.isInteger(clean.index) ? clean.index : null;
    delete clean.index;
    if (index === null) { next.push(clean); return; }
    const previous = next[index];
    const textDelta = clean.type === 'text' || (!clean.type && typeof clean.text === 'string');
    if (previous && previous.type === 'text' && textDelta) {
      next[index] = { ...previous, ...clean, type: 'text', text: previous.text + clean.text };
    } else next[index] = clean;
  });
  return next;
}

export function appendMessageDelta(raw, delta) {
  const next = {
    ...raw,
    role: 'assistant',
    content: appendContent(raw.content, delta.content),
  };
  if (Array.isArray(delta.images)) next.images = (raw.images || []).concat(delta.images);
  if (delta.audio) {
    next.audio = {
      ...(raw.audio || {}),
      ...delta.audio,
      data: String((raw.audio && raw.audio.data) || '') + String(delta.audio.data || ''),
      transcript: String((raw.audio && raw.audio.transcript) || '') + String(delta.audio.transcript || ''),
    };
  }
  return next;
}

export function historyMessages(messages) {
  return messages.map(message => ({
    role: message.role,
    content: message.role === 'assistant' ? message.raw.content : message.content,
  }));
}
