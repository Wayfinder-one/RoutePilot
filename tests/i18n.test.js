/* 中英文切换端到端检查 —— node tests/i18n.test.js */
/* i18n 端到端检查 v2：覆盖更多状态，并在 EN 态下继续操作 */
const fs = require('fs');
let JSDOM;
try { ({ JSDOM } = require('jsdom')); }
catch { ({ JSDOM } = require(process.env.JSDOM_PATH ||
  '/Users/yuansu/.workbuddy/binaries/node/workspace/node_modules/jsdom')); }
const __pick = f => [require('path').join(__dirname, '..', f),
  require('path').join(__dirname, '..', 'docs', 'prototypes', f)].find(p => fs.existsSync(p));
const html = fs.readFileSync(__pick('console-mockup.html'), 'utf8');
const dom = new JSDOM(html, { runScripts: 'dangerously', pretendToBeVisual: true });
const { window } = dom, doc = window.document;
window.Element.prototype.scrollIntoView = function () {};
window.Element.prototype.getBoundingClientRect = function () {
  return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
};

const CJK = /[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]/;
// 白名单只放「裸标识符」与文档文件名，不放带中文的组合串——否则会把真实漏翻遮掉
const ALLOW = [/^[A-Za-z][\w.\/\-]*$/, /Smart-Router-设计方案\.md/]
const nav = () => doc.querySelectorAll('nav button');
const pend = () => new Promise(r => window.setTimeout(r, 550));

function texts(root) {
  const w = doc.createTreeWalker(root, window.NodeFilter.SHOW_TEXT, null), out = [];
  while (w.nextNode()) {
    const n = w.currentNode, p = n.parentElement;
    if (!p || p.tagName === 'SCRIPT' || p.tagName === 'STYLE') continue;
    if (!n.nodeValue || !n.nodeValue.trim()) continue;
    out.push(n.nodeValue.trim());
  }
  return out;
}
function leaks() {
  const bad = [];
  texts(doc.body).forEach(t => {
    if (!CJK.test(t)) return;
    if (ALLOW.some(re => re.test(t))) return;
    bad.push(t);
  });
  doc.querySelectorAll('[placeholder],[title],[label]').forEach(el => {
    ['placeholder', 'title', 'label'].forEach(a => {
      const v = el.getAttribute(a);
      if (v && CJK.test(v) && !ALLOW.some(re => re.test(v))) bad.push('[' + a + '] ' + v);
    });
  });
  return [...new Set(bad)];
}

let fails = 0;
function check(name, bad, limit = 12) {
  if (bad.length === 0) { console.log('  ✓ ' + name); return; }
  fails++;
  console.log('  ✗ ' + name + ' — ' + bad.length + ' 条');
  bad.slice(0, limit).forEach(x => console.log('      · ' + x));
}

(async () => {
  // ── 1. 中文态：展开所有面板与交互态
  window.doTag(); await pend();
  window.showReq(0); window.showReq(1); window.showReq(2); window.showReq(3); window.showReq(4); window.showReq(5);
  window.replay();
  window.showSamples(); window.openCheckup();
  window.tryOne('0'); window.tryOne('1'); window.tryOne('2');
  window.setView('list', doc.querySelectorAll('.tseg button')[1]);
  window.setView('tree', doc.querySelectorAll('.tseg button')[0]);
  window.openAddNode();
  const TAG_ORDER = Object.keys(window.TAG_VALUES || {});
  for (const t of ['学科','难度','可疑','长度','关键词','要什么','语言','结构','轮次','身份','时段','事件','上轮反馈','重复提问','需核实','相似度','敏感信息']) {
    try { window.pickTag(t); } catch (e) { console.log('pickTag(' + t + ') 异常: ' + e.message); }
  }
  try { window.pickTag('学科'); window.toggleVal('计算机'); window.setCmp('至多'); window.setConf('比较确定'); window.startCustom(); window.commitCustom(); window.pickPos(2); window.pickModel('qwen3-32b'); } catch (e) { console.log('表单交互异常: ' + e.message); }
  window.addAsDraft(); window.cancelAdd();
  window.openAddModel();
  try { window.mPickProto('anthropic'); window.mPickProv('本地'); window.mToggleCap('文字'); window.mTest(); } catch (e) { console.log('模型表单异常: ' + e.message); }
  window.setCut && window.setCut(0, 0, '0.25'); window.setConfCut && window.setConfCut(0, '0.35');
  window.openPop({ stopPropagation() {}, currentTarget: doc.querySelector('.br .tools .ico:last-child') || doc.body }, 'b1-0');
  window.foldNode && doc.querySelector('.nhead .tools .ico') && window.foldNode(doc.querySelector('.nhead .tools .ico'));
  window.quick && window.quick(doc.querySelector('.toolbar .btn.sm'));
  const zhTexts = texts(doc.body);
  console.log('中文态文本节点：' + zhTexts.length);
  // 切 EN 前抓取中文快照（按节点身份）
  const survivors = [];
  {
    const w = doc.createTreeWalker(doc.body, window.NodeFilter.SHOW_TEXT, null);
    while (w.nextNode()) {
      const n = w.currentNode, p = n.parentElement;
      if (!p || p.tagName === 'SCRIPT' || p.tagName === 'STYLE') continue;
      if (n.nodeValue && n.nodeValue.trim()) survivors.push([n, n.nodeValue]);
    }
  }

  // ── 2. 切 EN
  window.setLang('en'); await pend();
  check('切换 EN 后无残留中文', leaks());

  // ── 3. EN 态下继续操作（新建内容也必须直接是英文）
  window.doTag(); await pend();
  window.openAddNode(); for (const t of ['学科','难度','可疑','关键词']) window.pickTag(t);
  window.pickTag('学科'); window.addAsDraft();
  window.openAddModel(); try { window.mPickProto('openai'); window.mTest(); } catch (e) {}
  window.showReq(1); window.replay(); window.showSamples(); window.openCheckup(); window.tryOne('1');
  window.setCut && window.setCut(1, 0, '1.0'); window.setConfCut && window.setConfCut(1, '0.7');
  check('EN 态下继续操作仍无中文', leaks());

  // ── 4. 切回中文：按「节点身份」比对，只有跨两次切换仍存活的节点才要求还原
  window.setLang('zh'); await pend();
  const path = e => { const a = []; while (e && e !== doc.body) { a.unshift(e.tagName + '.' + String(e.className || '').split(' ')[0]); e = e.parentElement; } return a.join('>'); };
  const lost = survivors.filter(([n, v]) => doc.body.contains(n) && n.nodeValue !== v)
    .map(([n, v]) => JSON.stringify(v.slice(0, 22)) + ' → ' + JSON.stringify(String(n.nodeValue).slice(0, 22)) +
                     '  __zh=' + JSON.stringify(String(n.__zh || '').slice(0, 22)) + '  @' + path(n.parentElement));
  check('切回中文后原文完整还原', lost);
  const enLeft = texts(doc.body).filter(t => /\b(Step \d|Fallback |Subject is|Route Tree|Confidence gate)\b/.test(t));
  check('切回中文后无英文残留', enLeft);

  console.log(fails === 0 ? '\n全部通过 ✅' : '\n失败 ' + fails + ' 项 ❌');
  process.exit(fails === 0 ? 0 : 1);
})().catch(e => { console.error('ERR', e.message); process.exit(1); });
