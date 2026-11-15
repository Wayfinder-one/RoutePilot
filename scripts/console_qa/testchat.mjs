/* RoutePilot 控制台 QA · 测试对话页安全渲染用例（计划 Task 4）
   ------------------------------------------------------------------
   本文件只注册用例，不启动服务，也不持有页面状态：真实 router、浏览器、
   mock 上游与 teardown 全部由 ../console_qa.mjs 提供（同一进程内顺序执行）。
   覆盖：文本 Markdown → DOM（标题/段落/列表/表格/链接/行内代码/围栏代码 +
   Copy 按钮）、恶意 HTML 与活动 URL 不执行、图片/音频/视频/文件媒体分派、
   未知 part 折叠 JSON、safeMediaUrl 的 scheme 白名单。
   入口：`QA_ONLY="test chat renderer" node scripts/console_qa/testchat.mjs`；
         全量：`node scripts/console_qa.mjs`（已在该文件 main() 中注册）。
   ------------------------------------------------------------------ */

import assert from 'node:assert/strict';
import {
  expectHttpStatusError,
  mockState,
  page,
  resetExpectedHttpStatusErrors,
  routerBase,
  runIfMain,
  screenshot,
  test,
  waitFor,
} from '../console_qa.mjs';

test('test chat renderer supports markdown and blocks active content', async () => {
  await page.goto(routerBase + '/console/index.html');
  const result = await page.evaluate(async () => {
    const { renderParts } = await import('/console/chat/render.js');
    const host = document.createElement('div');
    renderParts(host, [
      { type: 'text', text: '# Title\n\n**bold** <script>window.pwned=1</script>\n\n```js\nconst x = 1;\n```' },
      { type: 'image', url: 'javascript:window.pwned=2' },
      { type: 'image', url: 'data:image/png;base64,iVBORw0KGgo=' },
      { type: 'unknown', value: { answer: 42 } },
    ]);
    return {
      heading: host.querySelector('h1').textContent,
      code: host.querySelector('pre code').textContent,
      scripts: host.querySelectorAll('script').length,
      unsafeImages: host.querySelectorAll('img[src^="javascript:"]').length,
      safeImages: host.querySelectorAll('img[src^="data:image/"]').length,
      unknown: host.querySelector('details pre').textContent,
      pwned: window.pwned || 0,
    };
  });
  assert.deepEqual(result, {
    heading: 'Title', code: 'const x = 1;', scripts: 0, unsafeImages: 0,
    safeImages: 1, unknown: '{\n  "answer": 42\n}', pwned: 0,
  });

  /* 同一渲染入口还要分派音频/视频/文件：原生播放器带 controls，文件是可下载链接。
     只挂 data: 媒体源，避免 QA 浏览器发出容器外的真实请求（http(s)/blob 由下面的
     safeMediaUrl 断言覆盖）。 */
  const media = await page.evaluate(async () => {
    const { renderParts } = await import('/console/chat/render.js');
    const host = document.createElement('div');
    renderParts(host, [
      { type: 'audio', url: 'data:audio/mpeg;base64,AA==', name: 'a.mp3' },
      { type: 'audio', url: 'javascript:window.pwned=3' },
      { type: 'video', url: 'data:video/mp4;base64,AA==', name: 'v.mp4' },
      { type: 'video', url: 'javascript:window.pwned=4' },
      { type: 'file', url: 'data:application/pdf;base64,AA==', name: 'a.pdf' },
      { type: 'unknown', value: { folded: true } },
    ]);
    const download = host.querySelector('a[download]');
    return {
      audio: host.querySelectorAll('audio[controls]').length,
      video: host.querySelectorAll('video[controls]').length,
      downloads: host.querySelectorAll('a[download]').length,
      downloadText: download.textContent,
      downloadHref: download.getAttribute('href'),
      audioSrc: host.querySelector('audio').getAttribute('src'),
      unknownFolded: !host.querySelector('details').open,
      pwned: window.pwned || 0,
    };
  });
  assert.deepEqual(media, {
    audio: 1, video: 1, downloads: 1, downloadText: 'a.pdf',
    downloadHref: 'data:application/pdf;base64,AA==',
    audioSrc: 'data:audio/mpeg;base64,AA==', unknownFolded: true, pwned: 0,
  });
});

