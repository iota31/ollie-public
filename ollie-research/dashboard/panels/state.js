// Ollie State panel — heartbeat, brief, digest, open loops, 24h beat bar.
// Drop-in: registers itself; the shell wires nav/body/tiles.
import { api, registerPanel, registerTile } from '../app.js';
import { statTile, heartbeatBar, ladder, esc, fmtNum } from '../components.js';

const MARKUP = `
  <div class="panel-hdr"><h2>OLLIE STATE</h2></div>
  <div class="panel-body">
    <div class="state-grid">
      <div id="st-lastbeat"></div>
      <div id="st-outcome"></div>
      <div id="st-streak"></div>
      <div id="st-brief"></div>
    </div>
    <div class="mt8">
      <div class="mc-stat-label" style="margin-bottom:4px">24h heartbeat</div>
      <div id="st-bar"></div>
    </div>
    <div class="mt8">
      <div class="mc-stat-label" style="margin-bottom:4px">Open loops</div>
      <div id="st-loops" class="mc-loops"></div>
    </div>
    <div class="mt8">
      <div class="mc-stat-label" style="margin-bottom:4px">Work digest</div>
      <pre id="st-digest" class="mc-pre"></pre>
    </div>
  </div>`;

function _ageStr(iso) {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (isNaN(t)) return '—';
  const sec = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (sec < 60) return sec + 's';
  if (sec < 3600) return Math.round(sec / 60) + 'm';
  return Math.round(sec / 3600) + 'h';
}

function _mapOutcomeToLadder(outcome) {
  if (outcome === 'message') return 'ok';
  if (outcome === 'silence') return 'stale';
  return 'stale';
}

function _bucketsForBar(beats) {
  // 24 hourly buckets, oldest (left) to newest (right).
  // message→'ok', silence→'stale', missing hour→null (gray)
  const now = new Date();
  const curHour = new Date(now.getFullYear(), now.getMonth(), now.getDate(), now.getHours(), 0, 0, 0);
  const buckets = new Array(24).fill(null);
  if (!Array.isArray(beats) || !beats.length) return buckets;

  const hourMap = new Map();
  for (const b of beats) {
    if (!b || !b.ts) continue;
    const t = Date.parse(b.ts);
    if (isNaN(t)) continue;
    const d = new Date(t);
    const h = new Date(d.getFullYear(), d.getMonth(), d.getDate(), d.getHours(), 0, 0, 0).getTime();
    if (!hourMap.has(h)) hourMap.set(h, new Set());
    hourMap.get(h).add(b.outcome);
  }

  for (let i = 0; i < 24; i++) {
    const bucketStart = curHour.getTime() - (23 - i) * 3600 * 1000;
    const set = hourMap.get(bucketStart);
    if (!set) { buckets[i] = null; continue; }
    if (set.has('message')) { buckets[i] = 'ok'; }
    else if (set.has('silence')) { buckets[i] = 'stale'; }
    else { buckets[i] = null; }
  }
  return buckets;
}

export async function loadState() {
  let state = null, log = null, brief = null, digest = null, loops = null;
  try { state = await api('GET', '/api/hb/state'); } catch (_) {}
  try { log = await api('GET', '/api/hb/log'); } catch (_) {}
  try { brief = await api('GET', '/api/hb/brief'); } catch (_) {}
  try { digest = await api('GET', '/api/hb/digest'); } catch (_) {}
  try { loops = await api('GET', '/api/openloops'); } catch (_) {}

  const lastEl = document.getElementById('st-lastbeat');
  if (lastEl) {
    const ts = (log && log.last_beat_ts) || null;
    const age = (log && typeof log.age_s === 'number') ? `${log.age_s}s` : (ts ? _ageStr(ts) : '—');
    lastEl.innerHTML = statTile(age || '—', 'Last beat age', { state: ts ? 'ok' : 'stale' });
  }

  const outEl = document.getElementById('st-outcome');
  if (outEl) {
    const beats = (log && log.beats) || [];
    const last = beats.length ? beats[beats.length - 1] : null;
    const st = last ? _mapOutcomeToLadder(last.outcome) : 'stale';
    const meta = ladder(st);
    const word = (last && last.outcome) ? last.outcome.toUpperCase() : 'N/A';
    outEl.innerHTML = statTile(`${meta.glyph} ${word}`, 'Latest outcome', { state: st });
  }

  const stkEl = document.getElementById('st-streak');
  if (stkEl) {
    const n = (log && typeof log.silence_streak === 'number') ? log.silence_streak : null;
    const st = (n && n > 0) ? 'warn' : 'ok';
    stkEl.innerHTML = statTile(fmtNum(n, 0), 'Silence streak', { state: st });
  }

  const brEl = document.getElementById('st-brief');
  if (brEl) {
    if (!brief) {
      brEl.innerHTML = `<div class="mc-stat-tile"><div class="mc-stat-value"><span class="mc-na">—</span></div><div class="mc-stat-foot"><span class="mc-stat-label">Last brief</span></div></div>`;
    } else {
      const when = brief.mtime ? _ageStr(brief.mtime) : '—';
      const snip = brief.snippet ? esc(brief.snippet.replace(/\s+/g, ' ').slice(0, 120)) : '—';
      brEl.innerHTML = `<div class="mc-stat-tile"><div class="mc-stat-value" style="font-size:12px;line-height:1.3">${snip}</div><div class="mc-stat-foot"><span class="mc-stat-label">Last brief · ${esc(when)}</span></div></div>`;
    }
  }

  const barHost = document.getElementById('st-bar');
  if (barHost) {
    barHost.innerHTML = '';
    const beats = (log && log.beats) || [];
    const buckets = _bucketsForBar(beats);
    const cvs = heartbeatBar(buckets, { width: 520, height: 18 });
    barHost.appendChild(cvs);
  }

  const lpEl = document.getElementById('st-loops');
  if (lpEl) {
    lpEl.innerHTML = '';
    const arr = Array.isArray(loops) ? loops : [];
    if (!arr.length) {
      lpEl.innerHTML = '<span class="mc-na">—</span>';
    } else {
      const ul = document.createElement('ul');
      ul.style.margin = '4px 0 0 16px';
      ul.style.padding = '0';
      arr.forEach(it => {
        const li = document.createElement('li');
        li.style.fontSize = '12px';
        li.style.color = it.done ? 'var(--muted)' : 'var(--text)';
        li.innerHTML = (it.done ? '☑ ' : '☐ ') + esc(it.text || '');
        ul.appendChild(li);
      });
      lpEl.appendChild(ul);
    }
  }

  const dgEl = document.getElementById('st-digest');
  if (dgEl) {
    if (!digest || !digest.content) {
      dgEl.textContent = '—';
    } else {
      dgEl.textContent = digest.content;
    }
  }
}

registerPanel({
  id: 'state', title: 'Ollie State', group: 'System', refreshMs: 15000,
  render(el) {
    if (!el.dataset.built) { el.innerHTML = MARKUP; el.dataset.built = '1'; }
    loadState();
    // Wire the beat-now control button (safe/idempotent; server rate-limits).
    const btn = el.querySelector('#st-beat-now');
    const res = el.querySelector('#st-beat-result');
    if (btn && !btn._wired) {
      btn._wired = true;
      btn.onclick = async () => {
        const prev = btn.textContent;
        btn.disabled = true;
        btn.textContent = '…';
        if (res) res.textContent = '';
        try {
          const out = await ctrl('POST', '/api/ctrl/heartbeat/beat', { confirm: true });
          if (res) res.textContent = (out && out.ok) ? 'beat ok' : ('beat: ' + (out && out.note || ''));
        } catch (e) {
          if (res) res.textContent = 'beat failed';
        } finally {
          btn.disabled = false;
          btn.textContent = prev;
          setTimeout(() => { if (res) res.textContent = ''; }, 2000);
        }
      };
    }
  },
});

registerTile({
  id: 'state', refreshMs: 15000,
  async render(el) {
    let log = null;
    try { log = await api('GET', '/api/hb/log'); } catch (_) { log = null; }
    const beats = (log && log.beats) || [];
    const last = beats.length ? beats[beats.length - 1] : null;
    const age = (log && typeof log.age_s === 'number') ? `${log.age_s}s` : (last && last.ts ? _ageStr(last.ts) : '—');
    const st = last ? _mapOutcomeToLadder(last.outcome) : 'stale';
    const meta = ladder(st);
    const word = (last && last.outcome) ? last.outcome.toUpperCase() : 'N/A';
    el.innerHTML = statTile(`${meta.glyph} ${word} · ${age}`, 'Ollie State', { state: st });
  },
});
