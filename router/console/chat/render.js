/* Chat 安全 Markdown 与多模态 DOM 渲染 · 浏览器专用模块
   ------------------------------------------------------------------
   模型内容一律通过 document.createElement + textContent 生成节点，绝不使用
   innerHTML：原始 HTML（含 <script>）只作为文本显示，不执行。
   renderMarkdown 支持安全的标题/段落/列表/表格/链接/行内代码/围栏代码块
   （围栏代码附 Copy 按钮）；renderParts 在文本之外分派图片/音频/视频/文件，
   未识别 part 折叠为 JSON。媒体 URL 只允许 http(s)/blob，以及类型匹配的
   data:（通用 data: 仅限文件下载），javascript: 等主动 scheme 一律拒绝。
   本模块只依赖浏览器 API，不被 Node 单测导入；页面通过动态 import 加载。
   ------------------------------------------------------------------ */

export function safeMediaUrl(value, kind) {
  const url = String(value || '');
  if (/^https?:\/\//i.test(url) || /^blob:/i.test(url)) return url;
  if (kind === 'file' && /^data:/i.test(url)) return url;
  if (url.startsWith(`data:${kind}/`)) return url;
  return '';
}

function inlineNodes(text) {
  const nodes = [];
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\))/g;
  let cursor = 0;
  for (const match of String(text).matchAll(pattern)) {
    if (match.index > cursor) nodes.push(document.createTextNode(text.slice(cursor, match.index)));
    const token = match[0];
    if (token.startsWith('`')) {
      const code = document.createElement('code');
      code.textContent = token.slice(1, -1);
      nodes.push(code);
    } else if (token.startsWith('**')) {
      const strong = document.createElement('strong');
      strong.textContent = token.slice(2, -2);
      nodes.push(strong);
    } else {
      const parts = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(token);
      const href = safeMediaUrl(parts[2], 'link');
      if (href) {
        const link = document.createElement('a');
        link.href = href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.textContent = parts[1];
        nodes.push(link);
      } else nodes.push(document.createTextNode(parts[1]));
    }
    cursor = match.index + token.length;
  }
  if (cursor < text.length) nodes.push(document.createTextNode(text.slice(cursor)));
  return nodes;
}

/* 非安全上下文（如普通 http 部署）里 navigator.clipboard 不存在：围栏代码的
   Copy 按钮与 test.js 的消息级复制同一策略，clipboard API 不可用时退化为临时
   textarea + execCommand。复制失败只影响剪贴板，绝不让点击变成未捕获页面错误。 */
function copyText(text) {
  if (typeof navigator !== 'undefined' && navigator.clipboard &&
      typeof navigator.clipboard.writeText === 'function') {
    navigator.clipboard.writeText(text).catch(() => {});
    return;
  }
  const helper = document.createElement('textarea');
  helper.value = text;
  helper.setAttribute('readonly', '');
  helper.style.position = 'fixed';
  helper.style.top = '-1000px';
  helper.style.opacity = '0';
  try {
    document.body.append(helper);
    helper.select();
    document.execCommand('copy');
  } catch (error) {
    /* 无权限 / 无 execCommand：静默 */
  } finally {
    helper.remove();
  }
}

function renderMarkdown(source) {
  const root = document.createElement('div');
  root.className = 'tc-markdown';
  const lines = String(source || '').replace(/\r\n?/g, '\n').split('\n');
  const tableCells = line => line.trim().replace(/^\||\|$/g, '').split('|').map(cell => cell.trim());
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }
    const fence = /^```([\w-]*)\s*$/.exec(line);
    if (fence) {
      const body = [];
      index += 1;
      while (index < lines.length && lines[index] !== '```') body.push(lines[index++]);
      if (index < lines.length) index += 1;
      const wrap = document.createElement('div');
      wrap.className = 'tc-code';
      const copy = document.createElement('button');
      copy.type = 'button';
      copy.textContent = 'Copy';
      const pre = document.createElement('pre');
      const code = document.createElement('code');
      if (fence[1]) code.className = `language-${fence[1]}`;
      code.textContent = body.join('\n');
      copy.addEventListener('click', () => copyText(code.textContent));
      pre.append(code);
      wrap.append(copy, pre);
      root.append(wrap);
      continue;
    }
    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    if (heading) {
      const element = document.createElement(`h${heading[1].length}`);
      element.append(...inlineNodes(heading[2]));
      root.append(element);
      index += 1;
      continue;
    }
    if (line.includes('|') && index + 1 < lines.length &&
        /^\s*\|?\s*:?-{3,}/.test(lines[index + 1])) {
      const table = document.createElement('table');
      const thead = document.createElement('thead');
      const headRow = document.createElement('tr');
      tableCells(line).forEach(value => {
        const th = document.createElement('th');
        th.append(...inlineNodes(value));
        headRow.append(th);
      });
      thead.append(headRow);
      table.append(thead);
      const tbody = document.createElement('tbody');
      index += 2;
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
        const row = document.createElement('tr');
        tableCells(lines[index]).forEach(value => {
          const td = document.createElement('td');
          td.append(...inlineNodes(value));
          row.append(td);
        });
        tbody.append(row);
        index += 1;
      }
      table.append(tbody);
      root.append(table);
      continue;
    }
    const item = /^\s*([-*+]|\d+\.)\s+(.+)$/.exec(line);
    if (item) {
      const ordered = /^\d/.test(item[1]);
      const list = document.createElement(ordered ? 'ol' : 'ul');
      while (index < lines.length) {
        const match = /^\s*([-*+]|\d+\.)\s+(.+)$/.exec(lines[index]);
        if (!match || /^\d/.test(match[1]) !== ordered) break;
        const li = document.createElement('li');
        li.append(...inlineNodes(match[2]));
        list.append(li);
        index += 1;
      }
      root.append(list);
      continue;
    }
    const paragraph = document.createElement('p');
    paragraph.append(...inlineNodes(line));
    root.append(paragraph);
    index += 1;
  }
  return root;
}

export function renderParts(container, parts) {
  container.replaceChildren();
  parts.forEach(part => {
    if (part.type === 'text') {
      container.append(renderMarkdown(part.text));
      return;
    }
    if (part.type === 'unknown') {
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = 'Unsupported content';
      const pre = document.createElement('pre');
      pre.textContent = JSON.stringify(part.value, null, 2);
      details.append(summary, pre);
      container.append(details);
      return;
    }
    const url = safeMediaUrl(part.url, part.type);
    if (!url) return;
    let element;
    if (part.type === 'image') {
      element = document.createElement('img');
      element.className = 'tc-media-image';
      element.alt = part.name || 'Model image';
      element.src = url;
    } else if (part.type === 'audio' || part.type === 'video') {
      element = document.createElement(part.type);
      element.controls = true;
      element.src = url;
    } else {
      element = document.createElement('a');
      element.className = 'tc-file';
      element.download = part.name || 'attachment';
      element.href = url;
      element.textContent = part.name || 'attachment';
    }
    container.append(element);
  });
}