test('test chat renderer renders markdown blocks and enforces safe media urls', async () => {
  await page.goto(routerBase + '/console/index.html');
  const result = await page.evaluate(async () => {
    const { renderParts, safeMediaUrl } = await import('/console/chat/render.js');
    const host = document.createElement('div');
    renderParts(host, [{
      type: 'text',
      text: [
        'A paragraph with `inline` and [safe](https://example.test/x) and [bad](javascript:window.pwned=9).',
        '',
        '- one',
        '- two',
        '',
        '1. first',
        '',
        '| a | b |',
        '| --- | --- |',
        '| 1 | 2 |',
        '',
        '```python',
        'print(1)',
        '```',
      ].join('\n'),
    }]);
    const link = host.querySelector('p a');
    return {
      paragraphs: host.querySelectorAll('p').length,
      inlineCode: host.querySelector('p code').textContent,
      links: host.querySelectorAll('p a').length,
      linkHref: link.href,
      linkTarget: link.target,
      linkRel: link.rel,
      badLinkText: host.querySelector('p').textContent.includes('bad'),
      badLinks: host.querySelectorAll('a[href^="javascript:"]').length,
      unordered: host.querySelectorAll('ul li').length,
      ordered: host.querySelectorAll('ol li').length,
      tableHead: [...host.querySelectorAll('table th')].map(th => th.textContent),
      tableCells: [...host.querySelectorAll('table td')].map(td => td.textContent),
      copyText: host.querySelector('.tc-code button').textContent,
      codeLanguage: host.querySelector('.tc-code code').className,
      codeText: host.querySelector('.tc-code code').textContent,
      pwned: window.pwned || 0,
      urls: [
        safeMediaUrl('https://example.test/a.png', 'image'),
        safeMediaUrl('http://example.test/a.png', 'image'),
        safeMediaUrl('blob:https://example.test/x', 'video'),
        safeMediaUrl('javascript:alert(1)', 'image'),
        safeMediaUrl('data:image/png;base64,AA==', 'image'),
        safeMediaUrl('data:audio/mpeg;base64,AA==', 'image'),
        safeMediaUrl('data:application/pdf;base64,AA==', 'file'),
        safeMediaUrl('data:text/html;base64,AA==', 'image'),
      ],
    };
  });
  assert.deepEqual(result, {
    paragraphs: 1, inlineCode: 'inline', links: 1,
    linkHref: 'https://example.test/x', linkTarget: '_blank', linkRel: 'noopener noreferrer',
    badLinkText: true, badLinks: 0,
    unordered: 2, ordered: 1,
    tableHead: ['a', 'b'], tableCells: ['1', '2'],
    copyText: 'Copy', codeLanguage: 'language-python', codeText: 'print(1)',
    pwned: 0,
    urls: [
      'https://example.test/a.png', 'http://example.test/a.png',
      'blob:https://example.test/x', '',
      'data:image/png;base64,AA==', '',
      'data:application/pdf;base64,AA==', '',
    ],
  });
});

/* 计划 Task 5：首页从「概览」换成「测试」，导航、数据页标记与模型选择器都要
   一次性到位。此用例在旧概览页上必然超时（没有 #testModel），所以先红后绿。 */
test('test home replaces overview and lists Auto plus configured models', async () => {
  await page.goto(routerBase + '/console/index.html');
  /* 原生 <optgroup> 在 <select> 里没有布局盒，Playwright 的 visible 判定永远等不到；
     这里等它 attached（同一断言意图：模型分组已渲染）。 */
  await page.waitForSelector('#testModel optgroup', { state: 'attached' });
  assert.equal(await page.locator('body').getAttribute('data-page'), 'test');
  assert.equal(await page.locator('.top nav a.on').textContent(), '测试');
  assert.equal(await page.locator('#testModel').inputValue(), 'auto');
  assert.ok(await page.locator('#testModel option[value="qwen3-32b"]').count());
  assert.equal(await page.locator('#cards').count(), 0);
  await page.locator('#testModel').selectOption('qwen3-32b');
  assert.match(await page.locator('#testStatus').textContent(), /固定模型/);
  await page.locator('#testModel').selectOption('auto');
  assert.match(await page.locator('#testStatus').textContent(), /智能路由/);
});

