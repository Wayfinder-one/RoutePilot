/* Chat 附件协议、请求体积守卫与增量归并 · 纯函数模块
   ------------------------------------------------------------------
   浏览器与 Node 共用的无副作用工具：不读 DOM、不发请求、不读文件。
   attachmentPart 把内存里的附件描述映射成 OpenAI 兼容 content part；
   normalizeContent 把响应/历史里各种形态的 content 归一成渲染层统一结构；
   appendDelta 把流式 delta 归并进已归一化的 part 数组（相邻文本合并）；
   requestBytes 用完整 JSON 信封的 UTF-8 字节数做前端体积上限判断。
   ------------------------------------------------------------------ */

export const MAX_REQUEST_BYTES = 10 * 1024 * 1024;

const TEXT_EXTENSION = /\.(?:txt|md|markdown|csv|json|jsonl|py|js|mjs|ts|tsx|html|css|xml|ya?ml|log)$/i;

export function requestBytes(body) {
  return new TextEncoder().encode(JSON.stringify(body)).byteLength;
}

export function attachmentPart({ name, type = '', dataUrl = '', text = '' }) {
  const filename = String(name || 'attachment');
  const mime = String(type || '').toLowerCase();
  if (mime.startsWith('image/')) {
    return { type: 'image_url', image_url: { url: dataUrl } };
  }
  if (mime.startsWith('text/') || TEXT_EXTENSION.test(filename)) {
    return { type: 'text', text: `\n\n[File: ${filename}]\n${text}` };
  }
  if (mime === 'audio/mpeg' || mime === 'audio/wav' || mime === 'audio/x-wav') {
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
      return {
        type: 'file',
        url: file.file_data || file.url || '',
        name: file.filename || file.name || 'attachment',
      };
    }
    return { type: 'unknown', value: part };
  });
}

export function appendDelta(parts, delta) {
  const next = parts.map(part => ({ ...part }));
  const incoming = normalizeContent(delta && delta.content);
  incoming.forEach(part => {
    const last = next[next.length - 1];
    if (part.type === 'text' && last && last.type === 'text') last.text += part.text;
    else next.push(part);
  });
  return next;
}
