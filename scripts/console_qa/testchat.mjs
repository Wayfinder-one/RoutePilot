/* RoutePilot 控制台 QA · 模型测试对话页（计划 Task 4–8）
   ------------------------------------------------------------------
   本文件只注册用例，不启动服务，也不持有页面状态：真实 router、浏览器、
   mock 上游与 teardown 全部由 ../console_qa.mjs 提供（同一进程内顺序执行）。
   覆盖：
   * Task 4 安全渲染：Markdown → DOM、恶意 HTML/活动 URL 不执行、
     图片/音频/视频/文件分派、未知 part 折叠、safeMediaUrl 白名单；
   * Task 5 首页壳：Test 导航、模型目录、Auto/固定模型状态；
   * Task 6 附件：选择/拖拽/粘贴、chip 预览与移除、空文本 + 附件可发送；
   * Task 7 流式：Auto/固定模型、多轮历史、附件 wire 形状、多模态 part、
     Stop、内联错误 + Retry、提前 EOF（未完成）、New Chat 中止与会话轮换；
   * Task 8 首页截图（仅在 QA_SCREENSHOTS=1 时执行）。
   入口：`QA_ONLY="test chat" node scripts/console_qa/testchat.mjs`；
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

  /* F1：围栏代码的 Copy 按钮在 Clipboard API 不可用时也必须安全（不能抛 pageerror）。
     render.js 的浏览器端兜底由 renderer 负责人并行修复；这里捕获窗口级 error 作为
     明确断言，harness 的 pageerror 守卫仍会兜底整轮失败。 */
  const rendererCopyErrors = await page.evaluate(async () => {
    const { renderParts } = await import('/console/chat/render.js');
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined });
    const captured = [];
    const onError = event => captured.push(String(event.message || (event.error && event.error.message) || event));
    window.addEventListener('error', onError);
    const host = document.createElement('div');
    document.body.append(host);
    renderParts(host, [{ type: 'text', text: '```js\nconst x = 1;\n```' }]);
    host.querySelector('.tc-code button').click();
    await new Promise(resolve => setTimeout(resolve, 80));
    window.removeEventListener('error', onError);
    host.remove();
    return captured;
  });
  assert.deepEqual(rendererCopyErrors, [],
    'renderer Copy must not throw when the Clipboard API is unavailable');
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
  /* Auto 必须落到具体上游（不是 "auto"），且是 QA bootstrap 注册过的模型之一。 */
  const routedModels = ['deepseek-v3', 'qwen3-8b', 'qwen3-32b', 'qwen3.8-flash-sft', 'tierless-model'];
  assert.notEqual(lastCall.model, 'auto', 'upstream body must carry a concrete model, not "auto"');
  assert.match(lastCall.model, /^[a-z0-9.-]+$/);
  assert.ok(routedModels.includes(lastCall.model),
    `auto routing picked an unexpected upstream ${JSON.stringify(lastCall.model)}`);
  const meta = await page.locator('.tc-message.assistant .tc-message-meta').textContent();
  assert.match(meta, /^Auto → \S+$/);
  assert.ok(meta.includes(lastCall.model),
    `answer meta ${JSON.stringify(meta)} must name the upstream model ${lastCall.model}`);
  const actions = await page.locator('.tc-message.assistant .tc-message-actions button').allTextContents();
  assert.deepEqual(actions, ['复制', '重新生成']);
  assert.match(await page.locator('#testStatus').textContent(), /智能路由/);
  assert.equal(await page.locator('#testInput').inputValue(), '');

  /* I1：语言切换按 state.messages 重建消息区，动作按钮与元信息同步换语言。 */
  await page.locator('.langtog button[data-lang="en"]').click();
  await page.waitForFunction(() => document.documentElement.lang === 'en');
  await waitFor(() => {
    const labels = [...document.querySelectorAll('.tc-message.assistant .tc-message-actions button')]
      .map(button => button.textContent);
    return labels.length === 2 && labels[0] === 'Copy' && labels[1] === 'Regenerate';
  });
  assert.match(await page.locator('.tc-message.assistant .tc-message-meta').textContent(),
    /^Auto → \S+$/);
  await page.locator('.langtog button[data-lang="zh"]').click();
  await page.waitForFunction(() => document.documentElement.lang === 'zh-CN');
  await waitFor(() => {
    const labels = [...document.querySelectorAll('.tc-message.assistant .tc-message-actions button')]
      .map(button => button.textContent);
    return labels.length === 2 && labels[0] === '复制' && labels[1] === '重新生成';
  });

  /* F1：Copy 点击。Clipboard API 不可用时走 execCommand 兜底，且不得抛 pageerror。 */
  await page.evaluate(() => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined });
    window.__copyFallbacks = 0;
    document.execCommand = () => { window.__copyFallbacks += 1; return true; };
  });
  await page.locator('.tc-message.assistant .tc-message-actions button', { hasText: '复制' }).click();
  await page.waitForTimeout(120);
  assert.equal(await page.evaluate(() => window.__copyFallbacks), 1,
    'Copy must fall back to execCommand when the Clipboard API is unavailable');

  /* F2：390×844 窄屏（中文页头换行）下底栏必须完整可见且整页不需滚动。 */
  await page.setViewportSize({ width: 390, height: 844 });
  try {
    const fit = await page.evaluate(() => {
      const composer = document.querySelector('.tc-composer');
      const panel = document.querySelector('.tc-panel');
      return {
        innerHeight: window.innerHeight,
        composerBottom: Math.round(composer.getBoundingClientRect().bottom),
        panelBottom: Math.round(panel.getBoundingClientRect().bottom),
        scrollHeight: document.documentElement.scrollHeight,
      };
    });
    assert.ok(fit.composerBottom <= fit.innerHeight,
      `composer must be above the fold at 390x844 (bottom ${fit.composerBottom} > ${fit.innerHeight})`);
    assert.ok(fit.panelBottom <= fit.innerHeight,
      `panel must fit at 390x844 (bottom ${fit.panelBottom} > ${fit.innerHeight})`);
    assert.ok(fit.scrollHeight <= fit.innerHeight,
      `mobile page must not scroll at 390x844 (scrollHeight ${fit.scrollHeight})`);
  } finally {
    await page.setViewportSize({ width: 1440, height: 960 });
  }
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
  /* M10：旧回答只保留 Copy；Regenerate 只出现在最后一条 assistant 上。 */
  const actionRows = await page.evaluate(() =>
    [...document.querySelectorAll('.tc-message.assistant')].map(view =>
      [...view.querySelectorAll('.tc-message-actions button')].map(button => button.textContent)));
  assert.deepEqual(actionRows, [['复制'], ['复制', '重新生成']]);

  /* F1：点击重新生成 —— 恰好一次真实上游调用，只替换最后一条 assistant。 */
  const callsBeforeRegenerate = mockState.chatCalls.length;
  const firstAnswer = await page.locator('.tc-message.assistant .tc-bubble').first().textContent();
  await page.locator('.tc-message.assistant').last()
    .locator('.tc-message-actions button', { hasText: '重新生成' }).click();
  await waitFor(() => {
    const bubbles = document.querySelectorAll('.tc-message.assistant .tc-bubble');
    if (bubbles.length !== 2) return false;
    const buttons = document.querySelectorAll('.tc-message.assistant .tc-message-actions button');
    return bubbles[1].textContent.includes('固定回复') && buttons.length === 3;
  });
  assert.equal(mockState.chatCalls.length, callsBeforeRegenerate + 1,
    'Regenerate must issue exactly one real upstream request');
  const regenerateCall = mockState.chatCalls.at(-1);
  assert.deepEqual(regenerateCall.messages.map(message => message.role),
    ['user', 'assistant', 'user'], 'Regenerate must replay the wire-format history');
  const afterRegenerate = await page.evaluate(() => ({
    assistants: document.querySelectorAll('.tc-message.assistant').length,
    firstAnswerText: document.querySelector('.tc-message.assistant .tc-bubble').textContent,
    firstMeta: document.querySelector('.tc-message.assistant .tc-message-meta').textContent,
    actionRows: [...document.querySelectorAll('.tc-message.assistant')].map(view =>
      [...view.querySelectorAll('.tc-message-actions button')].map(button => button.textContent)),
  }));
  assert.equal(afterRegenerate.assistants, 2, 'Regenerate replaces only the last assistant');
  assert.equal(afterRegenerate.firstAnswerText, firstAnswer, 'earlier answers stay untouched');
  assert.equal(afterRegenerate.firstMeta, 'qwen3-32b · 固定模型');
  assert.deepEqual(afterRegenerate.actionRows, [['复制'], ['复制', '重新生成']]);
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
  /* I1（生成中）：语言切换按已缓存的 parts 重建消息区，输入区不得解锁。 */
  await page.locator('.langtog button[data-lang="en"]').click();
  await page.waitForFunction(() => document.documentElement.lang === 'en');
  assert.equal(await page.locator('#testInput').isDisabled(), true,
    'language repaint must not unlock the composer mid-stream');
  assert.match(await page.locator('.tc-message.assistant .tc-bubble').textContent(), /第一段/);
  await page.locator('.langtog button[data-lang="zh"]').click();
  await page.waitForFunction(() => document.documentElement.lang === 'zh-CN');
  await waitFor(() => document.getElementById('testSend').textContent === '停止');
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

  /* M4：上游提前 EOF（没有 [DONE]）→ 未完成态，保留已收到的文本，不给完成动作。 */
  await sendText('截断流');
  await waitFor(() => {
    const assistants = document.querySelectorAll('.tc-message.assistant');
    const last = assistants[assistants.length - 1];
    const mark = last && last.querySelector('.tc-message-actions .mut');
    return Boolean(mark && mark.textContent === '未完成（流被中断）');
  });
  const incomplete = await page.evaluate(() => {
    const assistants = document.querySelectorAll('.tc-message.assistant');
    const last = assistants[assistants.length - 1];
    return {
      assistantCount: assistants.length,
      text: last.querySelector('.tc-bubble').textContent,
      mark: last.querySelector('.tc-message-actions .mut').textContent,
      buttons: last.querySelectorAll('.tc-message-actions button').length,
      send: document.getElementById('testSend').textContent,
      inputDisabled: document.getElementById('testInput').disabled,
    };
  });
  assert.equal(incomplete.mark, '未完成（流被中断）');
  assert.match(incomplete.text, /截断回复/);
  assert.equal(incomplete.buttons, 0, 'an incomplete stream offers no completed actions');
  assert.equal(incomplete.send, '发送');
  assert.equal(incomplete.inputDisabled, false);
  assert.equal(incomplete.assistantCount, 2);
});

test('test chat retry after an inline admin error', async () => {
  const chatCallsBefore = mockState.chatCalls.length;
  await openTestPage();
  await page.locator('#testModel').selectOption('qwen3-32b');
  /* 一次性注入队列：先 422（管理面 {detail}），Retry 之后补一次 503（数据面
     {error:{message}}）。中间与之后的请求都走真实 router -> mock 上游。 */
  expectHttpStatusError({ url: /\/admin\/test\/chat\/completions$/, status: /422/ });
  expectHttpStatusError({ url: /\/admin\/test\/chat\/completions$/, status: /503/ });
  const routeMatcher = '**/admin/test/chat/completions';
  const pending = [{ status: 422, body: { detail: 'qa injected validation failure' } }];
  await page.route(routeMatcher, async route => {
    const injection = pending.shift();
    if (!injection) { await route.continue(); return; }
    await route.fulfill({
      status: injection.status,
      contentType: 'application/json',
      body: JSON.stringify(injection.body),
    });
  });
  try {
    await sendText('触发错误');
    await waitFor(() => {
      const error = document.querySelector('.tc-message.assistant .tc-error');
      return Boolean(error && error.textContent.includes('qa injected validation failure'));
    });
    let retry = page.locator('.tc-message.assistant .tc-message-actions button', { hasText: '重试' });
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

    /* M6：数据面 {error:{message}} 形状 -> 内联文本是 message 本身，不是 JSON 串。 */
    pending.push({ status: 503, body: { error: { message: 'qa upstream unavailable' } } });
    await sendText('再触发错误');
    await waitFor(() => {
      const error = document.querySelector('.tc-message.assistant .tc-error');
      return Boolean(error && error.textContent.includes('qa upstream unavailable'));
    });
    const upstreamError = await page.locator('.tc-message.assistant .tc-error').textContent();
    assert.equal(upstreamError, 'qa upstream unavailable',
      'admin error text must not be JSON-stringified');
    assert.doesNotMatch(upstreamError, /\{.*\}/);
    retry = page.locator('.tc-message.assistant .tc-message-actions button', { hasText: '重试' });
    assert.equal(await retry.count(), 1);
    await retry.click();
    /* 旧的成功回答保留，只替换失败的那一条。 */
    await waitFor(() => {
      const bubbles = document.querySelectorAll('.tc-message.assistant .tc-bubble');
      const last = bubbles[bubbles.length - 1];
      return bubbles.length === 2 && last.textContent.includes('固定回复') &&
        !document.querySelector('.tc-message.assistant .tc-error');
    });
    assert.equal(mockState.chatCalls.length, chatCallsBefore + 2,
      'both retries must issue real upstream requests');
    assert.equal(await page.locator('.tc-message.assistant').count(), 2,
      'Retry keeps earlier completed answers and replaces only the failed one');
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
    /* M8：New Chat 必须真的中止在途请求（浏览器侧 net::ERR_ABORTED）。 */
    const abortedRequest = page.waitForEvent('requestfailed', {
      predicate: request => request.url().includes('/admin/test/chat/completions'),
      timeout: 8000,
    });
    await page.locator('#testNew').click();
    const failed = await abortedRequest;
    const failure = failed.failure();
    assert.match(String(failure && failure.errorText), /ABORT/i,
      'New Chat must abort the in-flight request');
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
   各截中英两张；移动端先断言底栏完整可见且页面无需滚动。screenshot() 自身在
   QA_SCREENSHOTS=1 时才写文件；本用例开头再显式早退，保证普通
   `npm run check:console` 不多做导航、也不会因没有截图文件而失败。 */
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
        /* F2：窄屏布局改为 body 列 flex，页头换行（zh/en 高度不同）不再把底栏顶出
           视口；截图前断言底栏完整可见且页面无需滚动，保证截图是诚实的首屏证据。 */
        if (viewport.width < 640) {
          const fit = await page.evaluate(() => {
            const composer = document.querySelector('.tc-composer');
            const panel = document.querySelector('.tc-panel');
            return {
              innerHeight: window.innerHeight,
              composerBottom: Math.round(composer.getBoundingClientRect().bottom),
              panelBottom: Math.round(panel.getBoundingClientRect().bottom),
              scrollHeight: document.documentElement.scrollHeight,
            };
          });
          assert.ok(fit.composerBottom <= fit.innerHeight,
            `[${language.name}] composer must be above the fold at 390x844`);
          assert.ok(fit.panelBottom <= fit.innerHeight,
            `[${language.name}] panel must fit at 390x844`);
          assert.ok(fit.scrollHeight <= fit.innerHeight,
            `[${language.name}] mobile page must not scroll at 390x844`);
        }
        await screenshot(`test-chat-${language.name}-${viewport.name}.png`);
      }
    }
  } finally {
    await page.setViewportSize({ width: 1440, height: 960 });
  }
});

await runIfMain(import.meta.url);
