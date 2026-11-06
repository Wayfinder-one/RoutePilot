/* Users management page (admin only, 2026-10-26).

   Contract (frozen, see docs/superpowers/plans/2026-10-26-user-management.md):
     GET    /admin/me                      -> {id,email,role,...}
     GET    /admin/users                   -> {users:[{id,email,role,status,created_at,last_login_at}]}
     POST   /admin/users                   -> 201 {user} | 409 email already exists | 422
     PUT    /admin/users/{id}              -> 200 {user} | 404/409/422/400 last admin
     POST   /admin/users/{id}/password     -> 200 {ok:true} | 404/422
     DELETE /admin/users/{id}              -> 200 {ok:true} | 400 cannot delete self / last admin

   The server enforces every guard; this page computes the same guards from the
   loaded list only to disable buttons early (self / last active admin). Every
   user-supplied value is inserted through esc(). */
import { api, apiErrText, esc, toast } from './api.js';
import { onLangChange, t, tf } from './i18n.js';
import './nav.js';

const $ = s => document.querySelector(s);
const $$ = (s, root = document) => Array.from((root || document).querySelectorAll(s));
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const MIN_PWD = 8;

const STATE = {
  me: null,
  users: [],
  modal: null,          // {mode:'create'} | {mode:'edit', id}
  pwd: null,            // {id, email}
};

