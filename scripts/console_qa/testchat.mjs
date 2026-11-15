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
import { page, routerBase, runIfMain, test } from '../console_qa.mjs';

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

await runIfMain(import.meta.url);