/* 计划 Task 6：附件全部只留在内存（FileReader → data URL → 协议 part），
   选择、拖拽、粘贴都落到同一套 chip；移除按附件 id 删并回收预览 URL。
   先红后绿：Task 5 的壳还没有任何附件处理器。 */
const QA_PIXEL = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=';
const QA_MARKDOWN = '# QA\n\nhello attachment\n';
const chipCount = () => document.querySelectorAll('#testFiles .tc-chip').length;

test('test chat attachments add, remove, drop and paste', async () => {
  await page.goto(routerBase + '/console/index.html');
  await page.waitForSelector('#testModel optgroup', { state: 'attached' });

  /* 空输入区 + 无附件时 Send 必须是禁用态。 */
  await waitFor(() => document.getElementById('testSend').disabled === true);
  assert.equal(await page.locator('#testSend').isDisabled(), true, 'Send must start disabled');

  /* 文件选择：一张内存 PNG + 一个 Markdown，两个 chip、一个图片预览、一个体积标签。 */
  await page.locator('#testFileInput').setInputFiles([
    { name: 'qa-pixel.png', mimeType: 'image/png', buffer: Buffer.from(QA_PIXEL, 'base64') },
    { name: 'qa-notes.md', mimeType: 'text/markdown', buffer: Buffer.from(QA_MARKDOWN, 'utf8') },
  ]);
  await waitFor(() => document.querySelectorAll('#testFiles .tc-chip').length === 2);
  assert.equal(await page.locator('#testFiles .tc-chip').count(), 2);
  assert.equal(await page.locator('#testFiles .tc-chip img').count(), 1, 'exactly one image preview');
  assert.match(await page.locator('#testFiles .tc-chip img').getAttribute('src'), /^blob:/,
    'image preview must be an in-memory blob URL');
  assert.equal(await page.locator('#testFiles .tc-chip img').getAttribute('alt'), 'qa-pixel.png');
  const mdChip = page.locator('#testFiles .tc-chip', { hasText: 'qa-notes.md' });
  assert.equal(await mdChip.count(), 1);
  assert.match(await mdChip.locator('.tc-chip-meta').textContent(), /^\d+ B$/,
    'chip must show a numeric file size');
  assert.ok(await mdChip.getAttribute('data-attachment-id'), 'chip carries its attachment id');
  /* 读完即清空 input.value，同一个文件才能被再次选择。 */
  await waitFor(() => document.getElementById('testFileInput').value === '');
  assert.equal(await page.locator('#testFileInput').evaluate(input => input.value), '',
    'file input must be cleared after reading');

  /* 空文本 + 一个附件也能发送。 */
  assert.equal(await page.locator('#testInput').inputValue(), '');
  assert.equal(await page.locator('#testSend').isDisabled(), false,
    'one attachment without text must enable Send');

  /* 移除 Markdown chip：只按 id 删一个，图片预览仍在。 */
  await mdChip.locator('.tc-chip-remove').click();
  await waitFor(() => document.querySelectorAll('#testFiles .tc-chip').length === 1);
  assert.equal(await page.locator('#testFiles .tc-chip', { hasText: 'qa-notes.md' }).count(), 0);
  assert.equal(await page.locator('#testFiles .tc-chip img').count(), 1);

  /* 拖拽：同一个 PNG 通过 DataTransfer 落到 .tc-panel，恰好新增一个附件。 */
  await page.evaluate(base64 => {
    const binary = Uint8Array.from(atob(base64), char => char.charCodeAt(0));
    const transfer = new DataTransfer();
    transfer.items.add(new File([binary], 'qa-drop.png', { type: 'image/png' }));
    document.querySelector('.tc-panel').dispatchEvent(
      new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: transfer }));
  }, QA_PIXEL);
  await waitFor(() => document.querySelectorAll('#testFiles .tc-chip').length === 2);
  assert.equal(await page.locator('#testFiles .tc-chip', { hasText: 'qa-drop.png' }).count(), 1);

  /* 粘贴：同一个 PNG 通过 ClipboardEvent 落到 #testInput，同样只新增一个附件。 */
  await page.locator('#testInput').click();
  await page.evaluate(base64 => {
    const binary = Uint8Array.from(atob(base64), char => char.charCodeAt(0));
    const transfer = new DataTransfer();
    transfer.items.add(new File([binary], 'qa-paste.png', { type: 'image/png' }));
    document.getElementById('testInput').dispatchEvent(new ClipboardEvent('paste', {
      clipboardData: transfer, bubbles: true, cancelable: true }));
  }, QA_PIXEL);
  await waitFor(() => document.querySelectorAll('#testFiles .tc-chip').length === 3);
  assert.equal(await page.locator('#testFiles .tc-chip', { hasText: 'qa-paste.png' }).count(), 1);
  assert.equal(await page.locator('#testFiles .tc-chip').count(), 3);
  assert.equal(await page.locator('#testFiles .tc-chip img').count(), 3);
});

/* ── 计划 Task 7：真实流式对话 ────────────────────────────────────────
   Auto 路由、固定模型多轮且不碰 JEV、附件 wire 形状、多模态 part 渲染、
   延迟流 Stop、内联 4xx 错误 + Retry、New Chat 中止并轮换会话 id。
   mock 触发词见 console_qa.mjs 的 mock /v1/chat/completions 分支。 */
async function openTestPage() {
  await page.goto(routerBase + '/console/index.html');
  await page.waitForSelector('#testModel optgroup', { state: 'attached' });
}

async function sendText(text) {
  await page.locator('#testInput').fill(text);
  await page.waitForFunction(() => !document.getElementById('testSend').disabled);
  await page.locator('#testSend').click();
}

function waitForAssistantText(text) {
  return waitFor(value => {
    const bubbles = Array.from(document.querySelectorAll('.tc-message.assistant .tc-bubble'));
    return bubbles.some(bubble => bubble.textContent.includes(value));
  }, text);
}

test('test chat auto routing streams the fixed reply', async () => {
  const chatCallsBefore = mockState.chatCalls.length;
  const jevCallsBefore = mockState.systemoneCalls.length;
  await openTestPage();
  await page.locator('#testModel').selectOption('auto');
  await sendText('你好，请走智能路由');
  await waitForAssistantText('固定回复');
  await waitFor(() =>
    document.querySelectorAll('.tc-message.assistant .tc-message-actions button').length === 2);
  assert.equal(mockState.chatCalls.length, chatCallsBefore + 1, 'one upstream call per send');
  assert.equal(mockState.systemoneCalls.length > jevCallsBefore, true,
    'Auto routing must consult the decision service first');
  const lastCall = mockState.chatCalls.at(-1);
  assert.equal(lastCall.stream, true);
  assert.equal(lastCall.messages.at(-1).role, 'user');
  assert.ok(typeof lastCall.model === 'string' && lastCall.model.length > 0,
    'upstream body must carry the routed upstream model');
  const meta = await page.locator('.tc-message.assistant .tc-message-meta').textContent();
  assert.match(meta, /^Auto → \S+$/);
  assert.ok(meta.includes(lastCall.model),
    `answer meta ${JSON.stringify(meta)} must name the upstream model ${lastCall.model}`);
  const actions = await page.locator('.tc-message.assistant .tc-message-actions button').allTextContents();
  assert.deepEqual(actions, ['复制', '重新生成']);
  assert.match(await page.locator('#testStatus').textContent(), /智能路由/);
  assert.equal(await page.locator('#testInput').inputValue(), '');
});

