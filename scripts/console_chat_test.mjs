/* Test chat 协议与流式解码的纯函数契约测试（计划 Task 2–3，修订轮）。
 *
 * 运行：node --test scripts/console_chat_test.mjs
 * 约束：纯 ES module，不碰 DOM / fetch / 文件系统；被测模块在浏览器里复用。
 * 分区：Task 2 = protocol.js（附件分类 / 体积 / 归一化 / raw 增量 / 无损历史）；
 *       Task 3 = stream.js（SSE 解码与终态跟踪），见下方独立分区。
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  MAX_REQUEST_BYTES,
  appendMessageDelta,
  attachmentPart,
  chatHeaders,
  estimateAttachmentBytes,
  historyMessages,
  isTextFile,
  normalizeContent,
  normalizeMessage,
  requestBytes,
} from '../router/console/chat/protocol.js';
import { createSseParser } from '../router/console/chat/stream.js';

/* ---- Task 2 · protocol.js ---- */

test('text detection and attachment serialization use the same file classification', () => {
  for (const name of ['a.yaml', 'a.html', 'a.jsonl', 'a.markdown']) {
    const file = { name, type: '', size: 4 };
    assert.equal(isTextFile(file), true, name);
    assert.equal(attachmentPart({ ...file, text: 'body' }).text.endsWith('body'), true);
  }
  assert.equal(isTextFile({ name: 'a.png', type: 'image/png' }), false);
  assert.ok(estimateAttachmentBytes({ name: 'a.png', type: 'image/png', size: 3 }) >= 4);
});

test('attachmentPart maps images, text, audio, and generic files', () => {
  assert.deepEqual(
    attachmentPart({ name: 'a.png', type: 'image/png', dataUrl: 'data:image/png;base64,AA==' }),
    { type: 'image_url', image_url: { url: 'data:image/png;base64,AA==' } });
  assert.deepEqual(
    attachmentPart({ name: 'a.md', type: 'text/markdown', text: '# Title' }),
    { type: 'text', text: '\n\n[File: a.md]\n# Title' });
  assert.deepEqual(
    attachmentPart({ name: 'a.mp3', type: 'audio/mpeg', dataUrl: 'data:audio/mpeg;base64,AA==' }),
    { type: 'input_audio', input_audio: { data: 'AA==', format: 'mp3' } });
  assert.deepEqual(
    attachmentPart({ name: 'a.wav', type: 'audio/wav', dataUrl: 'data:audio/wav;base64,AA==' }),
    { type: 'input_audio', input_audio: { data: 'AA==', format: 'wav' } });
  assert.deepEqual(
    attachmentPart({ name: 'a.pdf', type: 'application/pdf', dataUrl: 'data:application/pdf;base64,AA==' }),
    { type: 'file', file: { filename: 'a.pdf', file_data: 'data:application/pdf;base64,AA==' } });
  /* 空 MIME 时退回扩展名分类：a.log 是文本附件。 */
  assert.deepEqual(
    attachmentPart({ name: 'a.log', type: '', text: 'LOG' }),
    { type: 'text', text: '\n\n[File: a.log]\nLOG' });
});

test('estimateAttachmentBytes inflates binary bodies and adds envelope overhead', () => {
  /* 文本附件按原始字节数计；二进制按 base64 膨胀 4·ceil(size/3)。 */
  assert.equal(estimateAttachmentBytes({ name: 'a.yaml', type: '', size: 300 }),
    300 + Buffer.byteLength('a.yaml') + 256);
  assert.equal(estimateAttachmentBytes({ name: 'a.png', type: 'image/png', size: 300 }),
    400 + Buffer.byteLength('a.png') + 256);
  /* 文件名按 UTF-8 计字节；任何附件都预留信封开销，预检宁可保守。 */
  assert.equal(estimateAttachmentBytes({ name: '附件.bin', type: 'application/octet-stream', size: 3 }),
    4 + Buffer.byteLength('附件.bin') + 256);
  assert.ok(estimateAttachmentBytes({ name: 'a', type: '', size: 0 }) >= 256);
});

test('requestBytes measures the complete UTF-8 envelope', () => {
  const body = { messages: [{ role: 'user', content: '你好' }] };
  assert.equal(requestBytes(body), Buffer.byteLength(JSON.stringify(body)));
  assert.equal(MAX_REQUEST_BYTES, 10 * 1024 * 1024);
});

/* ---- Task 9 · 管理端测试对话的可信部门 Header ---- */

