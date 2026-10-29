const FALLBACK_REASON = { declared: false, supported: null, format: null, efforts: [], default: null };
let SERVER_OFFSET_MS = 0;
let clockTimer = null;

function setServerOffset(isoNow) {
  if (isoNow) SERVER_OFFSET_MS = new Date(isoNow).getTime() - Date.now();
}

function serverNow() {
  return new Date(Date.now() + SERVER_OFFSET_MS);
}

function fmtServerTime(date) {
  const pad = value => String(value).padStart(2, '0');
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function startServerClockTick(tick) {
  if (clockTimer) clearInterval(clockTimer);
  tick();
  clockTimer = setInterval(tick, 1000);
}
