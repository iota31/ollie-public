// Sources panel — list, add, inline-edit (weight/recency/enabled), delete.
// Logic moved verbatim from the original inline <script>; now re-homed under
// the Mission Control shell as a "Curiosity" drill-down via registerPanel.
import { api, toast, esc, registerPanel } from '../app.js';

let sources = [];

// Markup that used to live in index.html's #panel-sources. Rendered into the
// shell-provided container on first activation.
const MARKUP = `
  <div class="panel-hdr">
    <h2>SOURCES</h2>
    <span id="src-count" class="count">0</span>
    <button class="btn btn-sm btn-ok" onclick="toggleAddForm()" style="margin-left:auto">+ Add source</button>
  </div>
  <div class="panel-body">
    <div id="add-form" style="display:none;background:var(--input);border:1px solid var(--border);
         border-radius:4px;padding:14px;margin-bottom:14px">
      <div style="font-size:12px;color:var(--muted);margin-bottom:8px">NEW SOURCE</div>
      <div class="form-row">
        <label>Type *
          <select id="f-type">
            <option>rss</option><option>reddit</option><option>blog</option>
            <option>discovery</option><option>instagram</option><option>x</option>
          </select>
        </label>
        <label>Target (URL / handle / query) *
          <input id="f-target" placeholder="https://... or r/subreddit or @handle">
        </label>
        <label>Domain tags (comma-sep)
          <input id="f-tags" placeholder="ai,ml,tools">
        </label>
        <label>Weight
          <input id="f-weight" type="number" step="0.1" min="0.1" max="10" value="1.0">
        </label>
        <label>Recency days
          <input id="f-recency" type="number" min="1" max="365" value="7">
        </label>
        <label>Enabled
          <select id="f-enabled"><option value="true">yes</option><option value="false">no</option></select>
        </label>
      </div>
      <div class="form-actions">
        <button class="btn btn-ok" onclick="addSource()">Add</button>
        <button class="btn" onclick="toggleAddForm()">Cancel</button>
      </div>
      <div id="add-err" class="err"></div>
    </div>
    <table id="src-table">
      <thead><tr>
        <th>ID</th><th>Type</th><th>Target</th><th>Tags</th>
        <th>Weight</th><th>Recency</th><th>Enabled</th><th></th>
      </tr></thead>
      <tbody id="src-body"></tbody>
    </table>
  </div>`;

registerPanel({
  id: 'sources', title: 'Sources', group: 'Curiosity', refreshMs: 0,
  render(el) {
    if (!el.dataset.built) { el.innerHTML = MARKUP; el.dataset.built = '1'; }
    loadSources();
  },
});

export async function loadSources() {
  try {
    sources = await api('GET', '/api/sources');
    renderSources();
  } catch (e) { toast('Sources: ' + e.message, false); }
}

function renderSources() {
  document.getElementById('src-count').textContent = sources.length;
  const tbody = document.getElementById('src-body');
  tbody.innerHTML = '';
  sources.forEach(s => {
    const tr = document.createElement('tr');
    const tagsHtml = (s.domain_tags || []).map(t => `<span class="tag">${esc(t)}</span>`).join('');
    tr.innerHTML = `
      <td style="font-size:11px;color:var(--muted)">${esc(s.id)}</td>
      <td><span class="tag">${esc(s.type)}</span></td>
      <td style="max-width:260px;word-break:break-all;font-size:12px">${esc(s.target)}</td>
      <td>${tagsHtml || '<span class="muted">—</span>'}</td>
      <td><input type="number" step="0.1" min="0.1" max="10" value="${s.weight}"
           style="width:60px" onchange="patchSource('${esc(s.id)}','weight',+this.value)"></td>
      <td><input type="number" min="1" max="365" value="${s.recency_days}"
           style="width:60px" onchange="patchSource('${esc(s.id)}','recency_days',+this.value)"></td>
      <td><toggle class="${s.enabled ? 'on' : ''}" onclick="toggleEnabled('${esc(s.id)}',this)"></toggle></td>
      <td><button class="btn btn-sm btn-danger" onclick="deleteSource('${esc(s.id)}')">del</button></td>
    `;
    tbody.appendChild(tr);
  });
}

export function toggleAddForm() {
  const f = document.getElementById('add-form');
  f.style.display = f.style.display === 'none' ? 'block' : 'none';
}

export async function addSource() {
  const errEl = document.getElementById('add-err');
  errEl.textContent = '';
  const tags = document.getElementById('f-tags').value.split(',').map(t => t.trim()).filter(Boolean);
  const body = {
    type: document.getElementById('f-type').value,
    target: document.getElementById('f-target').value.trim(),
    domain_tags: tags,
    weight: parseFloat(document.getElementById('f-weight').value) || 1,
    recency_days: parseInt(document.getElementById('f-recency').value) || 7,
    enabled: document.getElementById('f-enabled').value === 'true'
  };
  if (!body.target) { errEl.textContent = 'Target required'; return; }
  try {
    await api('POST', '/api/sources', body);
    toast('Source added');
    toggleAddForm();
    loadSources();
  } catch (e) { errEl.textContent = e.message; }
}

export async function patchSource(id, field, value) {
  try {
    await api('PUT', '/api/sources/' + id, { [field]: value });
    toast('Saved');
  } catch (e) { toast(e.message, false); loadSources(); }
}

export async function toggleEnabled(id, el) {
  const current = el.classList.contains('on');
  const next = !current;
  el.classList.toggle('on', next);
  try {
    await api('PUT', '/api/sources/' + id, { enabled: next });
    const s = sources.find(x => x.id === id);
    if (s) s.enabled = next;
  } catch (e) { el.classList.toggle('on', current); toast(e.message, false); }
}

export async function deleteSource(id) {
  if (!confirm('Delete source ' + id + '?')) return;
  try {
    await api('DELETE', '/api/sources/' + id);
    toast('Deleted');
    loadSources();
  } catch (e) { toast(e.message, false); }
}

// Inline onclick= handlers in the markup need these on window.
Object.assign(window, { toggleAddForm, addSource, patchSource, toggleEnabled, deleteSource });