test('admin chat sends a percent-encoded department when provided', () => {
  const headers = chatHeaders({ sessionId: 's1', department: '财务部' });
  assert.equal(headers['x-sr-session-id'], 's1');
  assert.equal(headers['x-sr-department'], encodeURIComponent('财务部'));
  /* 部门只作为可信 Header 传输，绝不进入 OpenAI 请求体（body 契约由
     requestAssistant 保持原样：model/messages/stream 三字段）。 */
  assert.deepEqual(Object.keys(headers).sort(), ['x-sr-department', 'x-sr-session-id']);
});

test('admin chat omits the department header when empty or absent', () => {
  for (const department of [undefined, null, '']) {
    const headers = chatHeaders({ sessionId: 's2', department: department });
    assert.equal(headers['x-sr-session-id'], 's2');
    assert.equal('x-sr-department' in headers, false,
      `department ${JSON.stringify(department)} must not add a header`);
  }
});

test('normalizeContent keeps text and known/unknown multimodal parts', () => {
  assert.deepEqual(normalizeContent('hello'), [{ type: 'text', text: 'hello' }]);
  const parts = normalizeContent([
    { type: 'text', text: 'look' },
    { type: 'image_url', image_url: { url: 'https://example.test/a.png' } },
    { type: 'custom', value: 1 },
  ]);
  assert.equal(parts[0].type, 'text');
  assert.deepEqual(parts[1], { type: 'image', url: 'https://example.test/a.png' });
  assert.equal(parts[2].type, 'unknown');
});

test('normalizeMessage reads content and message-level images/audio', () => {
  const parts = normalizeMessage({
    content: null,
    images: [{ type: 'image_url', image_url: { url: 'data:image/png;base64,AA==' } }],
    audio: { data: 'AA==', format: 'wav' },
  });
  assert.equal(parts[0].type, 'image');
  assert.equal(parts[1].type, 'audio');
  assert.equal(parts[1].url, 'data:audio/wav;base64,AA==');
});

test('normalizeMessage appends message-level media after content parts', () => {
  const parts = normalizeMessage({
    content: [{ type: 'text', text: 'answer' }],
    images: [{ type: 'image_url', image_url: { url: 'data:image/png;base64,BB==' } }],
    audio: { data: 'AA==', format: 'wav' },
  });
  assert.deepEqual(parts, [
    { type: 'text', text: 'answer' },
    { type: 'image', url: 'data:image/png;base64,BB==' },
    { type: 'audio', url: 'data:audio/wav;base64,AA==' },
  ]);
  /* 非对象输入安全退化成空 parts。 */
  assert.deepEqual(normalizeMessage(null), []);
});

test('raw deltas preserve content while normalized parts expose media', () => {
  let raw = { role: 'assistant', content: '' };
  raw = appendMessageDelta(raw, { content: 'hel' });
  raw = appendMessageDelta(raw, { content: 'lo' });
  raw = appendMessageDelta(raw, { audio: { data: 'AA==', format: 'wav' } });
  assert.equal(raw.content, 'hello');
  assert.equal(raw.audio.data, 'AA==');
  assert.deepEqual(historyMessages([{ role: 'assistant', raw }]), [
    { role: 'assistant', content: 'hello' },
  ]);
  assert.equal(normalizeMessage(raw).at(-1).type, 'audio');
});

test('appendMessageDelta concatenates message-level audio data and transcripts', () => {
  let raw = { role: 'assistant', content: '' };
  raw = appendMessageDelta(raw, { audio: { data: 'AA==', format: 'wav', transcript: '你' } });
  raw = appendMessageDelta(raw, { audio: { data: 'BB==', transcript: '好' } });
  assert.equal(raw.audio.data, 'AA==BB==');
  assert.equal(raw.audio.transcript, '你好');
  /* 后续 delta 不带 format 时保留先前的元数据。 */
  assert.equal(raw.audio.format, 'wav');
});

test('appendMessageDelta concatenates message-level images without dropping earlier ones', () => {
  const first = { type: 'image_url', image_url: { url: 'data:image/png;base64,AA==' } };
  const second = { type: 'image_url', image_url: { url: 'data:image/png;base64,BB==' } };
  let raw = { role: 'assistant', content: '' };
  raw = appendMessageDelta(raw, { images: [first] });
  raw = appendMessageDelta(raw, { images: [second] });
  assert.deepEqual(raw.images, [first, second]);
});

