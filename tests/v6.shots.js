/* 真实浏览器视觉核对 —— node tests/v6.shots.js
   jsdom 只能验结构与逻辑；配色、层次、动效落点、对比度必须用真浏览器看。
   本脚本同时收集 console 报错，任何 JS 异常都会让检查失败。*/
const path = require('path');
const { chromium } = require('playwright-core');

const EXE = process.env.CHROME ||
  '/Users/yuansu/Library/Caches/ms-playwright/chromium-1234/chrome-mac-arm64/' +
  'Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing';
const FILE = 'file://' + path.join(__dirname, '..', 'console-v6.html');
const OUT = process.env.SHOT_DIR || '/tmp/v6shots';
require('fs').mkdirSync(OUT, { recursive: true });

(async () => {
  const browser = await chromium.launch({ executablePath: EXE });
  const page = await browser.newPage({ viewport: { width: 1440, height: 980 }, deviceScaleFactor: 2 });

  const errors = [];
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));

  await page.goto(FILE, { waitUntil: 'networkidle' });
  await page.waitForTimeout(900);

  const shot = async (name, opts = {}) => {
    await page.waitForTimeout(420);
    await page.screenshot({ path: `${OUT}/${name}.png`, ...opts });
    console.log('  · ' + name + '.png');
  };

  console.log('══ 视觉核对 ══');

  // 01 试一试：先跑一次，让信号轨通电
  await page.click('button[onclick="doTag()"]');
  await page.waitForTimeout(1200);
  await shot('01-tryout');

  // 信号轨特写 —— 本设计的主角
  const rail = await page.locator('#p-try .panel').nth(2);
  await rail.screenshot({ path: `${OUT}/02-signal-rail.png` });
  console.log('  · 02-signal-rail.png');

  // 02 分流树 + 试算
  await page.click('.rk[onclick="go(\'tree\')"]');
  await page.waitForTimeout(600);
  await page.selectOption('#sampleSel', '0');
  await page.waitForTimeout(600);
  await shot('03-tree-dryrun');

  await page.selectOption('#sampleSel', '1');
  await page.waitForTimeout(500);
  await page.click('button[onclick="openAddNode()"]');
  await page.waitForTimeout(700);
  await shot('04-add-node', { fullPage: true });
  await page.click('button[onclick="closeAdd()"]');

  // 列表视图
  await page.click('button[onclick="setView(\'list\')"]');
  await page.waitForTimeout(500);
  await shot('05-tree-list');
  await page.click('button[onclick="setView(\'tree\')"]');

  // 03 流量
  await page.click('.rk[onclick="go(\'traffic\')"]');
  await page.waitForTimeout(500);
  await page.click('tr[onclick="showReq(0)"]');
  await page.waitForTimeout(700);
  await shot('06-traffic-detail', { fullPage: true });

  // 04 模型
  await page.click('.rk[onclick="go(\'models\')"]');
  await page.waitForTimeout(600);
  await shot('07-models', { fullPage: true });

  // 05 设置
  await page.click('.rk[onclick="go(\'settings\')"]');
  await page.waitForTimeout(600);
  await shot('08-settings', { fullPage: true });

  // 英文态
  await page.click('#lg-en');
  await page.waitForTimeout(900);
  await page.click('.rk[onclick="go(\'try\')"]');
  await page.waitForTimeout(600);
  await page.click('button[onclick="doTag()"]');
  await page.waitForTimeout(1200);
  await shot('09-tryout-en');

  await page.click('#lg-zh');
  await page.waitForTimeout(700);

  // 移动端
  const m = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  await m.goto(FILE, { waitUntil: 'networkidle' });
  await m.waitForTimeout(800);
  await m.click('button[onclick="doTag()"]');
  await m.waitForTimeout(1100);
  await m.screenshot({ path: `${OUT}/10-mobile.png` });
  console.log('  · 10-mobile.png');

  // 横向溢出检查：任何断点都不该出现横向滚动
  for (const [w, h, label] of [[1440, 980, '1440'], [768, 1024, '768'], [390, 844, '390']]) {
    await page.setViewportSize({ width: w, height: h });
    await page.waitForTimeout(320);
    const over = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
    console.log(`  ${label}px 横向溢出：${over}px` + (over > 1 ? '  ✗' : '  ✓'));
    if (over > 1) errors.push(`overflow at ${w}px: ${over}px`);
  }

  // ── 主题切换：两套主题都要过同一组检查 ──
  for (const theme of ['light', 'dark']) {
    await page.click(theme === 'dark' ? '#th-dark' : '#th-light');
    await page.waitForTimeout(500);
    const applied = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
    if (applied !== theme) errors.push(`theme not applied: want ${theme}, got ${applied}`);
    // 深色/浅色下各截全页图
    await page.screenshot({ path: `${OUT}/theme-${theme}-tryout.png`, fullPage: true });
    console.log(`  · theme-${theme}-tryout.png`);
  }
  await page.click('#th-light');
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${OUT}/theme-light-tree.png`, fullPage: true });
  await page.click('#th-dark');
  await page.waitForTimeout(400);

  // 粘字检测：中英切换把一句话拆到多个文本节点，节点边界没有空格，
  // 英文单词会直接粘在一起（Model tagsproduced）。这类问题只有真浏览器看得出来。
  const glue = await page.evaluate(() => {
    const bad = [];
    document.querySelectorAll('body *').forEach(el => {
      if (el.children.length) return;
      if (/^(SCRIPT|STYLE|NOSCRIPT)$/.test(el.tagName)) return;
      if (el.closest('script, style')) return;
      const t = (el.textContent || '').trim();
      if (!t) return;
      if (/[A-Za-z]{2,}[\u4e00-\u9fff]|[\u4e00-\u9fff][A-Za-z]{2,}/.test(t)) bad.push(t.slice(0, 46));
      // 英文里出现小写紧跟大写单词（tagsProduced）也算粘连
      if (/[a-z]{3}[A-Z][a-z]{3}/.test(t)) bad.push(t.slice(0, 46));
    });
    return [...new Set(bad)];
  });
  console.log('\n  中英混排粘字检测：' + (glue.length ? '' : '（无）'));
  glue.slice(0, 8).forEach(g => { console.log('   ✗ ' + g); errors.push('glue text: ' + g); });

  // 对比度抽查：两套主题各跑一遍。
  // 这一点比截图重要——浅色主题的第一版把琥珀直接照搬过来，
  // 亮琥珀压在纸白上只有约 2:1，肉眼"看得出"但实测不合格。
  const contrastAll = {};
  for (const theme of ['dark', 'light']) {
    await page.evaluate(t => document.documentElement.setAttribute('data-theme', t), theme);
    await page.waitForTimeout(320);
    contrastAll[theme] = await page.evaluate(() => {
    const lum = c => { const [r, g, b] = c.map(v => { v /= 255; return v <= .03928 ? v/12.92 : Math.pow((v+.055)/1.055, 2.4); });
                        return .2126*r + .7152*g + .0722*b; };
    const parse = s => (s.match(/\d+(\.\d+)?/g) || []).slice(0,3).map(Number);
    const bgOf = el => { let n = el; while(n){ const c = getComputedStyle(n).backgroundColor;
                          if(c && !/rgba\(0, 0, 0, 0\)|transparent/.test(c)) return parse(c); n = n.parentElement; } return [11,10,8]; };
    const ratio = (el) => { const fg = parse(getComputedStyle(el).color), bg = bgOf(el);
                            const L1 = lum(fg), L2 = lum(bg); return (Math.max(L1,L2)+.05)/(Math.min(L1,L2)+.05); };
    const out = [];
    [['.lede','正文导语'], ['.gauge .gv','读数值'], ['.gauge .gk','读数标签'],
     ['.tele-i dd','遥测读数'], ['.note','注释'], ['.panel-hd .sub','面板副标'],
     ['.stn-why','站点理由'], ['.pg-no','章节号'], ['.stn.hit .snm','命中站点名'],
     ['.stn.hit .stn-why','命中理由'], ['.verdict .pill','判定药丸'],
     ['.tbl th','表头'], ['.kv .k','键值表键'], ['.dist .s2','分档条深档'],
     ['.sbr .live','通电标记'], ['.btn.pri','主按钮'], ['.chip .v','芯片值']].forEach(([sel, name]) => {
      const el = document.querySelector(sel); if (el) out.push({ name, r: +ratio(el).toFixed(2) });
    });
    return out;
    });
  }
  for (const theme of ['dark', 'light']) {
    console.log(`\n  对比度抽查 · ${theme === 'dark' ? '深色' : '浅色'}（正文需 ≥ 4.5:1，大字需 ≥ 3:1）`);
    contrastAll[theme].forEach(c => {
      const need = /章节号|读数值|命中站点名/.test(c.name) ? 3 : 4.5;
      const good = c.r >= need;
      console.log(`   ${good ? '✓' : '✗'} ${c.name.padEnd(12)} ${c.r}:1  (需 ≥ ${need})`);
      if (!good) errors.push(`contrast ${theme}/${c.name} = ${c.r}`);
    });
  }
  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));

  // 触摸目标尺寸
  const small = await page.evaluate(() => {
    const out = [];
    document.querySelectorAll('.rk, .btn, .pk, .dip button, .seg button').forEach(el => {
      const r = el.getBoundingClientRect();
      if (r.width && r.height && (r.width < 44 || r.height < 44)) {
        const cls = (el.className || '').toString().split(' ')[0];
        out.push(`${cls} ${Math.round(r.width)}×${Math.round(r.height)}`);
      }
    });
    return [...new Set(out)];
  });
  console.log('\n  小于 44×44 的可点区域（紧凑控件可接受，主导航必须达标）：');
  console.log('   ', small.length ? small.join(' | ') : '（无）');
  const navSmall = small.filter(s => s.startsWith('rk'));
  if (navSmall.length) errors.push('nav targets too small: ' + navSmall.join(','));

  console.log('\n══ 结果 ══');
  if (errors.length) { console.log('问题：'); errors.forEach(e => console.log('  ✗ ' + e)); }
  else console.log('无 JS 报错 · 无横向溢出 · 对比度达标 · 导航触摸目标达标 ✅');
  console.log('截图目录：' + OUT);
  await browser.close();
  process.exit(errors.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });