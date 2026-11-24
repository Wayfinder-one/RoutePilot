/* RoutePilot 控制台 QA · 模型测试对话页（计划 Task 4–8）
   ------------------------------------------------------------------
   本文件只注册用例，不启动服务，也不持有页面状态：真实 router、浏览器、
   mock 上游与 teardown 全部由 ../console_qa.mjs 提供（同一进程内顺序执行）。
   覆盖：
   * Task 4 安全渲染：Markdown → DOM、恶意 HTML/活动 URL 不执行、
     图片/音频/视频/文件分派、未知 part 折叠、safeMediaUrl 白名单、
     复制链整链失败写进 Copy 按钮标签；
   * Task 5 首页壳：Test 导航、Auto 状态行（不提供模型选择）、390/800/1440
     首屏适配、viewer 锁定输入区；
   * Task 6 附件：五类文件 chip 与移除、拖拽/粘贴、空文本 + 附件启用 Send、
     文本文件免 Data URL 转换、预估超限先于读取就地拒绝、单文件失败内联；
   * Task 7 流式（具名）：auto stream / multi-turn text / stop / incomplete eof /
     new generation race（代次守卫回归）/ provider stream error；附件 wire、
     多模态 part、Retry、New Chat 清场仍由 v1 用例覆盖，Task 8 具名收编；
   * Task 8（具名）：message-level media / content media / attachment wire /
     exact oversize request / retry / regenerate / copy / new chat clears
     media；另有首页截图（仅在 QA_SCREENSHOTS=1 时执行）。
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
  /* 计划 Task 4 Step 1 的唯一综合用例（合并 v1 的两条 renderer 用例）：一次
     renderParts 渲染 Markdown 全部块级/行内元素、http + javascript: 两种链接、
     图片/音频/视频/文件/未知五类 part，并注入字面 <script> 与 javascript:
     图片。所有主动内容只作为文本显示或被丢弃，绝不执行。 */
  const result = await page.evaluate(async () => {
    const { renderParts } = await import('/console/chat/render.js');
    const host = document.createElement('div');
    renderParts(host, [
      {
        type: 'text',
        text: [
          '# RoutePilot QA',
          '',
          '**bold** statement with literal <script>window.pwned=1</script> kept as text',
          '',
          'Paragraph with `inline` code, a [safe](https://example.test/x) link and a [bad](javascript:window.pwned=9) link.',
          '',
          '- one',
          '- two',
          '',
          '1. first',
          '2. second',
          '',
          '| a | b |',
          '| --- | --- |',
          '| 1 | 2 |',
          '',
          '```python',
          'print(1)',
          '```',
        ].join('\n'),
      },
      { type: 'image', url: 'javascript:window.pwned=2' },
      { type: 'image', url: 'data:image/png;base64,iVBORw0KGgo=', name: 'pixel.png' },
      { type: 'audio', url: 'data:audio/mpeg;base64,AA==', name: 'a.mp3' },
      { type: 'audio', url: 'javascript:window.pwned=3' },
      { type: 'video', url: 'data:video/mp4;base64,AA==', name: 'v.mp4' },
      { type: 'video', url: 'javascript:window.pwned=4' },
      { type: 'file', url: 'data:application/pdf;base64,AA==', name: 'a.pdf' },
      { type: 'file', url: 'javascript:window.pwned=5' },
      { type: 'unknown', value: { answer: 42 } },
    ]);
    const link = host.querySelector('p a');
    const download = host.querySelector('a[download]');
    return {
      heading: host.querySelector('h1').textContent,
      bold: host.querySelector('strong').textContent,
      paragraphs: host.querySelectorAll('p').length,
      scriptLiteral: [...host.querySelectorAll('p')].some(node =>
        node.textContent.includes('<script>window.pwned=1</script>')),
      inlineCode: host.querySelector('p code').textContent,
      links: host.querySelectorAll('p a').length,
      linkHref: link.getAttribute('href'),
      linkTarget: link.target,
      linkRel: link.rel,
      badLinkText: [...host.querySelectorAll('p')].some(node => node.textContent.includes('bad')),
      unordered: [...host.querySelectorAll('ul li')].map(node => node.textContent),
      ordered: [...host.querySelectorAll('ol li')].map(node => node.textContent),
      tableHead: [...host.querySelectorAll('table th')].map(node => node.textContent),
      tableCells: [...host.querySelectorAll('table td')].map(node => node.textContent),
      codeLanguage: host.querySelector('.tc-code code').className,
      codeText: host.querySelector('.tc-code code').textContent,
      copyButton: host.querySelector('.tc-code button').textContent,
      scripts: host.querySelectorAll('script').length,
      activeUrls: host.querySelectorAll('[src^="javascript:"],[href^="javascript:"]').length,
      images: host.querySelectorAll('img.tc-media-image').length,
      imageAlt: host.querySelector('img.tc-media-image').alt,
      imageSrc: host.querySelector('img.tc-media-image').getAttribute('src'),
      audio: host.querySelectorAll('audio[controls]').length,
      audioSrc: host.querySelector('audio').getAttribute('src'),
      video: host.querySelectorAll('video[controls]').length,
      videoSrc: host.querySelector('video').getAttribute('src'),
      downloads: host.querySelectorAll('a[download]').length,
      downloadText: download.textContent,
      downloadHref: download.getAttribute('href'),
      unknownSummary: host.querySelector('details summary').textContent,
      unknownFolded: !host.querySelector('details').open,
      unknown: host.querySelector('details pre').textContent,
      pwned: window.pwned || 0,
    };
  });
  assert.deepEqual(result, {
    heading: 'RoutePilot QA',
    bold: 'bold',
    paragraphs: 2,
    scriptLiteral: true,
    inlineCode: 'inline',
    links: 1,
    linkHref: 'https://example.test/x',
    linkTarget: '_blank',
    linkRel: 'noopener noreferrer',
    badLinkText: true,
    unordered: ['one', 'two'],
    ordered: ['first', 'second'],
    tableHead: ['a', 'b'],
    tableCells: ['1', '2'],
    codeLanguage: 'language-python',
    codeText: 'print(1)',
    copyButton: 'Copy',
    scripts: 0,
    activeUrls: 0,
    images: 1,
    imageAlt: 'pixel.png',
    imageSrc: 'data:image/png;base64,iVBORw0KGgo=',
    audio: 1,
    audioSrc: 'data:audio/mpeg;base64,AA==',
    video: 1,
    videoSrc: 'data:video/mp4;base64,AA==',
    downloads: 1,
    downloadText: 'a.pdf',
    downloadHref: 'data:application/pdf;base64,AA==',
    unknownSummary: 'Unsupported content',
    unknownFolded: true,
    unknown: '{\n  "answer": 42\n}',
    pwned: 0,
  });

  /* F1：围栏代码的 Copy 按钮在 Clipboard API 不可用时也必须安全（不能抛
     pageerror）：textarea+execCommand 兜底接管。这里捕获窗口级 error 作为明确
     断言，harness 的 pageerror 守卫仍会兜底整轮失败。 */
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
    const fenceText = host.querySelector('.tc-code code').textContent;
    host.remove();
    return { fenceText, captured };
  });
  assert.deepEqual(rendererCopyErrors, { fenceText: 'const x = 1;', captured: [] },
    'renderer Copy must not throw when the Clipboard API is unavailable');

  /* 评审决议（绑定）：整条复制链失败时把失败写进按钮标签 —— 剪贴板不可用且
     execCommand 返回 false → 标签变 Copy failed；随后 execCommand 成功（剪贴板
     仍不可用）→ 标签恢复 Copy。全程仍不得产生未捕获页面错误。 */
  const failureLabel = await page.evaluate(async () => {
    const { renderParts } = await import('/console/chat/render.js');
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined });
    const captured = [];
    const onError = event => captured.push(String(event.message || (event.error && event.error.message) || event));
    window.addEventListener('error', onError);
    const host = document.createElement('div');
    document.body.append(host);
    renderParts(host, [{ type: 'text', text: '```js\nconst x = 1;\n```' }]);
    const button = host.querySelector('.tc-code button');
    document.execCommand = () => false;
    button.click();
    await new Promise(resolve => setTimeout(resolve, 80));
    const failed = button.textContent;
    document.execCommand = () => true;
    button.click();
    await new Promise(resolve => setTimeout(resolve, 80));
    const restored = button.textContent;
    window.removeEventListener('error', onError);
    host.remove();
    delete document.execCommand;
    return { failed, restored, captured };
  });
  assert.deepEqual(failureLabel, { failed: 'Copy failed', restored: 'Copy', captured: [] },
    'Copy must display failure in its label only when the whole chain fails');
});

/* safeMediaUrl 白名单矩阵（v1 额外覆盖，保留）：http(s)/blob 全类型放行，
   data: 仅类型匹配（file 额外放行任意 data: 下载），javascript: 一律拒绝；
   http(s)/blob 的分派路径由上面的综合用例以 data: 源 + 断言覆盖。 */
test('test chat renderer safeMediaUrl whitelist matrix', async () => {
  await page.goto(routerBase + '/console/index.html');
  const urls = await page.evaluate(async () => {
    const { safeMediaUrl } = await import('/console/chat/render.js');
    return [
      safeMediaUrl('https://example.test/a.png', 'image'),
      safeMediaUrl('http://example.test/a.png', 'image'),
      safeMediaUrl('blob:https://example.test/x', 'video'),
      safeMediaUrl('javascript:alert(1)', 'image'),
      safeMediaUrl('data:image/png;base64,AA==', 'image'),
      safeMediaUrl('data:audio/mpeg;base64,AA==', 'image'),
      safeMediaUrl('data:application/pdf;base64,AA==', 'file'),
      safeMediaUrl('data:text/html;base64,AA==', 'image'),
    ];
  });
  assert.deepEqual(urls, [
    'https://example.test/a.png', 'http://example.test/a.png',
    'blob:https://example.test/x', '',
    'data:image/png;base64,AA==', '',
    'data:application/pdf;base64,AA==', '',
  ]);
});

/* 计划 Task 5（修订）：首页壳。页面只保留 Auto，不提供模型选择（2026-11-23
   决策）；390/800/1440 三档「首屏装下底栏且整页不滚动」的布局回归也在这里。 */
async function openShellPage(waitMarker, waitState) {
  /* 壳用例都以 zh 断言文案：导航前先把 router_lang 种进 localStorage；
     about:blank 没有同源 storage，先拿一次页面建立 origin。 */
  if (!page.url().startsWith(routerBase)) {
    await page.goto(routerBase + '/console/index.html');
  }
  await page.evaluate(() => localStorage.setItem('router_lang', 'zh'));
  await page.goto(routerBase + '/console/index.html');
  /* boot 填充 Auto 状态行之前 #testStatus 是空节点：等它非空即证明模块跑过。 */
  await page.waitForSelector(waitMarker || '#testStatus:not(:empty)', {
    state: waitState || 'visible', timeout: 15000,
  });
}

test('test home replaces overview with an auto-only status line', async () => {
  await openShellPage();
  assert.equal(await page.locator('body').getAttribute('data-page'), 'test');
  assert.equal(await page.locator('.top nav a.on').textContent(), '测试');
  /* 模型选择器与空目录入口都已移除：页面只走 Auto。 */
  assert.equal(await page.locator('#testModel').count(), 0,
    'the model selector must be gone');
  assert.equal(await page.locator('#testModelEmpty').count(), 0,
    'the empty-model notice must be gone');
  assert.equal(await page.locator('#cards').count(), 0);
  const status = await page.locator('#testStatus').textContent();
  assert.match(status, /^Auto · 智能路由/);
  assert.doesNotMatch(status, /固定模型/);

  /* F2/F3：窄屏（手机 390×844）+ 中档（800×900，shared.css 页头换行断点内）
     + 桌面（1440×1000）三档都必须首屏装下底栏且整页不滚动；桌面尺寸与旧布局一致。 */
  const fitBands = [
    { name: '390x844', width: 390, height: 844 },
    { name: '800x900', width: 800, height: 900 },
    { name: '1440x1000', width: 1440, height: 1000 },
  ];
  try {
    for (const band of fitBands) {
      await page.setViewportSize({ width: band.width, height: band.height });
      const fit = await page.evaluate(() => {
        const composer = document.querySelector('.tc-composer');
        const panel = document.querySelector('.tc-panel');
        return {
          innerHeight: window.innerHeight,
          scrollY: window.scrollY,
          composerBottom: Math.round(composer.getBoundingClientRect().bottom),
          panelBottom: Math.round(panel.getBoundingClientRect().bottom),
          scrollHeight: document.documentElement.scrollHeight,
        };
      });
      const label = band.name;
      assert.equal(fit.scrollY, 0, `${label} must not be scrolled on load`);
      assert.ok(fit.composerBottom <= fit.innerHeight + 1,
        `${label}: composer must be above the fold (bottom ${fit.composerBottom} > ${fit.innerHeight})`);
      assert.ok(fit.panelBottom <= fit.innerHeight + 1,
        `${label}: panel must fit (bottom ${fit.panelBottom} > ${fit.innerHeight})`);
      assert.ok(fit.scrollHeight <= fit.innerHeight + 1,
        `${label}: page must not scroll (scrollHeight ${fit.scrollHeight} > ${fit.innerHeight})`);
    }
  } finally {
    await page.setViewportSize({ width: 1440, height: 960 });
  }
});

/* 计划 Task 5：viewer 角色锁死整个输入区（textarea/附件/发送），并亮出
   test.adminOnly 文案。 */
test('test viewer cannot use the composer', async () => {
  const meMatcher = '**/admin/me';
  await page.route(meMatcher, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ id: 'qa-viewer', email: 'viewer@example.test', role: 'viewer' }),
  }));
  try {
    await openShellPage();
    assert.equal(await page.locator('#testInput').isDisabled(), true,
      'the textarea must be locked for a viewer');
    assert.equal(await page.locator('#testAttach').isDisabled(), true,
      'the attach button must be locked for a viewer');
    assert.equal(await page.locator('#testFileInput').isDisabled(), true,
      'the file input must be locked for a viewer');
    assert.equal(await page.locator('#testSend').isDisabled(), true,
      'send must be locked for a viewer');
    assert.equal(await page.locator('#testStatus').textContent(), '只有管理员可以测试模型');
    assert.equal(await page.locator('#testInput').getAttribute('placeholder'),
      '只有管理员可以测试模型');
  } finally {
    await page.unroute(meMatcher);
  }
});

/* ── 计划 Task 6：附件预检与文本直读 ──────────────────────────────────
   v1 的单条「add, remove, drop and paste」用例拆成两条并保留全部断言：
   * 五类文件（PNG/MD/YAML/PDF/MP3）的 chip / 预览 / 体积 / 移除；
   * 拖拽与粘贴各落一个附件，空文本 + 附件启用 Send。
   再加三条新行为用例（先红）：文本文件只读 file.text()（无 Data URL 转换）、
   预估超限先于任何读取就地拒绝（无 FileReader / 无 admin 请求 / 不加 chip）、
   单文件读取失败只内联该文件错误且其余文件可用。 */
const QA_PIXEL = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=';
const QA_MARKDOWN = '# QA\n\nhello attachment\n';
const QA_YAML = 'key: value\n';

test('test chat attachments add chips for every file type', async () => {
  await openTestPage();
  /* 空输入区 + 无附件时 Send 必须是禁用态。 */
  await waitFor(() => document.getElementById('testSend').disabled === true);
  assert.equal(await page.locator('#testSend').isDisabled(), true, 'Send must start disabled');

  /* 文件选择：PNG（图片预览）+ MD/YAML（文本）+ PDF/MP3（二进制），五个 chip。 */
  await page.locator('#testFileInput').setInputFiles([
    { name: 'qa-pixel.png', mimeType: 'image/png', buffer: Buffer.from(QA_PIXEL, 'base64') },
    { name: 'qa-notes.md', mimeType: 'text/markdown', buffer: Buffer.from(QA_MARKDOWN, 'utf8') },
    { name: 'qa-config.yaml', mimeType: 'text/yaml', buffer: Buffer.from(QA_YAML, 'utf8') },
    { name: 'qa-manual.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4 qa', 'utf8') },
    { name: 'qa-audio.mp3', mimeType: 'audio/mpeg', buffer: Buffer.from('ID3qa-audio', 'utf8') },
  ]);
  await waitFor(() => document.querySelectorAll('#testFiles .tc-chip').length === 5);
  assert.equal(await page.locator('#testFiles .tc-chip').count(), 5);
  assert.equal(await page.locator('#testFiles .tc-chip img').count(), 1, 'exactly one image preview');
  assert.match(await page.locator('#testFiles .tc-chip img').getAttribute('src'), /^blob:/,
    'image preview must be an in-memory blob URL');
  assert.equal(await page.locator('#testFiles .tc-chip img').getAttribute('alt'), 'qa-pixel.png');
  for (const name of ['qa-notes.md', 'qa-config.yaml', 'qa-manual.pdf', 'qa-audio.mp3']) {
    const chip = page.locator('#testFiles .tc-chip', { hasText: name });
    assert.equal(await chip.count(), 1, `${name} chip missing`);
    assert.match(await chip.locator('.tc-chip-meta').textContent(), /^\d+ B$/,
      `${name} chip must show a numeric file size`);
    assert.ok(await chip.getAttribute('data-attachment-id'), `${name} chip carries its attachment id`);
  }
  /* 读完即清空 input.value，同一个文件才能被再次选择。 */
  await waitFor(() => document.getElementById('testFileInput').value === '');
  assert.equal(await page.locator('#testFileInput').evaluate(input => input.value), '',
    'file input must be cleared after reading');

  /* 移除 YAML chip：只按 id 删一个，PNG 预览与其余 chip 原样保留。 */
  const yamlChip = page.locator('#testFiles .tc-chip', { hasText: 'qa-config.yaml' });
  await yamlChip.locator('.tc-chip-remove').click();
  await waitFor(() => document.querySelectorAll('#testFiles .tc-chip').length === 4);
  assert.equal(await page.locator('#testFiles .tc-chip', { hasText: 'qa-config.yaml' }).count(), 0);
  assert.equal(await page.locator('#testFiles .tc-chip img').count(), 1);
});

test('test chat attachments drop and paste add one each', async () => {
  await openTestPage();
  assert.equal(await page.locator('#testSend').isDisabled(), true, 'Send must start disabled');

  /* 拖拽：同一个 PNG 通过 DataTransfer 落到 .tc-panel，恰好新增一个附件。 */
  await page.evaluate(base64 => {
    const binary = Uint8Array.from(atob(base64), char => char.charCodeAt(0));
    const transfer = new DataTransfer();
    transfer.items.add(new File([binary], 'qa-drop.png', { type: 'image/png' }));
    document.querySelector('.tc-panel').dispatchEvent(
      new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: transfer }));
  }, QA_PIXEL);
  await waitFor(() => document.querySelectorAll('#testFiles .tc-chip').length === 1);
  assert.equal(await page.locator('#testFiles .tc-chip', { hasText: 'qa-drop.png' }).count(), 1);

  /* 空文本 + 一个附件也能启用 Send（发送动作本身在 Task 7 恢复）。 */
  assert.equal(await page.locator('#testInput').inputValue(), '');
  await page.waitForFunction(() => !document.getElementById('testSend').disabled, null,
    { timeout: 15000 });
  assert.equal(await page.locator('#testSend').isDisabled(), false,
    'one attachment without text must enable Send');

  /* 粘贴：同一个 PNG 通过 ClipboardEvent 落到 #testInput，同样只新增一个附件。 */
  await page.locator('#testInput').click();
  await page.evaluate(base64 => {
    const binary = Uint8Array.from(atob(base64), char => char.charCodeAt(0));
    const transfer = new DataTransfer();
    transfer.items.add(new File([binary], 'qa-paste.png', { type: 'image/png' }));
    document.getElementById('testInput').dispatchEvent(new ClipboardEvent('paste', {
      clipboardData: transfer, bubbles: true, cancelable: true }));
  }, QA_PIXEL);
  await waitFor(() => document.querySelectorAll('#testFiles .tc-chip').length === 2);
  assert.equal(await page.locator('#testFiles .tc-chip', { hasText: 'qa-paste.png' }).count(), 1);
  assert.equal(await page.locator('#testFiles .tc-chip img').count(), 2);
});

