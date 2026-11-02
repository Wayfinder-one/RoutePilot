/* Login page (email + password -> POST /admin/login).

   - The session token is stored by api.login() in localStorage (sr_admin_token);
     ?token=<static token> is still honoured by api.js and bounces straight on.
   - A signed-in visitor (GET /admin/me 200) never sees the form: it redirects
     to `next` or index.html.
   - `next` accepts only same-origin absolute paths ("/console/users.html"),
     never protocol-relative ("//evil.example") or backslash tricks: this is
     the post-401 bounce target and must not become an open redirect.
   - login.html does not load nav.js: the main nav is meaningless before auth. */
setLang(LANG);

(function () {
  const $ = s => document.querySelector(s);
  const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

  /* 站内绝对路径白名单；任何可疑值一律回首页。 */
  function nextTarget() {
    const raw = String(new URLSearchParams(window.location.search).get('next') || '').trim();
    if (!raw || raw[0] !== '/' || raw[1] === '/' || raw.includes('\\')) return 'index.html';
    try {
      const u = new URL(raw, window.location.href);
      if (u.origin !== window.location.origin) return 'index.html';
      return u.pathname + u.search + u.hash;
    } catch (e) { return 'index.html'; }
  }
  function go() { window.location.replace(nextTarget()); }

  /* Error bar state survives a language toggle (repainted by onLangChange). */
  let notice = null;            // {text, warn} | {key, vars, warn}
  function paintNotice() {
    const el = $('#loginError');
    if (!el) return;
    let text = '';
    if (notice) text = notice.key ? tf(notice.key, notice.vars) : notice.text;
    el.textContent = text;
    el.hidden = !text;
    el.classList.toggle('warn', !!(notice && notice.warn));
  }
  function showErr(text, warn) { notice = text ? { text, warn: !!warn } : null; paintNotice(); }
  function showErrKey(key, warn, vars) { notice = { key, vars, warn: !!warn }; paintNotice(); }

  function paintTitle() { document.title = t('login.title') + ' · RoutePilot'; }

  async function boot() {
    /* Signed in (session or ?token= static token) -> go back to the target page. */
    try {
      await api.me();
      go();
      return;
    } catch (e) {
      /* 401 = not signed in (api.js clears the stale token, no bounce on this
         page); 404 = backend older than user management: show the form anyway. */
    }
    const form = $('#loginForm');
    if (form) form.hidden = false;
    if (new URLSearchParams(window.location.search).get('expired')) {
      showErrKey('login.sessionExpired', true);
    }
    const email = $('#loginEmail');
    if (email) email.focus();
  }

  const form = $('#loginForm');
  if (form) form.addEventListener('submit', async ev => {
    ev.preventDefault();
    const emailEl = $('#loginEmail');
    const pwdEl = $('#loginPassword');
    const btn = $('#loginSubmit');
    const email = (emailEl.value || '').trim();
    const pwd = pwdEl.value || '';
    if (!email || !pwd) { showErrKey('login.empty'); return; }
    if (!EMAIL_RE.test(email)) { showErrKey('login.emailInvalid'); return; }
    btn.disabled = true;
    btn.textContent = t('login.signingIn');
    try {
      await api.login(email, pwd);
      go();
    } catch (e) {
      const status = e && (e.status || Number(e.message));
      if (status === 401) showErrKey('login.invalid');
      else showErrKey('login.failed', false, { err: apiErrText(e) });
      btn.disabled = false;
      btn.textContent = t('login.submit');
      pwdEl.select();
    }
  });

  onLangChange(() => { paintTitle(); paintNotice(); });
  paintTitle();
  boot();
})();
