/* Shared top bar: brand, page nav, signed-in user dropdown, language toggle.

   The top-right user control ("头像 + 邮箱 + 角色") and the admin-only 用户管理
   item come from GET /admin/me, fetched once per page. Both start hidden so a
   viewer never sees admin UI flash in; clicking the user control opens a
   dropdown with 用户管理 (admin) and 退出. An older backend without /admin/me
   simply leaves the bar in its previous form (no user control).

   ES module: the language buttons are bound here instead of using inline
   onclick handlers, because setLang is no longer a page global. */
import { api } from './api.js';
import { LANG, onLangChange, setLang, t } from './i18n.js';

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
       <button class="user-trigger" id="userTrigger" aria-haspopup="true" aria-expanded="false">
         <span class="u-ico" aria-hidden="true">👤</span>
         <span class="pill user-email" id="userEmail"></span>
         <span class="pill" id="userRole"></span>
         <span class="u-caret" aria-hidden="true">▾</span>
       </button>
       <div class="usermenu" id="userMenu" hidden>
         <a href="users.html" id="menuUsers" class="${page === 'users' ? 'on' : ''}"
            data-i18n="nav.users" hidden></a>
         <button id="logoutBtn" data-i18n="nav.logout"></button>
       </div>
     </span>
     <span class="langtog">
       <button type="button" data-lang="zh">中文</button>
       <button type="button" data-lang="en">EN</button>
     </span>`;
  document.body.prepend(bar);
  bar.querySelectorAll('.langtog button').forEach(button => {
    button.addEventListener('click', () => setLang(button.dataset.lang));
  });
  setLang(LANG);
  /* Narrow view scrolls the nav sideways; keep the current page in view
     without moving the document itself (scrollIntoView can move both). */
  requestAnimationFrame(() => {
    const nav = bar.querySelector('nav');
    const current = nav && nav.querySelector('a.on');
    if (!nav || !current || nav.scrollWidth <= nav.clientWidth) return;
    const box = nav.getBoundingClientRect();
    nav.scrollLeft += current.getBoundingClientRect().left - box.left - 10;
  });

  /* ── current user (async; everything stays hidden until it is known) ── */
  let me = null;

  function paintUser() {
    if (!me) return;
    const box = document.getElementById('userBox');
    const menuUsers = document.getElementById('menuUsers');
    const email = document.getElementById('userEmail');
    const role = document.getElementById('userRole');
    if (!box || !email || !role) return;
    const name = me.email || t('nav.service');
    email.textContent = name;
    email.title = name;
    role.textContent = me.role === 'admin' ? t('users.roleAdmin') : t('users.roleViewer');
    role.classList.toggle('indigo', me.role === 'admin');
    box.hidden = false;
    if (menuUsers) menuUsers.hidden = me.role !== 'admin';
  }

  function toggleMenu(open) {
    const menu = document.getElementById('userMenu');
    const trigger = document.getElementById('userTrigger');
    if (!menu || !trigger) return;
    const show = open == null ? menu.hidden : open;
    menu.hidden = !show;
    trigger.setAttribute('aria-expanded', show ? 'true' : 'false');
  }

  function initUser() {
    const trigger = document.getElementById('userTrigger');
    if (trigger) trigger.addEventListener('click', e => {
      e.stopPropagation();
      toggleMenu();
    });
    document.addEventListener('click', e => {
      if (!e.target.closest('.userbox')) toggleMenu(false);
    });
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape') toggleMenu(false);
    });
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

  onLangChange(paintUser);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initUser);
  else initUser();
