/* Test chat 附件协议、请求体积与流式增量的纯函数契约测试（计划 Task 2–3）。
 *
 * 运行：node --test scripts/console_chat_test.mjs
 * 约束：纯 ES module，不碰 DOM / fetch / 文件系统；同一模块在浏览器里复用。
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  MAX_REQUEST_BYTES,
  appendDelta,
  attachmentPart,
  normalizeContent,
  requestBytes,
} from '../router/console/chat/protocol.js';
import { createSseParser } from '../router/console/chat/stream.js';

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
    attachmentPart({ name: 'a.pdf', type: 'application/pdf', dataUrl: 'data:application/pdf;base64,AA==' }),
    { type: 'file', file: { filename: 'a.pdf', file_data: 'data:application/pdf;base64,AA==' } });
});

test('requestBytes counts UTF-8 and the complete JSON envelope', () => {
  assert.equal(requestBytes({ text: '你好' }), Buffer.byteLength(JSON.stringify({ text: '你好' })));
  assert.equal(MAX_REQUEST_BYTES, 10 * 1024 * 1024);
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

test('appendDelta merges adjacent text and retains media parts', () => {
  let parts = appendDelta([], { content: 'hel' });
  parts = appendDelta(parts, { content: 'lo' });
  parts = appendDelta(parts, { content: [
    { type: 'image_url', image_url: { url: 'https://example.test/a.png' } },
  ] });
  assert.equal(parts[0].text, 'hello');
  assert.equal(parts[1].type, 'image');
});

test('SSE parser handles split chunks, CRLF, multiline data and DONE', () => {
  const events = [];
  const parser = createSseParser(value => events.push(value));
  parser.push('data: {"choices":[{"delta":{"content":"你"}}]}\r');
  parser.push('\n\r\ndata: {"choices":');
  parser.push('[{"delta":{"content":"好"}}]}\n\n');
  parser.push('data: [DONE]\n\n');
  parser.end();
  assert.deepEqual(events.map(event => event.choices[0].delta.content), ['你', '好']);
});