test('appendMessageDelta merges indexed content parts and strips the index marker', () => {
  let raw = { role: 'assistant', content: '' };
  raw = appendMessageDelta(raw, { content: [{ type: 'text', text: 'a', index: 0 }] });
  raw = appendMessageDelta(raw, { content: [{ type: 'text', text: 'b', index: 0 }] });
  raw = appendMessageDelta(raw, { content: [{ type: 'text', text: '!', index: 1 }] });
  assert.deepEqual(raw.content, [
    { type: 'text', text: 'ab' },
    { type: 'text', text: '!' },
  ]);
  /* 无 index 的内容段按序追加，不覆盖已有 parts。 */
  raw = appendMessageDelta(raw, { content: [{ type: 'text', text: '?' }] });
  assert.equal(raw.content.length, 3);
  assert.deepEqual(raw.content.at(-1), { type: 'text', text: '?' });
});

test('appendMessageDelta leaves its raw input and delta untouched', () => {
  const raw = {
    role: 'assistant',
    content: 'a',
    images: [{ type: 'image_url', image_url: { url: 'data:image/png;base64,AA==' } }],
    audio: { data: 'AA==', format: 'wav', transcript: '嗨' },
  };
  const rawSnapshot = JSON.parse(JSON.stringify(raw));
  const delta = {
    content: 'b',
    images: [{ type: 'image_url', image_url: { url: 'data:image/png;base64,BB==' } }],
    audio: { data: 'BB==' },
  };
  const deltaSnapshot = JSON.parse(JSON.stringify(delta));
  const next = appendMessageDelta(raw, delta);
  assert.deepEqual(raw, rawSnapshot, 'appendMessageDelta must not mutate its input raw');
  assert.deepEqual(delta, deltaSnapshot, 'appendMessageDelta must not mutate its input delta');
  assert.equal(next.content, 'ab');
  assert.equal(next.audio.data, 'AA==BB==');
  assert.equal(next.audio.transcript, '嗨');
  assert.deepEqual(next.images, [
    { type: 'image_url', image_url: { url: 'data:image/png;base64,AA==' } },
    { type: 'image_url', image_url: { url: 'data:image/png;base64,BB==' } },
  ]);
  assert.notEqual(next, raw, 'a new raw object is returned for each delta');
});

test('appendMessageDelta copies array content parts instead of mutating them', () => {
  /* R-T23 评审加固：数组 content 走 appendContent 的防御性拷贝路径
   * （current.map(part => ({...part}))），不能原地合并。 */
  let raw = { role: 'assistant', content: '' };
  raw = appendMessageDelta(raw, { content: [{ type: 'text', text: 'a', index: 0 }] });
  raw = appendMessageDelta(raw, {
    content: [
      { type: 'image_url', image_url: { url: 'data:image/png;base64,AA==' } },
      { type: 'text', text: 'b', index: 0 },
    ],
  });
  const rawSnapshot = JSON.parse(JSON.stringify(raw));
  const delta = { content: [{ type: 'text', text: 'c', index: 0 }] };
  const deltaSnapshot = JSON.parse(JSON.stringify(delta));
  const next = appendMessageDelta(raw, delta);
  assert.deepEqual(raw, rawSnapshot, 'array content must not be merged in place');
  assert.deepEqual(delta, deltaSnapshot, 'the incoming indexed parts must not be mutated');
  assert.notEqual(next, raw, 'a new raw object is returned for each delta');
  assert.notEqual(next.content, raw.content, 'content parts are copied, not shared');
  assert.deepEqual(next.content, [
    { type: 'text', text: 'abc' },
    { type: 'image_url', image_url: { url: 'data:image/png;base64,AA==' } },
  ]);
});

test('historyMessages maps assistant raw content and other roles verbatim', () => {
  const content = [
    { type: 'text', text: 'answer' },
    { type: 'weird', x: 1 },
  ];
  assert.deepEqual(historyMessages([
    { role: 'user', content: 'question' },
    { role: 'assistant', raw: { role: 'assistant', content } },
  ]), [
    { role: 'user', content: 'question' },
    /* assistant 只回放原始 content（含未知 part，不做渲染往返）；
     * 消息级 images/audio 有意不出现在历史里。 */
    { role: 'assistant', content },
  ]);
});

/* ---- Task 3 · stream.js：SSE 解码与终态 ---- */

test('SSE handles split chunks and DONE', () => {
  const events = [];
  const parser = createSseParser(event => events.push(event));
  parser.push('data: {"choices":[{"delta":{"content":"你"}}]}\r');
  parser.push('\n\r\ndata: {"choices":[{"delta":{"content":"好"}}]}\n\n');
  parser.push('data: [DONE]\n\n');
  assert.equal(parser.end(), true);
  assert.equal(events.map(event => event.choices[0].delta.content).join(''), '你好');
});

