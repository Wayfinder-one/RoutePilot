/* console-v7 connected 模态 · 真 HTTP 端到端（M2 边界文档 §三.5 补齐）
   —— node tests/v7.connected.http.test.js

   与 tests/v7.connected.test.js 的区别：那份用 **fetch mock** 驱动接线层；本份起
   一个**真 `sr serve`**（子进程），jsdom 直连真端口——覆盖 mock 盖不住的缺口：
   - 服务端返回的规范键/中文显示词与我们内置表的「恰好同字串」不再是被依赖的巧合；
   - 引导序列七 GET 真发出并 200；树页渲染的是**真库**的 8 节点；
   - connected 态中英切换：API 驱动渲染的标签名（tag chips）在 EN 态变英文。

   跳过条件（不红）：找不到 python / jsdom 加载失败 —— 打印 SKIP 并以 0 退出。
   需要：仓库 .venv（或 SR_PYTHON 指定的 python）装了 fastapi/uvicorn/httpx。
*/
const fs = require('fs');
const os = require('os');
const net = require('net');
const path = require('path');
const { spawn } = require('child_process');

const REPO = path.join(__dirname, '..');
const FILE = process.env.TARGET || path.join(REPO, 'console-v7.html');
let JSDOM;
try { ({ JSDOM } = require('jsdom')); }
catch { ({ JSDOM } = require(process.env.JSDOM_PATH ||
  '/Users/yuansu/.workbuddy/binaries/node/workspace/node_modules/jsdom')); }

const PY = process.env.SR_PYTHON ||
  (fs.existsSync(path.join(REPO, '.venv', 'bin', 'python'))
    ? path.join(REPO, '.venv', 'bin', 'python') : 'python3');

let pass = 0, fail = 0;
const bad = [];
const t = (name, cond, detail) => { if (cond) { pass++; console.log('  ✓ ' + name); }
  else { fail++; bad.push(name + (detail ? ' → ' + detail : '')); console.log('  ✗ ' + name + (detail ? ' → ' + detail : '')); } };
const group = g => console.log('\n── ' + g);
const pend = ms => new Promise(r => setTimeout(r, ms || 200));

function freePort() {
  return new Promise((res, rej) => {
    const s = net.createServer();
    s.on('error', rej);
    s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); });
  });
}

async function runCLI(args) {
  return new Promise(res => {
    const p = spawn(PY, ['-m', 'sr', ...args], { cwd: REPO, env: subEnv() });
    let out = '', err = '';
    p.stdout.on('data', d => out += d); p.stderr.on('data', d => err += d);
    p.on('close', code => res({ code, out, err }));
  });
}

function subEnv() {
  const env = Object.assign({}, process.env);
  env.PYTHONPATH = REPO + path.delimiter + (env.PYTHONPATH || '');
  env.LOCAL_OPENAI_API_KEY = env.LOCAL_OPENAI_API_KEY || 'sk-test-local';
  env.ANTHROPIC_API_KEY = env.ANTHROPIC_API_KEY || 'sk-test-anthropic';
  return env;
}

async function waitReady(port, proc, log) {
  const url = `http://127.0.0.1:${port}/admin/settings`;
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    if (proc.exitCode !== null) throw new Error('server exited: ' + proc.exitCode + '\n' + log());
    try { const r = await fetch(url); if (r.ok) return; } catch (e) { /* retry */ }
    await pend(150);
  }
  throw new Error('server not ready in 15s\n' + log());
}

(async () => {
  console.log('══ console-v7 connected 真 HTTP 验证 ══\n文件：' + FILE + '\npython：' + PY);

  const port = await freePort();
  const db = path.join(os.tmpdir(), `sr-http-${process.pid}-${port}.db`);
  const init = await runCLI(['init', '--db', db]);
  if (init.code !== 0) {
    console.log('SKIP：sr init 失败（缺依赖？）\n' + init.err); process.exit(0);
  }
  let serverLog = '';
  const server = spawn(PY, ['-m', 'sr', 'serve', '--db', db, '--port', String(port),
    '--host', '127.0.0.1'], { cwd: REPO, env: subEnv() });
  server.stdout.on('data', d => serverLog += d);
  server.stderr.on('data', d => serverLog += d);
  const log = () => serverLog.split('\n').slice(-15).join('\n');
  const cleanup = async () => {
    try { server.kill('SIGTERM'); } catch (e) {}
    await pend(200);
    try { fs.unlinkSync(db); } catch (e) {}
    try { fs.unlinkSync(db + '-wal'); } catch (e) {}
    try { fs.unlinkSync(db + '-shm'); } catch (e) {}
  };

  try {
    await waitReady(port, server, log);

    const base = `http://127.0.0.1:${port}/`;
    const html = fs.readFileSync(FILE, 'utf8');
    const dom = new JSDOM(html, {
      runScripts: 'dangerously', pretendToBeVisual: true, url: base,
      beforeParse(window) {
        // jsdom 无 fetch：注入 Node 全局 fetch，并把相对路径解析到真端口
        window.fetch = (u, o) => globalThis.fetch(new URL(String(u), base).href, o);
        window.Element.prototype.scrollIntoView = function () {};
        window.Element.prototype.getBoundingClientRect = function () {
          return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
        };
      },
    });
    const w = dom.window, doc = w.document;
    const until = async (f, ms = 8000, what = 'cond') => {
      const t0 = Date.now();
      while (!f()) { if (Date.now() - t0 > ms) throw new Error('超时：' + what); await pend(60); }
    };

    group('真 HTTP · 引导与树渲染');
    await until(() => w.SR_MODE === 'connected', 8000, 'connected 模态');
    t('真服务端 + http 打开 → connected 模态', w.SR_MODE === 'connected', w.SR_MODE);
    await until(() => doc.querySelectorAll('.tnode').length === 8, 8000, '树 8 节点');
    t('树页渲染真库 8 节点（非内置演示数据）', doc.querySelectorAll('.tnode').length === 8);
    t('非演示模态（顶栏无「演示模式」）', !/演示模式/.test(doc.querySelector('.strip').textContent));

    group('真 HTTP · connected 态中英切换');
    const enTxt = doc.querySelector('.rk-t').textContent;
    t('中文态导航首项为中文', /[\u4e00-\u9fff]/.test(enTxt), enTxt);
    w.setLang('en');
    await pend(300);
    const navs = Array.from(doc.querySelectorAll('.rk-t')).map(e => e.textContent.trim());
    t('EN 态导航标签全英文（无 CJK 残留）', navs.every(x => !/[\u4e00-\u9fff]/.test(x)),
      navs.join(' | '));
    const chips = Array.from(doc.querySelectorAll('#treeWrap .chip .k')).map(e => e.textContent.trim());
    t('EN 态树分支标签名（tag chips）变英文（API 中文经内置表翻译）',
      chips.length > 0 && chips.every(x => !/[\u4e00-\u9fff]/.test(x)), chips.slice(0, 6).join(' | '));
    w.setLang('zh');
    await pend(200);
    t('切回中文后导航还原', /[\u4e00-\u9fff]/.test(doc.querySelector('.rk-t').textContent));

    console.log('\n══ 结果 ══');
    console.log('通过 ' + pass + ' · 失败 ' + fail);
    await cleanup();
    if (fail) { console.log('\n失败项：'); bad.forEach(b => console.log('  · ' + b)); process.exit(1); }
    console.log('全部通过 ✅');
    process.exit(0);
  } catch (e) {
    console.error('异常：', e && e.message ? e.message : e);
    await cleanup();
    process.exit(1);
  }
})();
