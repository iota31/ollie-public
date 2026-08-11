// Ollie Mission Control — shared visual vocabulary.
//
// Every panel imports these so the dashboard speaks ONE design language. No
// chart lib, no build step: inline SVG + a tiny <canvas>. All numbers render
// with tabular-nums (set globally in index.html). Honest absence is built in:
// pass null/undefined and you get a gray "—", never a fake 0 or fake green.
//
// THE 5-COLOR LADDER (locked; color is never decorative — every state also
// carries a glyph + word for accessibility):
//   ok          green  ●  "OK"
//   warn        amber  ▲  "WARN"
//   critical    red    ▲  "CRITICAL"
//   maintenance blue   ■  "MAINT"
//   stale       gray   ◌  "N/A"

export const LADDER = {
  ok:          { color: 'var(--ok)',     glyph: '●', word: 'OK' },         // ●
  warn:        { color: 'var(--warn)',   glyph: '▲', word: 'WARN' },       // ▲
  critical:    { color: 'var(--danger)', glyph: '▲', word: 'CRITICAL' },   // ▲
  maintenance: { color: 'var(--maint)',  glyph: '■', word: 'MAINT' },      // ■
  stale:       { color: 'var(--stale)',  glyph: '◌', word: 'N/A' },        // ◌
};

export function ladder(state) {
  return LADDER[state] || LADDER.stale;
}

// esc — small HTML escaper (re-declared here so components.js has no import
// cycle with app.js; identical semantics).
export function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// fmtNum — honest numeric formatter. null/undefined/NaN -> the gray em-dash.
export function fmtNum(v, digits) {
  if (v === null || v === undefined || (typeof v === 'number' && !isFinite(v))) {
    return '<span class="mc-na">—</span>';
  }
  const n = Number(v);
  if (!isFinite(n)) return '<span class="mc-na">—</span>';
  return esc(digits != null ? n.toFixed(digits) : String(n));
}

// ── statTile(value, label, opts) ────────────────────────────────────────────
// Big number + caption, optional trend arrow / delta% / sparkline / state dot.
// opts: { trend: 'up'|'down'|'flat', deltaPct: number, sparkline: number[],
//         state: '<ladder>' }
export function statTile(value, label, opts = {}) {
  const { trend, deltaPct, sparkline: spark, state } = opts;
  const dot = state
    ? `<span class="mc-glyph" style="color:${ladder(state).color}">${ladder(state).glyph}</span> `
    : '';
  const valHtml = (value === null || value === undefined || value === '')
    ? '<span class="mc-na">—</span>'
    : esc(String(value));
  let trendHtml = '';
  if (trend || deltaPct != null) {
    const arrow = trend === 'up' ? '↑' : trend === 'down' ? '↓' : '→';
    const cls = trend === 'up' ? 'mc-trend-up' : trend === 'down' ? 'mc-trend-down' : 'mc-trend-flat';
    const pct = deltaPct != null ? ` ${deltaPct > 0 ? '+' : ''}${esc(deltaPct)}%` : '';
    trendHtml = `<span class="mc-trend ${cls}">${arrow}${pct}</span>`;
  }
  const sparkHtml = Array.isArray(spark) && spark.length
    ? `<div class="mc-stat-spark">${sparkline(spark)}</div>` : '';
  return `<div class="mc-stat-tile">
    <div class="mc-stat-value">${dot}${valHtml}</div>
    <div class="mc-stat-foot"><span class="mc-stat-label">${esc(label)}</span>${trendHtml}</div>
    ${sparkHtml}
  </div>`;
}

// ── barGauge(value, max, opts) ──────────────────────────────────────────────
// Horizontal fill bar (div width %, NOT radial). opts: { state, label }.
// Honest absence: value null -> empty gray track + "N/A".
export function barGauge(value, max, opts = {}) {
  const { state, label } = opts;
  const known = value !== null && value !== undefined && isFinite(Number(value)) && max;
  const pct = known ? Math.max(0, Math.min(100, (Number(value) / Number(max)) * 100)) : 0;
  const color = state ? ladder(state).color : 'var(--accent)';
  const labelHtml = label
    ? `<div class="mc-gauge-label"><span>${esc(label)}</span><span>${
        known ? esc(`${Number(value)} / ${Number(max)}`) : '<span class="mc-na">—</span>'
      }</span></div>` : '';
  return `<div class="mc-gauge">
    ${labelHtml}
    <div class="mc-gauge-track">
      <div class="mc-gauge-fill" style="width:${pct.toFixed(1)}%;background:${color}"></div>
    </div>
  </div>`;
}

// ── sparkline(numbers[]) ────────────────────────────────────────────────────
// Inline SVG <polyline>. Returns a small fixed-viewbox sparkline. Empty/short
// input -> gray dash placeholder.
export function sparkline(numbers, opts = {}) {
  const w = opts.width || 96, h = opts.height || 24, pad = 2;
  const nums = (numbers || []).filter(n => typeof n === 'number' && isFinite(n));
  if (nums.length < 2) return '<span class="mc-na">—</span>';
  const min = Math.min(...nums), max = Math.max(...nums);
  const span = (max - min) || 1;
  const step = (w - pad * 2) / (nums.length - 1);
  const pts = nums.map((n, i) => {
    const x = pad + i * step;
    const y = h - pad - ((n - min) / span) * (h - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const stroke = opts.color || 'var(--accent)';
  return `<svg class="mc-spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" `
    + `preserveAspectRatio="none" aria-hidden="true">`
    + `<polyline fill="none" stroke="${stroke}" stroke-width="1.5" `
    + `stroke-linejoin="round" stroke-linecap="round" points="${pts}"/></svg>`;
}

// ── heartbeatBar(buckets[], opts) ───────────────────────────────────────────
// Tiny <canvas>, newest bucket on the RIGHT. Each bucket: a number 0..1
// (intensity) OR a ladder-state string OR null (gray = no data). Returns a
// canvas element you append; draws on the next frame.
export function heartbeatBar(buckets, opts = {}) {
  const w = opts.width || 240, h = opts.height || 18, gap = 1;
  const cvs = document.createElement('canvas');
  cvs.className = 'mc-heartbeat';
  // Crisp on HiDPI.
  const dpr = (typeof window !== 'undefined' && window.devicePixelRatio) || 1;
  cvs.width = w * dpr; cvs.height = h * dpr;
  cvs.style.width = w + 'px'; cvs.style.height = h + 'px';
  const ctx = cvs.getContext('2d');
  ctx.scale(dpr, dpr);
  const data = buckets || [];
  const n = data.length || 1;
  const bw = Math.max(1, (w - gap * (n - 1)) / n);
  // resolve a CSS var to a concrete color for canvas fill.
  const cssVar = (name) => {
    try {
      return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#666';
    } catch (_) { return '#666'; }
  };
  const colorFor = (b) => {
    if (b === null || b === undefined) return cssVar('--stale');
    if (typeof b === 'string') return cssVar(ladder(b).color.replace(/var\((.+)\)/, '$1'));
    // numeric intensity 0..1 -> ok green at varying alpha-ish via lightness
    return cssVar('--ok');
  };
  data.forEach((b, i) => {
    const x = i * (bw + gap);
    ctx.fillStyle = colorFor(b);
    let bh = h;
    if (typeof b === 'number') bh = Math.max(2, Math.min(1, b) * h);
    ctx.globalAlpha = (b === null || b === undefined) ? 0.35 : 1;
    ctx.fillRect(x, h - bh, bw, bh);
  });
  ctx.globalAlpha = 1;
  return cvs;
}