test('finish_reason is terminal even without DONE', () => {
  const parser = createSseParser(() => {});
  parser.push('data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n');
  assert.equal(parser.end(), true);

  /* R-T23 评审加固：终态 chunk 必须照常投递。stream.js 在标记 terminal 后仍要
   * 调用 onPayload；上游常把 finish_reason 与最后一个 delta 同帧发送，回归若在
   * 设置 terminal 后提前 return，会静默丢掉这一帧的增量内容。 */
  const events = [];
  const delivering = createSseParser(event => events.push(event));
  delivering.push('data: {"choices":[{"delta":{"content":"尾"},"finish_reason":"stop"}]}\n\n');
  assert.equal(events.length, 1, 'the terminal chunk must still be delivered exactly once');
  assert.equal(events[0].choices[0].delta.content, '尾');
  assert.equal(delivering.end(), true);
});

test('EOF without DONE or finish_reason is incomplete', () => {
  const parser = createSseParser(() => {});
  parser.push('data: {"choices":[{"delta":{"content":"partial"}}]}\n\n');
  assert.equal(parser.end(), false);
});

test('provider error payload throws', () => {
  const parser = createSseParser(() => {});
  assert.throws(
    () => parser.push('data: {"error":{"message":"upstream failed"}}\n\n'),
    /upstream failed/,
  );
});

test('SSE reassembles events split mid-JSON across chunks', () => {
  const events = [];
  const parser = createSseParser(event => events.push(event));
  parser.push('data: {"choices":[{"delta":{"content":"你"}}]}\r');
  parser.push('\n\r\ndata: {"choices":');
  parser.push('[{"delta":{"content":"好"}}]}\n\n');
  parser.push('data: [DONE]\n\n');
  assert.equal(parser.end(), true);
  assert.deepEqual(events.map(event => event.choices[0].delta.content), ['你', '好']);
});

test('end() flushes a trailing unterminated event and reports it incomplete', () => {
  const events = [];
  const parser = createSseParser(event => events.push(event));
  parser.push('data: {"choices":[{"delta":{"content":"尾"}}]}');
  assert.equal(parser.end(), false);
  assert.deepEqual(events.map(event => event.choices[0].delta.content), ['尾']);
  /* 冲刷后的二次 end() 不再触发回调，仍报告未完成。 */
  assert.equal(parser.end(), false);
  assert.equal(events.length, 1);
});

test('SSE parser joins multiline data fields with newlines before parsing', () => {
  const events = [];
  const parser = createSseParser(event => events.push(event));
  parser.push('data: {"choices":[{"delta":\ndata: {"content":"联"}}]}\n\n');
  parser.end();
  assert.deepEqual(events, [{ choices: [{ delta: { content: '联' } }] }]);
});

test('empty payloads, comment lines and DONE markers never reach onPayload', () => {
  const events = [];
  const parser = createSseParser(event => events.push(event));
  parser.push('data: \n\n');
  parser.push(': keepalive\n\n');
  parser.push(': data: {"fake":true}\n\n');
  parser.push('data: [DONE]\n\n');
  assert.deepEqual(events, []);
  /* 终态标记之后调用方仍可继续 push（防御性），事件照常解码。 */
  parser.push('data: {"choices":[{"delta":{"content":"ok"}}]}\n\n');
  assert.deepEqual(events.map(event => event.choices[0].delta.content), ['ok']);
  assert.equal(parser.end(), true);
});

test('terminal markers never fire onPayload and end() stays quiet after flushing', () => {
  const events = [];
  const parser = createSseParser(event => events.push(event));
  parser.push('data: {"choices":[{"delta":{"content":"x"}}]}\n\n');
  parser.push('data: [DONE]');
  assert.equal(parser.end(), true);
  assert.deepEqual(events.map(event => event.choices[0].delta.content), ['x']);
  /* 终态契约：end() 冲刷掉 [DONE] 本身不产生 payload，重复 end() 也不再触发任何回调。 */
  assert.equal(parser.end(), true);
  assert.equal(events.length, 1);
});

test('provider errors carry .payload and stringify a missing message', () => {
  const parser = createSseParser(() => {});
  assert.throws(
    () => parser.push('data: {"error":{"message":"upstream failed"}}\n\n'),
    error => {
      assert.equal(error.message, 'upstream failed');
      assert.deepEqual(error.payload, { message: 'upstream failed' });
      return true;
    },
  );
  const fallback = createSseParser(() => {});
  assert.throws(
    () => fallback.push('data: {"error":{"code":503}}\n\n'),
    error => {
      assert.equal(error.message, JSON.stringify({ code: 503 }));
      assert.deepEqual(error.payload, { code: 503 });
      return true;
    },
  );
});
