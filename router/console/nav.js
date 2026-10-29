/* Shared top bar: brand, page nav, signed-in user chip, language toggle.

   The user chip ("邮箱 + 角色 + 退出") and the admin-only 用户管理 item come
   from GET /admin/me, fetched once per page. Both start hidden so a viewer
   never sees admin UI flash in; an older backend without /admin/me simply
   leaves the bar in its previous form (no chip, no users item). The fetch is
   deferred to DOMContentLoaded because nav.js loads *before* api.js. */
(function () {
  const page = document.body.dataset.page;
  const pages = ['overview', 'routing', 'logs', 'jev', 'models'];
  const bar = document.createElement('header');
  bar.className = 'top';
  bar.innerHTML =
    `<a class="brand" href="index.html"><span class="mark">SR</span><span data-i18n="app.title"></span></a>
     <nav>${pages.map(p =>
       `<a href="${p === 'overview' ? 'index' : p}.html"
           class="${p === page ? 'on' : ''}" data-i18n="nav.${p}"></a>`).join('')}
       <a href="users.html" id="navUsers" class="${page === 'users' ? 'on' : ''}"
          data-i18n="nav.users" hidden></a></nav>
     <span class="spacer"></span>
     <span class="userbox" id="userBox" hidden>
       <span class="pill user-email" id="userEmail"></span>
       <span class="pill" id="userRole"></span>
       <button class="btn ghost sm" id="logoutBtn" data-i18n="nav.logout"></button>
     </span>
     <span class="langtog">
       <button data-lang="zh" onclick="setLang('zh')">中文</button>
       <button data-lang="en" onclick="setLang('en')">EN</button>
     </span>`;
  document.body.prepend(bar);
  setLang(LANG);

  /* ── current user (async; everything stays hidden until it is known) ── */
  let me = null;

  function paintUser() {
    if (!me) return;
    const box = document.getElementById('userBox');
    const link = document.getElementById('navUsers');
    const email = document.getElementById('userEmail');
    const role = document.getElementById('userRole');
    if (!box || !email || !role) return;
    const name = me.email || t('nav.service');
    email.textContent = name;
    email.title = name;
    role.textContent = me.role === 'admin' ? t('users.roleAdmin') : t('users.roleViewer');
    role.classList.toggle('indigo', me.role === 'admin');
    box.hidden = false;
    if (link) link.hidden = me.role !== 'admin';
  }

  function initUser() {
    const logout = document.getElementById('logoutBtn');
    if (logout) logout.addEventListener('click', async () => {
      logout.disabled = true;
      try { await api.logout(); } catch (e) { api.setToken(''); }
      window.location.href = 'login.html';
    });
    api.me().then(data => { me = data; paintUser(); }).catch(() => {
      /* 401 → api.js already bounced to login.html; 404 → backend older than the
         user-management release, leave the bar as it was. */
    });
  }

  if (typeof onLangChange === 'function') onLangChange(paintUser);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initUser);
  else initUser();
})();
