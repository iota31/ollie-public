// Budget panel — status + spend-log tail. Re-homed under the Mission Control
// shell as a "Curiosity" drill-down, plus a compact Overview tile.
// Errors stay swallowed (budget is non-fatal background info).
import { api, registerPanel, registerTile } from '../app.js';
import { statTile, esc } from '../components.js';

const MARKUP = `
  <div class="panel-hdr"><h2>BUDGET &amp; SPEND</h2></div>
  <div class="panel-body">
    <div class="budget-block">
      <h3>Today's counts vs ceilings</h3>
      <pre id="b-status">loading…</pre>
      <h3>Recent Firecrawl credit log (last 20 lines)</h3>
      <pre id="b-log">—</pre>
    </div>
    <div class="form-actions mt8">
      <button class="btn btn-sm" onclick="loadBudget()">Refresh</button>
    </div>
  </div>`;

export async function loadBudget() {
  try {
    const d = await api('GET', '/api/budget');
    const s = document.getElementById('b-status');
    const l = document.getElementById('b-log');
    if (s) s.textContent = d.status || '—';
    if (l) l.textContent =
      (d.spend_tail && d.spend_tail.length) ? d.spend_tail.join('\n') : '(no entries yet)';
  } catch (e) { /* budget errors are non-fatal */ }
}

registerPanel({
  id: 'budget', title: 'Budget', group: 'Curiosity', refreshMs: 30000,
  render(el) {
    if (!el.dataset.built) { el.innerHTML = MARKUP; el.dataset.built = '1'; }
    loadBudget();
  },
});

// Overview tile: a one-line budget summary (honest absence on failure).
registerTile({
  id: 'budget', refreshMs: 30000,
  async render(el) {
    let summary = null;
    try {
      const d = await api('GET', '/api/budget');
      summary = (d.status || '').replace(/\s+/g, ' ').substring(0, 60) || null;
    } catch (e) { summary = null; }
    el.innerHTML = statTile(summary || '—', 'Budget · today', { state: summary ? 'ok' : 'stale' });
  },
});

Object.assign(window, { loadBudget });