/* 计划 Task 6：文本文件只读 file.text()，绝不再走 Data URL 转换。
   在页面里给 FileReader.prototype.readAsDataURL 挂计数器即可证明。
   上游 wire 正文的断言归 Task 8（`test chat attachment wire`）。 */
test('test chat attachments read text files without data urls', async () => {
  await openTestPage();
  await page.evaluate(() => {
    window.__dataUrlReads = 0;
    window.__savedReadAsDataURL = FileReader.prototype.readAsDataURL;
    FileReader.prototype.readAsDataURL = function (...args) {
      window.__dataUrlReads += 1;
      return window.__savedReadAsDataURL.apply(this, args);
    };
  });
  try {
    await page.locator('#testFileInput').setInputFiles({
      name: 'qa-config.yaml', mimeType: 'text/yaml', buffer: Buffer.from(QA_YAML, 'utf8'),
    });
    await waitFor(() => document.querySelectorAll('#testFiles .tc-chip').length === 1);
    const chip = page.locator('#testFiles .tc-chip', { hasText: 'qa-config.yaml' });
    assert.equal(await chip.count(), 1, 'a text-file chip must exist');
    assert.ok(await chip.getAttribute('data-attachment-id'), 'the chip carries its attachment id');
    assert.equal(await page.evaluate(() => window.__dataUrlReads), 0,
      'a text file must not be converted to a Data URL');
  } finally {
    await page.evaluate(() => {
      FileReader.prototype.readAsDataURL = window.__savedReadAsDataURL;
    });
  }
});

/* 计划 Task 6：预估超限的文件在读取之前就被拒绝 —— 8MB 二进制的 base64 膨胀
   估算 ≈ 10.7MB，超出 10MB 请求体上限。断言三件事：就地内联提示、零
   FileReader 转换（挂计数器）、零 admin 测试请求；chip 也不得出现。 */
test('test oversized attachment is rejected before any read', async () => {
  await openTestPage();
  await page.evaluate(() => {
    window.__dataUrlReads = 0;
    window.__savedReadAsDataURL = FileReader.prototype.readAsDataURL;
    FileReader.prototype.readAsDataURL = function (...args) {
      window.__dataUrlReads += 1;
      return window.__savedReadAsDataURL.apply(this, args);
    };
  });
  const chatRequests = [];
  const onRequest = request => {
    if (request.url().includes('/admin/test/chat/completions')) chatRequests.push(request.url());
  };
  page.on('request', onRequest);
  try {
    await page.locator('#testFileInput').setInputFiles({
      name: 'qa-big.bin', mimeType: 'application/octet-stream',
      buffer: Buffer.alloc(8 * 1024 * 1024, 7),
    });
    await waitFor(() => !document.getElementById('testError').hidden, null, { timeout: 8000 });
    assert.equal(await page.locator('#testError').isHidden(), false,
      'the oversize rejection must surface inline');
    assert.equal(await page.locator('#testError').textContent(),
      '请求体超过 10 MB 上限，请减少附件或历史消息');
    assert.equal(await page.locator('#testFiles .tc-chip').count(), 0,
      'an estimated-oversize file must not become a chip');
    assert.equal(await page.evaluate(() => window.__dataUrlReads), 0,
      'the oversize file must be rejected before any FileReader conversion');
    assert.equal(chatRequests.length, 0, 'no admin test request may be issued');
  } finally {
    page.off('request', onRequest);
    await page.evaluate(() => {
      FileReader.prototype.readAsDataURL = window.__savedReadAsDataURL;
    });
  }
});

/* 计划 Task 6：单文件读取失败只内联「文件名: 错误」，不得炸成未捕获页面
   错误，同批的好文件照常可用（可移除、可继续添加新附件）。 */
test('test chat attachments read failure shows a per-file inline error', async () => {
  await openTestPage();
  await page.evaluate(() => {
    window.__capturedPageErrors = [];
    window.addEventListener('error', event => {
      window.__capturedPageErrors.push(String(event.message || event));
    });
    window.addEventListener('unhandledrejection', event => {
      window.__capturedPageErrors.push('rejection: ' +
        String(event.reason && event.reason.message || event.reason));
    });
    /* File.prototype.text 原本继承自 Blob.prototype：挂一个自有覆盖，
       finally 里 delete 即可还原继承链。 */
    File.prototype.text = function () {
      if (this.name === 'qa-broken.md') {
        return Promise.reject(new Error('forced read failure'));
      }
      return Object.getPrototypeOf(File.prototype).text.call(this);
    };
  });
  try {
    await page.locator('#testFileInput').setInputFiles([
      { name: 'qa-broken.md', mimeType: 'text/markdown', buffer: Buffer.from('# broken\n', 'utf8') },
      { name: 'qa-good.png', mimeType: 'image/png', buffer: Buffer.from(QA_PIXEL, 'base64') },
    ]);
    await waitFor(() => document.querySelectorAll('#testFiles .tc-chip').length === 1);
    assert.equal(await page.locator('#testFiles .tc-chip', { hasText: 'qa-good.png' }).count(), 1,
      'the valid file must stay usable as a chip');
    assert.equal(await page.locator('#testError').isHidden(), false,
      'the read failure must surface inline');
    assert.equal(await page.locator('#testError').textContent(), 'qa-broken.md: forced read failure');
    assert.deepEqual(await page.evaluate(() => window.__capturedPageErrors), [],
      'a per-file read failure must not become an unhandled page error');
    /* 好文件仍然可用：既能移除，也能继续添加新附件。 */
    await page.locator('#testFiles .tc-chip', { hasText: 'qa-good.png' })
      .locator('.tc-chip-remove').click();
    await waitFor(() => document.querySelectorAll('#testFiles .tc-chip').length === 0);
    await page.locator('#testFileInput').setInputFiles({
      name: 'qa-after.png', mimeType: 'image/png', buffer: Buffer.from(QA_PIXEL, 'base64'),
    });
    await waitFor(() => document.querySelectorAll('#testFiles .tc-chip').length === 1);
    assert.equal(await page.locator('#testFiles .tc-chip', { hasText: 'qa-after.png' }).count(), 1,
      'the attachment pipeline must keep working after a failed read');
  } finally {
    await page.evaluate(() => { delete File.prototype.text; });
  }
});

/* ── 计划 Task 7：真实流式对话（修订版具名用例）──────────────────────
   auto stream / multi-turn text / stop / incomplete eof /
   new generation race（评审发现 1 的回归：旧请求的 finally 不得清掉新
   controller）/ provider stream error（SSE 错误负载内联且绝不完成）。
   固定模型的直连路径由后端测试覆盖，页面只保留 Auto（2026-11-23）。
   附件 wire 形状、多模态 part、Retry/Regenerate 流程与 New Chat 清场
   仍由 v1 用例覆盖，Task 8 按计划具名重排。
   mock 触发词见 console_qa.mjs 的 mock /v1/chat/completions 分支。 */
async function openTestPage() {
  /* 对话用例都以 zh 断言文案：导航前先把 router_lang 种进 localStorage
     （与壳用例 openShellPage 同一策略；about:blank 先拿一次页面建立 origin）。 */
  if (!page.url().startsWith(routerBase)) {
    await page.goto(routerBase + '/console/index.html');
  }
  await page.evaluate(() => localStorage.setItem('router_lang', 'zh'));
  await page.goto(routerBase + '/console/index.html');
  await page.waitForFunction(() =>
    document.getElementById('testStatus').textContent.includes('智能路由'));
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

test('test chat auto stream', async () => {
  const chatCallsBefore = mockState.chatCalls.length;
  const jevCallsBefore = mockState.systemoneCalls.length;
  await openTestPage();
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
});

/* 计划 Task 7：多轮文本 —— 第二次请求体必须是 user / 原始 assistant 内容 /
   user（无损历史回放，绝不做渲染往返转换）。重新生成段已按计划 Task 8
   平移进独立的 `test chat regenerate` 用例（断言原样，未弱化）。 */
test('test chat multi-turn text', async () => {
  const chatCallsBefore = mockState.chatCalls.length;
  await openTestPage();
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
  assert.equal(lastCall.messages[1].content, '固定回复',
    'the second body must replay the ORIGINAL assistant content, not render-derived parts');
  assert.equal(lastCall.messages[2].content, '第二轮');
  assert.notEqual(lastCall.model, 'auto', 'auto routing must send a concrete upstream model');
  const metas = await page.locator('.tc-message.assistant .tc-message-meta').allTextContents();
  assert.equal(metas.length, 2);
  metas.forEach(meta => assert.match(meta, /^Auto → \S+$/));
  /* M10：旧回答只保留 Copy；Regenerate 只出现在最后一条 assistant 上。 */
  const actionRows = await page.evaluate(() =>
    [...document.querySelectorAll('.tc-message.assistant')].map(view =>
      [...view.querySelectorAll('.tc-message-actions button')].map(button => button.textContent)));
  assert.deepEqual(actionRows, [['复制'], ['复制', '重新生成']]);
});

/* ── 计划 Task 8：附件 wire 与多模态历史 ───────────────────────────── */

/* 计划 Task 8：附件 wire —— PNG→image_url、MP3→input_audio（裸 base64）、
   PDF→file.file_data、YAML 正文进 text part；发送后 chip 清空（v1 断言保留）。 */
test('test chat attachment wire', async () => {
  await openTestPage();
  const mp3Body = Buffer.from('ID3qa-audio', 'utf8');
  await page.locator('#testFileInput').setInputFiles([
    { name: 'qa-wire.png', mimeType: 'image/png', buffer: Buffer.from(QA_PIXEL, 'base64') },
    { name: 'qa-wire.mp3', mimeType: 'audio/mpeg', buffer: mp3Body },
    { name: 'qa-wire.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4 qa', 'utf8') },
    { name: 'qa-wire.yaml', mimeType: 'text/yaml', buffer: Buffer.from(QA_YAML, 'utf8') },
  ]);
  await waitFor(() => document.querySelectorAll('#testFiles .tc-chip').length === 4);
  /* 空文本 + 四个附件直接发送。 */
  assert.equal(await page.locator('#testInput').inputValue(), '');
  await page.waitForFunction(() => !document.getElementById('testSend').disabled);
  await page.locator('#testSend').click();
  await waitForAssistantText('固定回复');
  const lastCall = mockState.chatCalls.at(-1);
  const content = lastCall.messages.at(-1).content;
  assert.ok(Array.isArray(content), 'the attachment turn must send a content part array');
  const image = content.find(part => part && part.type === 'image_url');
  assert.ok(image, 'missing image_url part');
  assert.match(image.image_url.url, /^data:image\/png;base64,/);
  const audio = content.find(part => part && part.type === 'input_audio');
  assert.ok(audio, 'missing input_audio part');
  assert.equal(audio.input_audio.format, 'mp3');
  assert.equal(audio.input_audio.data, mp3Body.toString('base64'),
    'input_audio.data must be the raw base64 payload without the data: prefix');
  const file = content.find(part => part && part.type === 'file');
  assert.ok(file, 'missing file part');
  assert.equal(file.file.filename, 'qa-wire.pdf');
  assert.match(file.file.file_data, /^data:application\/pdf;base64,/);
  const text = content.find(part => part && part.type === 'text');
  assert.ok(text, 'missing text part for the YAML attachment');
  assert.match(text.text, /\[File: qa-wire\.yaml\]/);
  assert.ok(text.text.includes('key: value'), 'the YAML body must travel as text');
  assert.equal(await page.locator('#testFiles .tc-chip').count(), 0,
    'composer chips are cleared after send');
});

/* 计划 Task 8：消息级媒体 —— mock 把图片/音频放在 content 之外
   （delta.images / delta.audio）投递，气泡照常渲染；下一轮请求里的
   assistant 历史只有 role+content，content 是原始回放，绝不含消息级
   图片/音频，也没有任何渲染派生字段。 */
test('test chat message-level media', async () => {
  const chatCallsBefore = mockState.chatCalls.length;
  await openTestPage();
  await sendText('返回多模态');
  await waitFor(() => Boolean(document.querySelector('.tc-message.assistant img.tc-media-image')));
  await waitFor(() =>
    document.querySelectorAll('.tc-message.assistant .tc-message-actions button').length === 2);
  const rendered = await page.evaluate(() => {
    const bubble = document.querySelector('.tc-message.assistant .tc-bubble');
    const image = bubble.querySelector('img.tc-media-image');
    const audio = bubble.querySelector('audio');
    return {
      text: bubble.textContent,
      imageSrc: image && image.getAttribute('src'),
      audioControls: Boolean(audio && audio.hasAttribute('controls')),
      audioSrc: audio && audio.getAttribute('src'),
      scripts: bubble.querySelectorAll('script').length,
    };
  });
  assert.match(rendered.text, /多模态回复/);
  assert.match(rendered.imageSrc, /^data:image\/png;base64,/);
  assert.equal(rendered.audioControls, true);
  assert.match(rendered.audioSrc, /^data:audio\/wav;base64,/);
  assert.equal(rendered.scripts, 0);
  /* 下一轮：assistant 历史不带消息级媒体，也不做渲染往返转换。 */
  await sendText('你好');
  await waitForAssistantText('固定回复');
  assert.equal(mockState.chatCalls.length, chatCallsBefore + 2);
  const history = mockState.chatCalls.at(-1).messages[1];
  assert.deepEqual(Object.keys(history), ['role', 'content'],
    'history must carry only role and content — no render-derived images/audio fields');
  assert.equal(history.role, 'assistant');
  assert.ok(Array.isArray(history.content), 'the streamed content array replays verbatim');
  const historyTypes = history.content.map(part => part && part.type);
  assert.equal(historyTypes[0], 'text');
  assert.equal(history.content[0].text, '多模态回复：',
    'the ORIGINAL streamed text must survive into the next request');
  assert.equal(historyTypes.includes('image_url'), false,
    'message-level images must not leak into the content history');
  assert.equal(historyTypes.includes('audio') || historyTypes.includes('input_audio'), false,
    'message-level audio must not leak into the content history');
});

