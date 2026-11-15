/* Test chat 附件协议、请求体积、wire 序列化与流式增量的纯函数契约测试
 * （计划 Task 2–3 + Task 7 的 partToWire/partsToWire 与 SSE onDone）。
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
  partToWire,
  partsToWire,
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

/* ---- Hardening round（评审补强：只补测试，不改实现） ---- */

test('attachmentPart maps wav audio and text-extension fallback', () => {
  assert.deepEqual(
    attachmentPart({ name: 'a.wav', type: 'audio/wav', dataUrl: 'data:audio/wav;base64,AA==' }),
    { type: 'input_audio', input_audio: { data: 'AA==', format: 'wav' } });
  assert.deepEqual(
    attachmentPart({ name: 'a.log', type: '', text: 'LOG' }),
    { type: 'text', text: '\n\n[File: a.log]\nLOG' });
});

test('appendDelta leaves its input untouched and keeps text after media separate', () => {
  const input = [
    { type: 'text', text: 'a' },
    { type: 'image', url: 'https://example.test/a.png' },
  ];
  const snapshot = JSON.parse(JSON.stringify(input));
  const merged = appendDelta(input, { content: 'b' });
  assert.deepEqual(input, snapshot, 'appendDelta must not mutate its input array/parts');
  assert.equal(merged.length, 3);
  assert.deepEqual(merged[0], { type: 'text', text: 'a' });
  assert.deepEqual(merged[1], { type: 'image', url: 'https://example.test/a.png' });
  assert.deepEqual(merged[2], { type: 'text', text: 'b' },
    'text after a non-text part must be pushed as a new part');
});

test('SSE parser flushes a trailing unterminated event on end()', () => {
  const events = [];
  const parser = createSseParser(value => events.push(value));
  parser.push('data: {"choices":[{"delta":{"content":"尾"}}]}');
  parser.end();
  assert.deepEqual(events.map(event => event.choices[0].delta.content), ['尾']);
});

test('SSE parser joins multiline data fields with newlines before parsing', () => {
  const events = [];
  const parser = createSseParser(value => events.push(value));
  parser.push('data: {"choices":[{"delta":\ndata: {"content":"联"}}]}\n\n');
  parser.end();
  assert.deepEqual(events, [{ choices: [{ delta: { content: '联' } }] }]);
});

test('SSE parser ignores empty payloads, comment lines and DONE markers', () => {
  const events = [];
  const parser = createSseParser(value => events.push(value));
  parser.push('data: \n\n');
  parser.push(': keepalive\n\n');
  parser.push(': data: {"fake":true}\n\n');
  parser.push('data: [DONE]\n\n');
  assert.deepEqual(events, []);
  parser.push('data: {"choices":[{"delta":{"content":"ok"}}]}\n\n');
  parser.end();
  assert.deepEqual(events.map(event => event.choices[0].delta.content), ['ok']);
});

test('partToWire and partsToWire keep text, media and unknown parts round-trippable', () => {
  const parts = [
    { type: 'text', text: 'a' },
    { type: 'image', url: 'data:image/png;base64,AA==' },
    { type: 'unknown', value: { x: 1 } },
  ];
  assert.deepEqual(partsToWire(parts), [
    { type: 'text', text: 'a' },
    { type: 'image_url', image_url: { url: 'data:image/png;base64,AA==' } },
    { x: 1 },
  ]);
  assert.deepEqual(partToWire({ type: 'video', url: 'https://example.test/v.mp4' }),
    { type: 'video_url', video_url: { url: 'https://example.test/v.mp4' } });
  assert.deepEqual(partToWire({ type: 'audio', url: 'data:audio/wav;base64,AA==' }),
    { type: 'audio', audio: { url: 'data:audio/wav;base64,AA==' } });
  assert.deepEqual(
    partToWire({ type: 'file', name: 'a.bin', url: 'data:application/octet-stream;base64,AA==' }),
    { type: 'file', file: { filename: 'a.bin', file_data: 'data:application/octet-stream;base64,AA==' } });
  /* 纯文本数组压成字符串（OpenAI content 的另一种形态）。 */
  assert.equal(partsToWire([{ type: 'text', text: 'a' }, { type: 'text', text: 'b' }]), 'ab');
  /* 线上格式经 normalizeContent 回到渲染层形状：文本/图片/未知片段都等价。 */
  assert.deepEqual(normalizeContent(partsToWire(parts)), parts);
});

test('SSE parser reports DONE through the optional onDone callback', () => {
  const events = [];
  let doneCount = 0;
  const parser = createSseParser(value => events.push(value), () => { doneCount += 1; });
  parser.push('data: {"choices":[{"delta":{"content":"x"}}]}\n\n');
  parser.push('data: [DONE]\n\n');
  parser.end();
  assert.deepEqual(events.map(event => event.choices[0].delta.content), ['x']);
  assert.equal(doneCount, 1);

  /* 没有 onDone 的旧调用方行为不变：[DONE] 既不产生 payload 也不抛错。 */
  const legacy = [];
  const legacyParser = createSseParser(value => legacy.push(value));
  legacyParser.push('data: [DONE]\n\n');
  legacyParser.end();
  assert.deepEqual(legacy, []);

  /* 上游提前 EOF（没有 [DONE]）不触发 onDone，调用方据此判定 incomplete。 */
  let truncatedDone = 0;
  const truncated = createSseParser(() => {}, () => { truncatedDone += 1; });
  truncated.push('data: {"choices":[{"delta":{"content":"y"}}]}\n\n');
  truncated.end();
  assert.equal(truncatedDone, 0);
});