test('test chat direct model keeps multi-turn history and skips JEV', async () => {
  const chatCallsBefore = mockState.chatCalls.length;
  const jevCallsBefore = mockState.systemoneCalls.length;
  await openTestPage();
  await page.locator('#testModel').selectOption('qwen3-32b');
  assert.match(await page.locator('#testStatus').textContent(), /固定模型/);
  await sendText('第一轮');
  await waitFor(() => {
    const bubbles = document.querySelectorAll('.tc-message.assistant .tc-bubble');
    return bubbles.length === 1 && bubbles[0].textContent.includes('固定回复');
  });
  await sendText('第二轮');
  await waitFor(() => {
    const bubbles = document.querySelectorAll('.tc-message.assistant .tc-bubble');
    return bubbles.length === 2 && bubbles[1].textContent.includes('固定回复');
  });
  assert.equal(mockState.chatCalls.length, chatCallsBefore + 2);
  const lastCall = mockState.chatCalls.at(-1);
  assert.deepEqual(lastCall.messages.map(message => message.role), ['user', 'assistant', 'user']);
  assert.equal(lastCall.messages[1].content, '固定回复', 'assistant history stays wire-format text');
  assert.equal(lastCall.messages[2].content, '第二轮');
  assert.equal(lastCall.model, 'qwen3-32b');
  assert.equal(mockState.systemoneCalls.length, jevCallsBefore,
    'fixed model mode must not call the decision service');
  const metas = await page.locator('.tc-message.assistant .tc-message-meta').allTextContents();
  assert.deepEqual(metas, ['qwen3-32b · 固定模型', 'qwen3-32b · 固定模型']);
});

test('test chat stream sends the attachment as image_url', async () => {
  await openTestPage();
  await page.locator('#testModel').selectOption('qwen3-32b');
  await page.locator('#testFileInput').setInputFiles({
    name: 'qa-wire.png', mimeType: 'image/png', buffer: Buffer.from(QA_PIXEL, 'base64'),
  });
  await waitFor(() => document.querySelectorAll('#testFiles .tc-chip').length === 1);
  await page.waitForFunction(() => !document.getElementById('testSend').disabled);
  await page.locator('#testSend').click();
  await waitForAssistantText('固定回复');
  const lastCall = mockState.chatCalls.at(-1);
  const content = lastCall.messages.at(-1).content;
  assert.ok(Array.isArray(content), 'image-only turn must send a content part array');
  const image = content.find(part => part && part.type === 'image_url');
  assert.ok(image, 'missing image_url part');
  assert.match(image.image_url.url, /^data:image\/png;base64,/);
  assert.equal(await page.locator('#testFiles .tc-chip').count(), 0,
    'composer chips are cleared after send');
});

test('test chat multimodal answer renders every part type', async () => {
  const chatCallsBefore = mockState.chatCalls.length;
  await openTestPage();
  await page.locator('#testModel').selectOption('qwen3-32b');
  await sendText('返回多模态');
  await waitFor(() => Boolean(document.querySelector('.tc-message.assistant img.tc-media-image')));
  await waitFor(() =>
    document.querySelectorAll('.tc-message.assistant .tc-message-actions button').length === 2);
  const rendered = await page.evaluate(() => {
    const bubble = document.querySelector('.tc-message.assistant .tc-bubble');
    const image = bubble.querySelector('img.tc-media-image');
    const audio = bubble.querySelector('audio');
    const video = bubble.querySelector('video');
    const file = bubble.querySelector('a.tc-file');
    const details = bubble.querySelector('details');
    return {
      text: bubble.textContent,
      imageSrc: image && image.getAttribute('src'),
      audioControls: Boolean(audio && audio.hasAttribute('controls')),
      audioSrc: audio && audio.getAttribute('src'),
      videoControls: Boolean(video && video.hasAttribute('controls')),
      videoSrc: video && video.getAttribute('src'),
      fileText: file && file.textContent,
      fileHref: file && file.getAttribute('href'),
      fileDownload: file && file.getAttribute('download'),
      unknownFolded: Boolean(details && !details.open),
      unknownSummary: details && details.querySelector('summary').textContent,
      scripts: bubble.querySelectorAll('script').length,
    };
  });
  assert.match(rendered.text, /多模态回复/);
  assert.match(rendered.imageSrc, /^data:image\/png;base64,/);
  assert.equal(rendered.audioControls, true);
  assert.match(rendered.audioSrc, /^data:audio\/wav;base64,/);
  assert.equal(rendered.videoControls, true);
  assert.match(rendered.videoSrc, /^data:video\/mp4;base64,/);
  assert.equal(rendered.fileText, 'result.bin');
  assert.equal(rendered.fileDownload, 'result.bin');
  assert.match(rendered.fileHref, /^data:application\/octet-stream;base64,/);
  assert.equal(rendered.unknownFolded, true);
  assert.equal(rendered.unknownSummary, 'Unsupported content');
  assert.equal(rendered.scripts, 0);
  assert.equal(mockState.chatCalls.length, chatCallsBefore + 1);
});