/* 计划 Task 8：内容 part —— 视频/文件/未知 part 渲染；下一轮请求原样保留
   原始 content part（video_url 还是 video_url，绝不做 partToWire 式转换）。 */
test('test chat content media', async () => {
  const chatCallsBefore = mockState.chatCalls.length;
  await openTestPage();
  await sendText('返回多模态');
  await waitFor(() => Boolean(document.querySelector('.tc-message.assistant video')));
  await waitFor(() =>
    document.querySelectorAll('.tc-message.assistant .tc-message-actions button').length === 2);
  const rendered = await page.evaluate(() => {
    const bubble = document.querySelector('.tc-message.assistant .tc-bubble');
    const video = bubble.querySelector('video');
    const file = bubble.querySelector('a.tc-file');
    const details = bubble.querySelector('details');
    return {
      videoControls: Boolean(video && video.hasAttribute('controls')),
      videoSrc: video && video.getAttribute('src'),
      fileText: file && file.textContent,
      fileHref: file && file.getAttribute('href'),
      fileDownload: file && file.getAttribute('download'),
      unknownFolded: Boolean(details && !details.open),
      unknownSummary: details && details.querySelector('summary').textContent,
    };
  });
  assert.equal(rendered.videoControls, true);
  assert.match(rendered.videoSrc, /^data:video\/mp4;base64,/);
  assert.equal(rendered.fileText, 'result.bin');
  assert.equal(rendered.fileDownload, 'result.bin');
  assert.match(rendered.fileHref, /^data:application\/octet-stream;base64,/);
  assert.equal(rendered.unknownFolded, true);
  assert.equal(rendered.unknownSummary, 'Unsupported content');
  /* 下一轮：content 深度等于原始 part 数组（逐字段，含未知 part）。 */
  await sendText('你好');
  await waitForAssistantText('固定回复');
  assert.equal(mockState.chatCalls.length, chatCallsBefore + 2);
  assert.deepEqual(mockState.chatCalls.at(-1).messages[1].content, [
    { type: 'text', text: '多模态回复：' },
    { type: 'video_url', video_url: { url: 'data:video/mp4;base64,AAAA' } },
    { type: 'file', file: { filename: 'result.bin', file_data: 'data:application/octet-stream;base64,AA==' } },
    { type: 'custom', value: { source: 'qa' } },
  ], 'the next request must preserve the ORIGINAL content parts verbatim');
});

/* 计划 Task 8：精确超限 —— 小附件过预检（估算只看附件），但文本把完整
   JSON 信封顶过 10MB 精确上限：就地内联报错、零新请求、不推进任何状态。 */
