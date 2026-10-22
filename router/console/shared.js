/* Shared console helpers (loaded after api.js, before the page script).

   The signal registry, the signal label helpers and the server clock live
   here: every page reads the same /admin/signals payload, so the labels
   (sigSpec / sigName / valLabel), the "is this a rule signal" test and the
   clock cannot drift between the pages (they used to be copy-pasted three
   times with slightly different fallbacks). */
const SIGNAL_REGISTRY = {};      // key -> spec (GET /admin/signals)
let SERVER_OFFSET_MS = 0;        // server clock - browser clock
let clockTimer = null;

/* Fill the registry from GET /admin/signals. */
function setSignalRegistry(list) {
  (list || []).forEach(s => {
    SIGNAL_REGISTRY[s.key] = { ...s, values: (s.values || []).map(v =>
      Array.isArray(v) ? v : [v.key, v.zh, v.en]) };
  });
}

const sigSpec = k => SIGNAL_REGISTRY[k] || {
  key: k, zh: k, en: k, group: 'rule', kind: 'model', values: [] };
const sigName = k => (LANG === 'zh'
  ? (sigSpec(k).zh || k)
  : (sigSpec(k).en || sigSpec(k).zh || k));
const valLabel = (k, v) => {
  if (v == null || v === '') return '';
  const hit = (sigSpec(k).values || []).find(x => String(x[0]) === String(v));
  if (!hit) return String(v);
  return LANG === 'zh' ? hit[1] : (hit[2] || hit[1]);
};
const isRuleSig = k => (sigSpec(k).kind || 'model') === 'rule';
const isFreeSig = k => sigSpec(k).free !== undefined
  ? !!sigSpec(k).free
  : !(sigSpec(k).values || []).length;

/* Reasoning capability fallback: the model declares nothing. */
const FALLBACK_REASON = { declared: false, supported: null, format: null,
  efforts: [], default: null };

/* ── server clock (the time_window rule judges by server time) ───────── */
function setServerOffset(isoNow) {
  if (isoNow) SERVER_OFFSET_MS = new Date(isoNow).getTime() - Date.now();
}
function serverNow() { return new Date(Date.now() + SERVER_OFFSET_MS); }
function fmtServerTime(d) {
  const p2 = n => String(n).padStart(2, '0');
  return `${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
}
function startServerClockTick(tick) {
  if (clockTimer) clearInterval(clockTimer);
  tick();
  clockTimer = setInterval(tick, 1000);
}