test('test chat stop preserves the partial stream', async () => {
  await openTestPage();
  await page.locator('#testModel').selectOption('qwen3-32b');
  await sendText('延迟流');
  await waitForAssistantText('第一段');
  await waitFor(() => document.getElementById('testSend').textContent === '停止');
  assert.equal(await page.locator('#testInput').isDisabled(), true,
    'composer is locked while generating');
  await page.locator('#testSend').click();
  await waitFor(() => {
    const stopped = document.querySelector('.tc-message.assistant .tc-message-actions .mut');
    return Boolean(stopped && stopped.textContent === '已停止');
  });
  const afterStop = await page.evaluate(() => ({
    text: document.querySelector('.tc-message.assistant .tc-bubble').textContent,
    send: document.getElementById('testSend').textContent,
    sendDisabled: document.getElementById('testSend').disabled,
    stopped: document.querySelector('.tc-message.assistant .tc-message-actions .mut').textContent,
  }));
  assert.match(afterStop.text, /第一段/);
  assert.doesNotMatch(afterStop.text, /第二段/);
  assert.equal(afterStop.stopped, '已停止');
  assert.equal(afterStop.send, '发送');
  assert.equal(afterStop.sendDisabled, true);
  assert.equal(await page.locator('#testInput').isDisabled(), false);
  await page.waitForTimeout(1900);
  assert.doesNotMatch(await page.locator('.tc-message.assistant .tc-bubble').textContent(), /第二段/,
    'an aborted stream must not append later chunks');
});

test('test chat retry after an inline admin error', async () => {
  const chatCallsBefore = mockState.chatCalls.length;
  await openTestPage();
  await page.locator('#testModel').selectOption('qwen3-32b');
  /* 预期的 4xx 资源错误：先声明，再让路由只注入一次 422。 */
  expectHttpStatusError({ url: /\/admin\/test\/chat\/completions$/, status: /422/ });
  const routeMatcher = '**/admin/test/chat/completions';
  let injected = false;
  await page.route(routeMatcher, async route => {
    if (injected) { await route.continue(); return; }
    injected = true;
    await route.fulfill({
      status: 422,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'qa injected validation failure' }),
    });
  });
  try {
    await sendText('触发错误');
    await waitFor(() => {
      const error = document.querySelector('.tc-message.assistant .tc-error');
      return Boolean(error && error.textContent.includes('qa injected validation failure'));
    });
    const retry = page.locator('.tc-message.assistant .tc-message-actions button', { hasText: '重试' });
    assert.equal(await retry.count(), 1);
    assert.equal(await page.locator('.tc-message.assistant .tc-error').count(), 1);
    await retry.click();
    await waitFor(() => {
      const bubbles = document.querySelectorAll('.tc-message.assistant .tc-bubble');
      return bubbles.length === 1 && bubbles[0].textContent.includes('固定回复');
    });
    assert.equal(mockState.chatCalls.length, chatCallsBefore + 1,
      'Retry must issue a real upstream request');
    assert.equal(await page.locator('.tc-message.assistant .tc-error').count(), 0);
    assert.equal(await page.locator('.tc-message.assistant').count(), 1,
      'Retry removes only the failed assistant');
  } finally {
    await page.unroute(routeMatcher);
    resetExpectedHttpStatusErrors();
  }
});

test('test chat new chat aborts the stream and starts a new session', async () => {
  await openTestPage();
  await page.locator('#testModel').selectOption('qwen3-32b');
  const sessionIds = [];
  const captureSession = request => {
    if (request.url().endsWith('/admin/test/chat/completions')) {
      sessionIds.push(request.headers()['x-sr-session-id'] || '');
    }
  };
  page.on('request', captureSession);
  try {
    await page.locator('#testFileInput').setInputFiles({
      name: 'qa-new-chat.png', mimeType: 'image/png', buffer: Buffer.from(QA_PIXEL, 'base64'),
    });
    await waitFor(() => document.querySelectorAll('#testFiles .tc-chip').length === 1);
    await sendText('延迟流');
    await waitForAssistantText('第一段');
    assert.equal(await page.locator('#testFiles .tc-chip').count(), 0,
      'send clears the composer attachments');
    await page.locator('#testNew').click();
    await waitFor(() => document.querySelectorAll('.tc-message').length === 0 &&
      !document.getElementById('testEmpty').hidden);
    await page.waitForTimeout(1900);
    assert.equal(await page.locator('.tc-message').count(), 0,
      'aborted stream must not re-append after New Chat');
    /* New Chat 也要清掉空闲时新选的附件。 */
    await page.locator('#testFileInput').setInputFiles({
      name: 'qa-idle.png', mimeType: 'image/png', buffer: Buffer.from(QA_PIXEL, 'base64'),
    });
    await waitFor(() => document.querySelectorAll('#testFiles .tc-chip').length === 1);
    await page.locator('#testNew').click();
    await waitFor(() => document.querySelectorAll('#testFiles .tc-chip').length === 0);
    /* 下一条真实请求使用轮换后的会话 id。 */
    await sendText('你好');
    await waitForAssistantText('固定回复');
    assert.equal(sessionIds.length, 2, 'expected two real admin requests');
    assert.equal(sessionIds[0].length > 0, true);
    assert.equal(sessionIds[1].length > 0, true);
    assert.notEqual(sessionIds[1], sessionIds[0], 'New Chat must rotate the session id');
  } finally {
    page.off('request', captureSession);
  }
});

/* ── 计划 Task 8：首页截图（opt-in）────────────────────────────────────
   README 的 test.png 必须来自真实交互后的页面，而不是空壳：先通过真实 UI 发一条
   `你好`（默认 Auto，元信息显示 Auto → 路由到的上游模型），等流式「固定回复」完成
   （完成态才会渲染 Copy/Regenerate 按钮）后再截图。桌面 1440×1000、移动 390×844
   各截中英两张。screenshot() 自身在 QA_SCREENSHOTS=1 时才写文件；本用例开头再显式
   早退，保证普通 `npm run check:console` 不多做导航、也不会因没有截图文件而失败。 */
test('test chat screenshots (opt-in QA_SCREENSHOTS)', async () => {
  if (process.env.QA_SCREENSHOTS !== '1') return;
  const viewports = [
    { name: '1440', width: 1440, height: 1000 },
    { name: '390', width: 390, height: 844 },
  ];
  const languages = [
    { name: 'zh', htmlLang: 'zh-CN' },
    { name: 'en', htmlLang: 'en' },
  ];
  try {
    for (const viewport of viewports) {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      for (const language of languages) {
        await openTestPage();
        await page.locator(`.langtog button[data-lang="${language.name}"]`).click();
        await waitFor(expected => document.documentElement.lang === expected, language.htmlLang);
        await sendText('你好');
        await waitForAssistantText('固定回复');
        await waitFor(() =>
          document.querySelectorAll('.tc-message.assistant .tc-message-actions button').length === 2);
        /* 390×844 下页头换行会让面板比视口高（实测中文 27px、英文 43px），底栏
           会被裁掉一截；截图前滚到文档底部，让固定底栏完整入镜（页面本身可滚动，
           不改生产布局）。 */
        if (viewport.width < 640) {
          await page.evaluate(() =>
            window.scrollTo(0, document.documentElement.scrollHeight - window.innerHeight));
          await page.waitForTimeout(100);
        }
        await screenshot(`test-chat-${language.name}-${viewport.name}.png`);
      }
    }
  } finally {
    await page.setViewportSize({ width: 1440, height: 960 });
  }
});

await runIfMain(import.meta.url);