test('test chat exact oversize request', async () => {
  const chatCallsBefore = mockState.chatCalls.length;
  await openTestPage();
  /* 小附件：预检（估算）放行。 */
  await page.locator('#testFileInput').setInputFiles({
    name: 'qa-small.png', mimeType: 'image/png', buffer: Buffer.from(QA_PIXEL, 'base64'),
  });
  await waitFor(() => document.querySelectorAll('#testFiles .tc-chip').length === 1);
  /* 大文本：估算看不见文本，精确 JSON 检查必须拦下（11MB > 10MB 上限）。 */
  await page.evaluate(() => {
    const input = document.getElementById('testInput');
    input.value = 'a'.repeat(11 * 1024 * 1024);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await page.waitForFunction(() => !document.getElementById('testSend').disabled, null,
    { timeout: 15000 });
  await page.locator('#testSend').click();
  await waitFor(() => !document.getElementById('testError').hidden, null, { timeout: 8000 });
  const rejected = await page.evaluate(() => ({
    error: document.getElementById('testError').textContent,
    messages: document.querySelectorAll('.tc-message').length,
    inputLength: document.getElementById('testInput').value.length,
    chips: document.querySelectorAll('#testFiles .tc-chip').length,
  }));
  assert.equal(rejected.error, '请求体超过 10 MB 上限，请减少附件或历史消息');
  assert.equal(rejected.messages, 0, 'no user message may be pushed on an oversize request');
  assert.equal(rejected.inputLength, 11 * 1024 * 1024,
    'the composer state must be untouched by the rejected send');
  assert.equal(rejected.chips, 1,
    'the attachment stays in the composer — only the send is rejected');
  await page.waitForTimeout(300);
  assert.equal(mockState.chatCalls.length, chatCallsBefore, 'ZERO new upstream call');
});

/* 计划 Task 7：延迟流上 Stop —— 部分文本保留、输入区在生成中锁定
   （R-T5 评审回补 a）、语言重绘不解锁、中止后上游补写不再落地。 */
test('test chat stop', async () => {
  await openTestPage();
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
});

/* 计划 Task 7：上游提前 EOF（没有 [DONE] / finish_reason）→ 未完成态，
   保留已收到的文本，绝不落成完成态（没有 Copy/Regenerate）。 */
test('test chat incomplete eof', async () => {
  await openTestPage();
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
  assert.equal(incomplete.assistantCount, 1);
});

/* 计划 Task 7（评审发现 1 的回归）：代次竞态 —— 启动延迟流 A，点 New Chat
   立刻发 B；A 落定后 B 仍是活请求（按钮还是「停止」＝ A 的 finally 没清掉
   B 的 controller），Stop 只作用于 B；消息区只渲染 B 这一轮对话；B 的请求
   头带 New Chat 轮换后的新 x-sr-session-id；两条被放弃的流都不得补写。 */
test('test chat new generation race', async () => {
  const chatCallsBefore = mockState.chatCalls.length;
  await openTestPage();
  const sessionIds = [];
  const captureSession = request => {
    if (request.url().endsWith('/admin/test/chat/completions')) {
      sessionIds.push(request.headers()['x-sr-session-id'] || '');
    }
  };
  page.on('request', captureSession);
  try {
    /* A：延迟流在途，等「第一段」确认。 */
    await sendText('延迟流');
    await waitForAssistantText('第一段');
    /* M8（v1 保留）：New Chat 必须真的中止 A（浏览器侧 net::ERR_ABORTED）。
       事件 promise 先建好不阻塞，点完 New Chat 立刻发 B。 */
    const aAbort = page.waitForEvent('requestfailed', {
      predicate: request => request.url().includes('/admin/test/chat/completions'),
      timeout: 8000,
    });
    await page.locator('#testNew').click();
    /* B：新一轮对话，同样延迟流，保持 B 在途可停止。 */
    await sendText('延迟流');
    await waitFor(() => {
      const users = document.querySelectorAll('.tc-message.user');
      const assistants = document.querySelectorAll('.tc-message.assistant');
      return users.length === 1 && assistants.length === 1 &&
        assistants[0].textContent.includes('第一段');
    });
    /* A 落定：abort 事件 + A 的 finally 必须无痕退场。 */
    const failed = await aAbort;
    assert.match(String(failed.failure() && failed.failure().errorText), /ABORT/i,
      'New Chat must abort the in-flight request A');
    /* B 仍是活请求：A 的 finally 不得清掉 B 的 controller —— 按钮还得是
       「停止」，输入区仍锁定；消息区只有 B 这一轮。 */
    await waitFor(() => document.getElementById('testSend').textContent === '停止');
    const race = await page.evaluate(() => ({
      assistants: document.querySelectorAll('.tc-message.assistant').length,
      users: document.querySelectorAll('.tc-message.user').length,
      text: document.querySelector('.tc-message.assistant .tc-bubble').textContent,
      send: document.getElementById('testSend').textContent,
      inputDisabled: document.getElementById('testInput').disabled,
    }));
    assert.equal(race.assistants, 1, 'only conversation B renders; A never re-appends');
    assert.equal(race.users, 1, 'only B’s user bubble remains after New Chat');
    assert.match(race.text, /第一段/);
    assert.equal(race.send, '停止', 'B must still be the live request after A settles');
    assert.equal(race.inputDisabled, true, 'the composer stays locked while B streams');
    /* 会话轮换：B 的请求头必须是新 x-sr-session-id。 */
    assert.equal(sessionIds.length, 2, 'A and B each issued one admin request');
    assert.equal(sessionIds[0].length > 0, true);
    assert.equal(sessionIds[1].length > 0, true);
    assert.notEqual(sessionIds[1], sessionIds[0], 'New Chat must rotate the session id');
    /* Stop 只作用于当前 controller：B 停在第一段，带「已停止」标记。 */
    await page.locator('#testSend').click();
    await waitFor(() => {
      const stopped = document.querySelector('.tc-message.assistant .tc-message-actions .mut');
      return Boolean(stopped && stopped.textContent === '已停止');
    });
    const after = await page.evaluate(() => ({
      text: document.querySelector('.tc-message.assistant .tc-bubble').textContent,
      stopped: document.querySelector('.tc-message.assistant .tc-message-actions .mut').textContent,
      send: document.getElementById('testSend').textContent,
      inputDisabled: document.getElementById('testInput').disabled,
    }));
    assert.match(after.text, /第一段/);
    assert.doesNotMatch(after.text, /第二段/);
    assert.equal(after.stopped, '已停止');
    assert.equal(after.send, '发送');
    assert.equal(after.inputDisabled, false);
    /* A 的中止流与 B 的停止流都不得再补写。 */
    await page.waitForTimeout(1900);
    assert.doesNotMatch(await page.locator('.tc-message.assistant .tc-bubble').textContent(), /第二段/,
      'neither the aborted A nor the stopped B may append later chunks');
    assert.equal(mockState.chatCalls.length, chatCallsBefore + 2,
      'exactly one upstream call per conversation');
  } finally {
    page.off('request', captureSession);
  }
});

/* 计划 Task 7：SSE 错误负载 —— mock 用「流错误」触发词发 {"error":…}，
   parser 抛错走 catch：内联可见、绝不落成完成态（只有 Retry，没有
   Copy/Regenerate，也没有未完成标记）。 */
test('test chat provider stream error', async () => {
  const chatCallsBefore = mockState.chatCalls.length;
  await openTestPage();
  await sendText('流错误');
  await waitFor(() => {
    const error = document.querySelector('.tc-message.assistant .tc-error');
    return Boolean(error && error.textContent.includes('mock stream failure'));
  });
  const failed = await page.evaluate(() => {
    const assistants = document.querySelectorAll('.tc-message.assistant');
    const last = assistants[assistants.length - 1];
    return {
      error: last.querySelector('.tc-error').textContent,
      buttons: [...last.querySelectorAll('.tc-message-actions button')].map(node => node.textContent),
      marks: [...last.querySelectorAll('.tc-message-actions .mut')].map(node => node.textContent),
      send: document.getElementById('testSend').textContent,
      inputDisabled: document.getElementById('testInput').disabled,
    };
  });
  assert.equal(failed.error, 'mock stream failure');
  assert.deepEqual(failed.buttons, ['重试'], 'a provider error is never complete: only Retry');
  assert.deepEqual(failed.marks, [], 'a provider error is not an incomplete mark');
  assert.equal(failed.send, '发送');
  assert.equal(failed.inputDisabled, false);
  assert.equal(mockState.chatCalls.length, chatCallsBefore + 1);
});

/* 计划 Task 8：重试 —— 一次性 422 内联（{detail}），Retry 只移除失败的
   assistant 并真实重发；数据面 {error:{message}} 不得 JSON 化（v1 断言全保留）。 */
test('test chat retry', async () => {
  const chatCallsBefore = mockState.chatCalls.length;
  await openTestPage();
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

/* 计划 Task 8：重新生成 —— 只移除最后一条完成的 assistant，早轮原样保留，
   恰好一次真实上游请求并回放 wire 历史（自 Task 7 的 multi-turn 用例
   平移收编，断言原样未弱化）。 */
test('test chat regenerate', async () => {
  await openTestPage();
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
  /* F1：点击重新生成 —— 恰好一次真实上游调用，只替换最后一条 assistant。 */
  const callsBeforeRegenerate = mockState.chatCalls.length;
  const firstAnswer = await page.locator('.tc-message.assistant .tc-bubble').first().textContent();
  const firstMeta = await page.locator('.tc-message.assistant .tc-message-meta').first().textContent();
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
  assert.match(afterRegenerate.firstMeta, /^Auto → \S+$/);
  assert.equal(afterRegenerate.firstMeta, firstMeta,
    'Regenerate must not rewrite the earlier answer metadata');
  assert.deepEqual(afterRegenerate.actionRows, [['复制'], ['复制', '重新生成']]);
});

/* 计划 Task 8：消息复制 —— 只复制归一化 text part（不带 JSON/媒体 URL）；
   整条复制链失败时按 render.js 契约把失败写进按钮标签，不得抛页面错误，
   兜底链成功后标签恢复。 */
test('test chat copy', async () => {
  await openTestPage();
  await sendText('返回多模态');
  await waitFor(() =>
    document.querySelectorAll('.tc-message.assistant .tc-message-actions button').length === 2);
  /* 1) 捕获式 clipboard：写入内容必须只有文本 part。 */
  await page.evaluate(() => {
    window.__copied = [];
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: text => { window.__copied.push(text); return Promise.resolve(); } },
    });
  });
  await page.locator('.tc-message.assistant .tc-message-actions button', { hasText: '复制' }).click();
  await waitFor(() => window.__copied.length === 1);
  assert.deepEqual(await page.evaluate(() => window.__copied), ['多模态回复：'],
    'Copy must join only the text parts — no JSON, no media URLs');
  assert.equal(await page.locator('.tc-message.assistant .tc-message-actions button',
    { hasText: '复制' }).textContent(), '复制',
    'a successful copy leaves the button label alone');
  /* 2) 整链失败：clipboard 拒绝 + execCommand false → 按钮标签变 Copy failed。 */
  await page.evaluate(() => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: () => Promise.reject(new Error('denied')) },
    });
    document.execCommand = () => false;
    window.__capturedCopyErrors = [];
    window.addEventListener('error', event => {
      window.__capturedCopyErrors.push(String(event.message || event));
    });
    window.addEventListener('unhandledrejection', event => {
      window.__capturedCopyErrors.push('rejection: ' +
        String(event.reason && event.reason.message || event.reason));
    });
  });
  await page.locator('.tc-message.assistant .tc-message-actions button', { hasText: '复制' }).click();
  await page.waitForFunction(() =>
    [...document.querySelectorAll('.tc-message.assistant .tc-message-actions button')]
      .some(button => button.textContent === 'Copy failed'));
  assert.deepEqual(await page.evaluate(() => window.__capturedCopyErrors), [],
    'a failed copy chain must not become an unhandled page error');
  /* 3) 兜底链成功（clipboard 不可用 + execCommand true）→ 标签恢复。 */
  await page.evaluate(() => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined });
    document.execCommand = () => true;
  });
  await page.locator('.tc-message.assistant .tc-message-actions button',
    { hasText: 'Copy failed' }).click();
  await page.waitForFunction(() =>
    [...document.querySelectorAll('.tc-message.assistant .tc-message-actions button')]
      .some(button => button.textContent === '复制'));
  /* 清理现场：还原原型补丁，后续用例不受影响。 */
  await page.evaluate(() => {
    delete navigator.clipboard;
    delete document.execCommand;
  });
});