/* ── small helpers ───────────────────────────────────────────────────── */
function errTextFor(e) {
  const raw = apiErrText(e);
  const low = raw.toLowerCase();
  if (low.includes('already exists')) return t('users.emailTaken');
  if (low.includes('last active admin')) return t('users.lastAdmin');
  if (low.includes('not found')) return t('users.notFound');
  if (low.includes('yourself') || low.includes('your own') || low.includes('self')) return t('users.selfDeleteHint');
  if (low.includes('email') && (low.includes('valid') || low.includes('invalid'))) return t('users.emailInvalid');
  if (low.includes('password')) return t('users.pwdShort');
  return raw;
}
function fmtStamp(ts) {
  if (!ts) return t('users.never');
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return String(ts).slice(0, 16).replace('T', ' ');
  const p2 = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ` +
    `${p2(d.getHours())}:${p2(d.getMinutes())}`;
}
const roleLabel = r => r === 'admin' ? t('users.roleAdmin')
  : r === 'viewer' ? t('users.roleViewer') : String(r);
const statusLabel = s => s === 'active' ? t('users.active')
  : s === 'disabled' ? t('users.disabled') : String(s);
const userById = id => STATE.users.find(u => u.id === id) || null;
const isSelf = u => !!(STATE.me && STATE.me.id && u && u.id === STATE.me.id);
const activeAdminCount = () =>
  STATE.users.filter(u => u.role === 'admin' && u.status === 'active').length;
/* 最后一个启用中的 admin 不能被删除/降级/停用（服务端同样拒绝）。 */
const isLastActiveAdmin = u =>
  !!(u && u.role === 'admin' && u.status === 'active' && activeAdminCount() <= 1);

/* ── modal plumbing ──────────────────────────────────────────────────── */
function openModal(id) {
  const el = $('#' + id);
  if (!el) return;
  el.hidden = false;
  document.body.classList.add('modal-open');
}
function closeModal(id) {
  const el = $('#' + id);
  if (!el) return;
  el.hidden = true;
  if ($$('.modal').every(m => m.hidden)) document.body.classList.remove('modal-open');
  if (id === 'userModal') STATE.modal = null;
  if (id === 'pwdModal') STATE.pwd = null;
}
function closeTopModal() {
  const open = $$('.modal').filter(m => !m.hidden);
  if (!open.length) return false;
  closeModal(open[open.length - 1].id);
  return true;
}
function modalMsg(id, text, kind) {
  const el = $('#' + id);
  if (!el) return;
  el.textContent = text || '';
  el.classList.toggle('bad', kind === 'bad');
  el.classList.toggle('ok', kind === 'ok');
}

/* ── current identity / permission ───────────────────────────────────── */
function paintMe() {
  const pill = $('#mePill');
  if (!pill || !STATE.me) return;
  const role = STATE.me.role === 'admin' ? t('users.roleAdmin') : t('users.roleViewer');
  pill.textContent = tf('users.mePill', {
    email: STATE.me.email || t('nav.service'), role });
  pill.hidden = false;
}
function showDeny() {
  const card = $('#usersCard');
  if (card) card.hidden = true;
  const deny = $('#denyCard');
  if (deny) deny.hidden = false;
}

/* ── table ───────────────────────────────────────────────────────────── */
function render() {
  const table = $('#usersTable');
  if (!table) return;
  const count = $('#userCount');
  if (count) count.textContent = STATE.users.length ? `· ${STATE.users.length}` : '';
  const head = `<tr>
      <th>${t('users.col.email')}</th><th>${t('users.col.role')}</th>
      <th>${t('users.col.status')}</th><th>${t('users.col.created')}</th>
      <th>${t('users.col.lastLogin')}</th><th class="u-act">${t('users.col.actions')}</th></tr>`;
  if (!STATE.users.length) {
    table.innerHTML = head +
      `<tr><td colspan="6"><div class="empty-cell">${t('users.empty')}</div></td></tr>`;
    return;
  }
  table.innerHTML = head + STATE.users.map(u => {
    const self = isSelf(u);
    const lastAdmin = isLastActiveAdmin(u);
    const noDelete = self || lastAdmin;
    const title = self ? t('users.selfDeleteHint') : lastAdmin ? t('users.lastAdmin') : '';
    const email = esc(u.email || '—');
    return `<tr data-uid="${esc(u.id)}"${self ? ' class="self"' : ''}>
      <td><span class="mono u-email">${email}</span>${self ? `<span class="pill indigo">${t('users.you')}</span>` : ''}</td>
      <td><span class="pill${u.role === 'admin' ? ' indigo' : ''}">${esc(roleLabel(u.role))}</span></td>
      <td><span class="pill${u.status === 'active' ? ' ok' : ' bad'}">${esc(statusLabel(u.status))}</span></td>
      <td class="mono">${esc(fmtStamp(u.created_at))}</td>
      <td class="mono">${esc(fmtStamp(u.last_login_at))}</td>
      <td class="u-act">
        <button class="btn sm" data-edit="${esc(u.id)}">${t('users.edit')}</button>
        <button class="btn sm" data-pwd="${esc(u.id)}">${t('users.reset')}</button>
        <button class="btn sm danger" data-del="${esc(u.id)}"${noDelete ? ` disabled title="${esc(title)}"` : ''}>${t('users.delete')}</button>
      </td></tr>`;
  }).join('');
}

async function loadUsers() {
  try {
    const res = await api.get('/admin/users');
    STATE.users = Array.isArray(res) ? res : (res && Array.isArray(res.users) ? res.users : []);
    render();
  } catch (e) {
    const status = e && (e.status || Number(e.message));
    if (status === 401) return;            // api.js already bounced to login
    if (status === 403) { showDeny(); return; }
    toast(tf('users.loadFail', { err: errTextFor(e) }), 3600);
  }
}

/* ── two-step inline delete (删除 → 确认删除?, same pattern as models page) ── */
function disarmDelete(btn) {
  if (btn._armTimer) clearTimeout(btn._armTimer);
  btn._armTimer = null;
  btn.dataset.armed = '0';
  btn.textContent = btn.dataset.label || t('users.delete');
  btn.classList.remove('armed');
}
function armDelete(btn) {
  if (btn.dataset.armed === '1') return true;
  btn.dataset.armed = '1';
  btn.dataset.label = btn.textContent;
  btn.textContent = t('users.confirmDelete');
  btn.classList.add('armed');
  btn._armTimer = setTimeout(() => { if (btn.isConnected) disarmDelete(btn); }, 3000);
  return false;
}

async function onDelete(btn) {
  const u = userById(btn.dataset.del);
  if (!u || isSelf(u) || isLastActiveAdmin(u)) return;
  if (!armDelete(btn)) return;
  btn.disabled = true;
  try {
    await api.del('/admin/users/' + encodeURIComponent(u.id));
    toast(t('users.deletedOk'));
    await loadUsers();
  } catch (e) {
    if (e && (e.status || Number(e.message)) === 401) return;
    disarmDelete(btn);
    btn.disabled = false;
    toast(errTextFor(e), 3400);
  }
}

/* ── create / edit modal ─────────────────────────────────────────────── */
function paintModalTitle() {
  const title = $('#userModalTitle');
  if (!title) return;
  if (!STATE.modal) return;
  title.textContent = STATE.modal.mode === 'create' ? t('users.newTitle') : t('users.editTitle');
}
function openCreate() {
  STATE.modal = { mode: 'create' };
  paintModalTitle();
  $('#ufEmail').value = '';
  $('#ufPassword').value = '';
  $('#ufPasswordRow').hidden = false;
  $('#ufRole').disabled = false;
  $('#ufRole').value = 'viewer';
  $('#ufStatusRow').hidden = true;
  $('#ufStatus').disabled = false;
  modalMsg('userModalMsg', '');
  openModal('userModal');
  $('#ufEmail').focus();
}
function openEdit(id) {
  const u = userById(id);
  if (!u) return;
  STATE.modal = { mode: 'edit', id: u.id };
  paintModalTitle();
  $('#ufEmail').value = u.email || '';
  $('#ufPasswordRow').hidden = true;
  $('#ufRole').value = u.role === 'admin' ? 'admin' : 'viewer';
  $('#ufStatusRow').hidden = false;
  $('#ufStatus').value = u.status === 'disabled' ? 'disabled' : 'active';
  const locked = isLastActiveAdmin(u);
  $('#ufRole').disabled = locked;
  $('#ufStatus').disabled = locked;
  modalMsg('userModalMsg', locked ? t('users.lastAdminHint') : '');
  openModal('userModal');
  $('#ufEmail').focus();
}

async function saveUser() {
  if (!STATE.modal) return;
  const email = ($('#ufEmail').value || '').trim();
  const role = $('#ufRole').value === 'admin' ? 'admin' : 'viewer';
  const status = $('#ufStatus').value === 'disabled' ? 'disabled' : 'active';
  if (!email) { modalMsg('userModalMsg', t('users.emailInvalid'), 'bad'); return; }
  if (!EMAIL_RE.test(email)) { modalMsg('userModalMsg', t('users.emailInvalid'), 'bad'); return; }
  if (STATE.modal.mode === 'create') {
    const pwd = $('#ufPassword').value || '';
    if (!pwd) { modalMsg('userModalMsg', t('users.required'), 'bad'); return; }
    if (pwd.length < MIN_PWD) { modalMsg('userModalMsg', t('users.pwdShort'), 'bad'); return; }
  } else {
    const u = userById(STATE.modal.id);
    if (u && isLastActiveAdmin(u) && (role !== 'admin' || status !== 'active')) {
      modalMsg('userModalMsg', t('users.lastAdmin'), 'bad');
      return;
    }
  }
  const save = $('#userModalSave');
  save.disabled = true;
  modalMsg('userModalMsg', '');
  try {
    if (STATE.modal.mode === 'create') {
      await api.post('/admin/users', { email, password: $('#ufPassword').value, role });
      toast(t('users.createdOk'));
    } else {
      await api.put('/admin/users/' + encodeURIComponent(STATE.modal.id),
        { email, role, status });
      toast(t('users.updatedOk'));
    }
    closeModal('userModal');
    await loadUsers();
  } catch (e) {
    if (e && (e.status || Number(e.message)) === 401) return;
    modalMsg('userModalMsg', errTextFor(e), 'bad');
  } finally {
    save.disabled = false;
  }
}

/* ── reset password modal ────────────────────────────────────────────── */
function openPwd(id) {
  const u = userById(id);
  if (!u) return;
  STATE.pwd = { id: u.id, email: u.email || '' };
  $('#pwdModalTitle').textContent = tf('users.pwdTitle', { email: u.email || '' });
  $('#pwdNew').value = '';
  modalMsg('pwdModalMsg', '');
  openModal('pwdModal');
  $('#pwdNew').focus();
}
async function savePwd() {
  if (!STATE.pwd) return;
  const pwd = $('#pwdNew').value || '';
  if (pwd.length < MIN_PWD) { modalMsg('pwdModalMsg', t('users.pwdShort'), 'bad'); return; }
  const save = $('#pwdModalSave');
  save.disabled = true;
  modalMsg('pwdModalMsg', '');
  try {
    await api.post('/admin/users/' + encodeURIComponent(STATE.pwd.id) + '/password',
      { new_password: pwd });
    /* 改自己的密码会吊销当前会话（后端行为）：提示后回登录页，别让下一页请求 401。 */
    const self = !!(STATE.me && STATE.me.id && STATE.me.id === STATE.pwd.id);
    toast(t('users.pwdOk'));
    closeModal('pwdModal');
    if (self) setTimeout(() => { window.location.href = 'login.html?expired=1'; }, 1000);
  } catch (e) {
    if (e && (e.status || Number(e.message)) === 401) return;
    modalMsg('pwdModalMsg', errTextFor(e), 'bad');
  } finally {
    save.disabled = false;
  }
}

/* ── wiring ──────────────────────────────────────────────────────────── */
$('#usersTable').addEventListener('click', ev => {
  const btn = ev.target.closest('button');
  if (!btn || btn.disabled) return;
  if (btn.dataset.edit) openEdit(btn.dataset.edit);
  else if (btn.dataset.pwd) openPwd(btn.dataset.pwd);
  else if (btn.dataset.del) onDelete(btn);
});
$('#addUser').addEventListener('click', openCreate);
$('#userModalSave').addEventListener('click', saveUser);
$('#pwdModalSave').addEventListener('click', savePwd);
$$('[data-close]').forEach(btn => btn.addEventListener('click', () => closeModal(btn.dataset.close)));
$$('.modal').forEach(m => m.addEventListener('click', ev => {
  if (ev.target === m) closeModal(m.id);
}));
document.addEventListener('keydown', ev => { if (ev.key === 'Escape') closeTopModal(); });
$('#userModal').addEventListener('keydown', ev => {
  if (ev.key === 'Enter' && ev.target.tagName === 'INPUT') { ev.preventDefault(); saveUser(); }
});
$('#pwdModal').addEventListener('keydown', ev => {
  if (ev.key === 'Enter' && ev.target.tagName === 'INPUT') { ev.preventDefault(); savePwd(); }
});
onLangChange(() => {
  paintMe();
  if (!STATE.me || STATE.me.role !== 'admin') return;
  paintModalTitle();
  if (STATE.pwd) $('#pwdModalTitle').textContent = tf('users.pwdTitle', { email: STATE.pwd.email });
  render();
});

/* ── boot ────────────────────────────────────────────────────────────── */
(async function boot() {
  try {
    STATE.me = await api.me();
  } catch (e) {
    const status = e && (e.status || Number(e.message));
    if (status === 404) toast(t('users.oldBackend'), 3600);   // backend predates user management
    else if (status !== 401) toast(tf('users.loadFail', { err: errTextFor(e) }), 3600);
    return;                                  // 401 → api.js bounced to login
  }
  paintMe();
  if (!STATE.me || STATE.me.role !== 'admin') { showDeny(); return; }
  await loadUsers();
})();
