/* 控制台功能回归 —— node tests/regress.test.js */
/* 功能回归：i18n 改造后逐项验证原有交互 */
const fs = require('fs');
let JSDOM;
try { ({ JSDOM } = require('jsdom')); }
catch { ({ JSDOM } = require(process.env.JSDOM_PATH ||
  '/Users/yuansu/.workbuddy/binaries/node/workspace/node_modules/jsdom')); }
const html = fs.readFileSync(require('path').join(__dirname, '..', 'console-mockup.html'), 'utf8');
const dom = new JSDOM(html, { runScripts: 'dangerously', pretendToBeVisual: true });
const { window } = dom, doc = window.document;
window.Element.prototype.scrollIntoView = function () {};
window.Element.prototype.getBoundingClientRect = function () {
  return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
};
const pend = ms => new Promise(r => window.setTimeout(r, ms || 520));
let pass = 0, fail = 0;
const t = (name, cond, extra) => { if (cond) { pass++; console.log('  ✓ ' + name); }
  else { fail++; console.log('  ✗ ' + name + (extra ? '  ' + extra : '')); } };
const $ = s => doc.querySelector(s);

(async () => {
  console.log('【分流树】');
  t('树渲染 8 个节点', doc.querySelectorAll('#treeWrap .node').length === 8,
    '实际 ' + doc.querySelectorAll('#treeWrap .node').length);
  t('统计行正确', /8 个节点 · \d+ 条分支/.test($('#statline').textContent), $('#statline').textContent);
  t('「或」是连接格而非 chip', doc.querySelectorAll('#treeWrap .br[data-k="b4-0"] .and').length === 1);
  const b40 = doc.querySelector('#treeWrap .br[data-k="b4-0"]');
  t('b4-0 无 undefined 残留', b40 && b40.textContent.indexOf('undefined') < 0, b40 && b40.textContent.slice(0, 60));
  t('去向为下拉框', doc.querySelectorAll('#treeWrap select').length > 0);

  console.log('【列表视图】');
  window.setView('list', doc.querySelectorAll('.tseg button')[1]);
  t('列表视图有表头', /节点（越靠上越优先）/.test($('#treeWrap').textContent));
  t('列表行数 = 分支数（17）', doc.querySelectorAll('#treeWrap tbody tr').length === 17,
    '实际 ' + doc.querySelectorAll('#treeWrap tbody tr').length);
  window.setView('tree', doc.querySelectorAll('.tseg button')[0]);

  console.log('【试一试】');
  await window.doTag(); await pend();
  t('标签卡 17 张', doc.querySelectorAll('#tags .tag').length === 17,
    '实际 ' + doc.querySelectorAll('#tags .tag').length);
  t('分组表头 4 个', doc.querySelectorAll('#tags .gh').length === 4);
  t('判定表有命中行', doc.querySelectorAll('#eval tr.hit').length === 1);
  t('判定表条件用 chip 渲染', doc.querySelectorAll('#eval .chip .q').length >= 4,
    'chip 数 ' + doc.querySelectorAll('#eval .chip .q').length);
  t('判定表无 undefined', $('#eval').textContent.indexOf('undefined') < 0);
  t('结论行显示去向', /qwen3-32b/.test($('#verdict').textContent));

  console.log('【新增节点】');
  window.openAddNode();
  t('四步表单出现', doc.querySelectorAll('#addBox .fstep').length === 4);
  window.pickTag('学科');
  t('切标签后出现取值 chip', doc.querySelectorAll('#addBox .vchips .pick').length >= 14,
    '实际 ' + doc.querySelectorAll('#addBox .vchips .pick').length);
  window.toggleVal('计算机'); window.toggleVal('数学');
  t('多选两个值', /当 学科 属于 计算机、数学 时/.test($('#addBox .sentence').textContent.trim()),
    $('#addBox .sentence').textContent.trim());
  window.setConf('比较确定');
  t('把握度门槛进入预览', /把握度/.test($('#addBox .sentence').textContent));
  window.pickPos(2);
  const nm = doc.getElementById('nd-name');
  nm.value = '测试节点'; nm.dispatchEvent(new window.Event('input', { bubbles: true }));
  t('名称进入预览', /测试节点/.test($('#nd-preview').textContent));
  const before = doc.querySelectorAll('#treeWrap .node').length;
  window.addAsDraft();
  t('草稿入树（节点 +1）', doc.querySelectorAll('#treeWrap .node').length === before + 1);
  t('草稿标记存在', !!$('#treeWrap .node.draft'));
  t('影响预估出现', /每天约 \d+ 条请求进入此节点/.test($('#impactBox').textContent));
  window.confirmAdd();
  t('确认后草稿标记消失', !$('#treeWrap .node.draft'));
  t('确认后影响面板关闭', $('#impactBox').textContent.trim() === '');

  console.log('【模型页】');
  window.go('models', doc.querySelectorAll('nav button')[3]);
  t('供应商表 2 行', doc.querySelectorAll('#provbody tr').length === 2);
  t('模型表 5 行', doc.querySelectorAll('#modbody tr').length === 5);
  t('阶梯 3 档（step>0）', doc.querySelectorAll('#ladderbox .step').length === 3,
    '实际 ' + doc.querySelectorAll('#ladderbox .step').length);
  t('SDK 差异表 6 行', doc.querySelectorAll('#prototable tbody tr').length === 6);
  window.openAddModel();
  t('接入表单出现', !!$('#addModelBox .addform'));
  window.mPickProto('anthropic');
  t('切协议后地址变化', /\/v1\/messages/.test($('#addModelBox').textContent));
  const m0 = doc.querySelectorAll('#modbody tr').length;
  window.mSave();                       // 缺名称，应被拦
  t('缺名称不放行', doc.querySelectorAll('#modbody tr').length === m0);
  const fill = (ph, val) => {
    const el = doc.querySelector('#addModelBox input[placeholder="' + ph + '"]');
    if (!el) throw new Error('找不到输入框 ' + ph);
    el.value = val; el.dispatchEvent(new window.Event('input', { bubbles: true }));
  };
  fill('例如：qwen3-8b', 'test-model');
  fill('例如：Qwen/Qwen2.5-8B-Instruct', 'test/up');
  window.mToggleCap('文字');
  t('能力已选', /文字/.test($('#addModelBox').textContent));
  window.mSave();
  t('合法后模型 +1', doc.querySelectorAll('#modbody tr').length === m0 + 1,
    '实际 ' + doc.querySelectorAll('#modbody tr').length);

  console.log('【设置页】');
  window.go('settings', doc.querySelectorAll('nav button')[4]);
  t('值档位 4 组', doc.querySelectorAll('#bandbox .band').length === 4);
  t('把握度一组', doc.querySelectorAll('#confbox .band').length === 1);
  const dists = i => [...doc.querySelectorAll('#bandbox .band')[i].querySelectorAll('.dist .seg i')].map(e => e.textContent);
  const cuts = i => [...doc.querySelectorAll('#bandbox .band')[i].querySelectorAll('.cuts .cut')].map(e => e.value);
  const d0 = dists(0), c0 = cuts(0);
  window.setCut(0, 0, '0.35');
  t('拖动分界后占比重算', dists(0).join() !== d0.join(), d0.join() + ' → ' + dists(0).join());
  const sum = dists(0).reduce((a, x) => a + parseInt(x, 10), 0);
  t('占比总和为 100 上下', Math.abs(sum - 100) <= 2, String(sum));
  window.setCut(0, 1, '0.10');          // 非递增，应被拦
  t('分界必须递增', cuts(0).map(Number).join() === '0.35,0.5,0.8', cuts(0).join());
  window.resetBand(0);
  t('恢复默认生效', dists(0).join() === d0.join() && cuts(0).join() === c0.join());

  console.log('【流量页】');
  window.go('traffic', doc.querySelectorAll('nav button')[2]);
  window.showReq(1);
  t('请求详情展开', $('#reqdetail').style.display === 'block');
  t('详情含拦截结论', /已拦截/.test($('#dt-eval').textContent));
  t('详情条件为 chip', doc.querySelectorAll('#dt-eval .chip .q').length >= 2);
  window.replay();
  t('重放给出结论', /与当时一致/.test($('#replaybox').textContent));
  window.showReq(2);
  t('会话沿用提示', /未重新打标签/.test($('#dt-tags').textContent));

  console.log('【体检 / 样例】');
  window.openCheckup();
  t('体检 6 条', doc.querySelectorAll('#checkup .find').length === 6);
  window.showSamples();
  t('样例两组表', /命中该分支的请求/.test($('#checkup').textContent) &&
     /最接近但未命中的请求/.test($('#checkup').textContent));
  window.closeCheckup();

  console.log('【语言切换稳定性】');
  for (let i = 0; i < 3; i++) { window.setLang('en'); await pend(30); window.setLang('zh'); await pend(30); }
  const bodyText = (() => {   // 排除 script 源码本身（词条表里就有英文）
    const w = doc.createTreeWalker(doc.body, window.NodeFilter.SHOW_TEXT, null), a = [];
    while (w.nextNode()) { const p = w.currentNode.parentElement;
      if (p && p.tagName !== 'SCRIPT' && p.tagName !== 'STYLE') a.push(w.currentNode.nodeValue); }
    return a.join(' ');
  })();
  t('反复切换后仍是中文', !/Route Tree/.test(bodyText));
  window.setLang('en'); await pend(30);
  t('EN 态下树仍可渲染', doc.querySelectorAll('#treeWrap .node').length >= 8);
  window.setView('list', doc.querySelectorAll('.tseg button')[1]);
  t('EN 态列表视图可用', doc.querySelectorAll('#treeWrap tbody tr').length >= 13);
  window.setView('tree', doc.querySelectorAll('.tseg button')[0]);
  window.setLang('zh'); await pend(30);
  t('切回后功能正常', doc.querySelectorAll('#treeWrap .node').length >= 8);

  console.log('\n通过 ' + pass + ' · 失败 ' + fail);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('ERR', e.stack); process.exit(1); });