/* 计划 Task 8：New Chat 清场 —— 中止在途请求（net::ERR_ABORTED，v1 断言
   保留）、清空消息/附件并回收预览 blob URL、恢复空态、轮换会话 id；
   已发送内容渲染用的是 Data URL（读取管线产物），与被回收的 blob 无关。 */
test('test chat new chat clears media', async () => {
  await openTestPage();
  const sessionIds = [];
  const captureSession = request => {
    if (request.url().endsWith('/admin/test/chat/completions')) {
      sessionIds.push(request.headers()['x-sr-session-id'] || '');
    }
  };
  page.on('request', captureSession);
  await page.evaluate(() => {
    window.__savedRevokeObjectURL = URL.revokeObjectURL;
    window.__revokedUrls = [];
    URL.revokeObjectURL = function (url) {
      window.__revokedUrls.push(String(url));
      return window.__savedRevokeObjectURL.apply(this, arguments);
    };
  });
  try {
    await page.locator('#testFileInput').setInputFiles({
      name: 'qa-new-chat.png', mimeType: 'image/png', buffer: Buffer.from(QA_PIXEL, 'base64'),
    });
    await waitFor(() => document.querySelectorAll('#testFiles .tc-chip').length === 1);
    const sentPreview = await page.locator('#testFiles .tc-chip img').getAttribute('src');
    assert.match(sentPreview, /^blob:/, 'the chip preview is an in-memory blob URL');
    await sendText('延迟流');
    await waitForAssistantText('第一段');
    assert.equal(await page.locator('#testFiles .tc-chip').count(), 0,
      'send clears the composer attachments');
    /* 独立性：已发送的用户气泡渲染自 Data URL（FileReader 产物），
       不依赖预览 blob —— 回收 blob 不影响已发送内容。 */
    const userBubbleSrc = await page.locator('.tc-message.user img').first().getAttribute('src');
    assert.match(userBubbleSrc, /^data:image\/png;base64,/,
      'sent content renders from its Data URL, never from the preview blob');
    assert.equal(
      await page.evaluate(url => window.__revokedUrls.includes(url), sentPreview), true,
      'the sent attachment’s preview blob must be revoked when the composer clears');
    /* M8（v1 保留）：New Chat 必须真的中止在途请求（浏览器侧 net::ERR_ABORTED）。 */
    const abortedRequest = page.waitForEvent('requestfailed', {
      predicate: request => request.url().includes('/admin/test/chat/completions'),
      timeout: 8000,
    });
    await page.locator('#testNew').click();
    const failed = await abortedRequest;
    const failure = failed.failure();
    assert.match(String(failure && failure.errorText), /ABORT/i,
      'New Chat must abort the in-flight request');
    const cleared = await page.evaluate(() => ({
      messages: document.querySelectorAll('.tc-message').length,
      empty: !document.getElementById('testEmpty').hidden,
      chips: document.querySelectorAll('#testFiles .tc-chip').length,
      inputDisabled: document.getElementById('testInput').disabled,
    }));
    assert.equal(cleared.messages, 0);
    assert.equal(cleared.empty, true, 'the empty state must be restored');
    assert.equal(cleared.chips, 0);
    assert.equal(cleared.inputDisabled, false);
    await page.waitForTimeout(1900);
    assert.equal(await page.locator('.tc-message').count(), 0,
      'aborted stream must not re-append after New Chat');
    /* New Chat 也要清掉空闲时新选的附件，并回收它的预览 blob。 */
    await page.locator('#testFileInput').setInputFiles({
      name: 'qa-idle.png', mimeType: 'image/png', buffer: Buffer.from(QA_PIXEL, 'base64'),
    });
    await waitFor(() => document.querySelectorAll('#testFiles .tc-chip').length === 1);
    const idlePreview = await page.locator('#testFiles .tc-chip img').getAttribute('src');
    await page.locator('#testNew').click();
    await waitFor(() => document.querySelectorAll('#testFiles .tc-chip').length === 0);
    assert.equal(
      await page.evaluate(url => window.__revokedUrls.includes(url), idlePreview), true,
      'New Chat must revoke the idle attachment’s preview blob');
    /* 下一条真实请求使用轮换后的会话 id。 */
    await sendText('你好');
    await waitForAssistantText('固定回复');
    assert.equal(sessionIds.length, 2, 'expected two real admin requests');
    assert.equal(sessionIds[0].length > 0, true);
    assert.equal(sessionIds[1].length > 0, true);
    assert.notEqual(sessionIds[1], sessionIds[0], 'New Chat must rotate the session id');
  } finally {
    page.off('request', captureSession);
    await page.evaluate(() => {
      URL.revokeObjectURL = window.__savedRevokeObjectURL;
    });
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
