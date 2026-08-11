// Queue panel — priority list with drag-to-reorder, save order, mute, remove.
// Re-homed under the Mission Control shell as a "Curiosity" drill-down.
import { api, toast, esc, registerPanel } from '../app.js';

let queue = [];
let dragSrc = null;

const MARKUP = `
  <div class="panel-hdr">
    <h2>PRIORITY QUEUE</h2>
    <span id="q-count" class="count">0</span>
    <span style="margin-left:auto;font-size:11px;color:var(--muted)">drag rows to reorder · click save after</span>
    <button class="btn btn-sm btn-accent" onclick="saveQueueOrder()" style="margin-left:8px">Save order</button>
    <span id="q-order-msg" style="font-size:12px;margin-left:8px"></span>
  </div>
  <div class="panel-body" style="padding:0">
    <div class="q-hdr">
      <div></div><div>Title / URL</div><div>Source</div>
      <div>Relevance</div><div>Recency</div><div>Score</div><div>Status</div><div></div>
    </div>
    <div id="q-body"></div>
  </div>`;

registerPanel({
  id: 'queue', title: 'Queue', group: 'Curiosity', refreshMs: 0,
  render(el) {
    if (!el.dataset.built) { el.innerHTML = MARKUP; el.dataset.built = '1'; }
    loadQueue();
  },
});

export async function loadQueue() {
  try {
    queue = await api('GET', '/api/queue');
    renderQueue();
  } catch (e) { toast('Queue: ' + e.message, false); }
}

function renderQueue() {
  document.getElementById('q-count').textContent = queue.length;
  const body = document.getElementById('q-body');
  body.innerHTML = '';
  const sorted = [...queue].sort((a, b) => {
    const pa = a.manual_priority ?? 9999;
    const pb = b.manual_priority ?? 9999;
    if (pa !== pb) return pa - pb;
    return (b.score || 0) - (a.score || 0);
  });
  sorted.forEach(item => {
    const row = document.createElement('div');
    row.className = 'q-row';
    row.draggable = true;
    row.dataset.fp = item.fingerprint;
    const score = (item.score || 0).toFixed(3);
    const rel   = ((item.relevance || 0) * 100).toFixed(0);
    const rec   = ((item.recency_factor || 0) * 100).toFixed(0);
    const status = item.status || 'pending';
    const statusBadge = `<span class="status-badge ${status}">${esc(status)}</span>`;
    const title = item.title || item.url || item.fingerprint;
    const prio = item.manual_priority !== null && item.manual_priority !== undefined
      ? `<span class="tag">#${item.manual_priority}</span>` : '';
    row.innerHTML = `
      <div class="drag-handle">⠿</div>
      <div style="font-size:12px;overflow:hidden">
        ${prio}<span style="color:#bbb">${esc(title.substring(0, 80))}</span>
        <div style="font-size:10px;color:var(--muted);margin-top:1px">${esc((item.url || '').substring(0, 60))}</div>
      </div>
      <div style="font-size:11px;color:var(--muted)">${esc(item.source_id || '')}</div>
      <div>${rel}%<div class="score-bar" style="width:${rel}%"></div></div>
      <div>${rec}%</div>
      <div style="font-weight:600;color:var(--accent)">${score}</div>
      <div>${statusBadge}
        <button class="btn btn-sm" style="margin-top:3px;font-size:10px"
          onclick="muteItem('${esc(item.fingerprint)}')">mute</button>
      </div>
      <div><button class="btn btn-sm btn-danger" onclick="removeQueueItem('${esc(item.fingerprint)}')">✕</button></div>
    `;
    // drag events
    row.addEventListener('dragstart', e => {
      dragSrc = row;
      row.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
    });
    row.addEventListener('dragend', () => row.classList.remove('dragging'));
    row.addEventListener('dragover', e => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      document.querySelectorAll('.q-row').forEach(r => r.classList.remove('drag-over'));
      row.classList.add('drag-over');
    });
    row.addEventListener('dragleave', () => row.classList.remove('drag-over'));
    row.addEventListener('drop', e => {
      e.preventDefault();
      row.classList.remove('drag-over');
      if (dragSrc && dragSrc !== row) {
        const parent = row.parentNode;
        const rows = [...parent.querySelectorAll('.q-row')];
        const srcIdx = rows.indexOf(dragSrc);
        const tgtIdx = rows.indexOf(row);
        if (srcIdx < tgtIdx) parent.insertBefore(dragSrc, row.nextSibling);
        else parent.insertBefore(dragSrc, row);
      }
    });
    body.appendChild(row);
  });
}

export async function saveQueueOrder() {
  const rows = document.querySelectorAll('#q-body .q-row');
  const fps = [...rows].map(r => r.dataset.fp).filter(Boolean);
  const msg = document.getElementById('q-order-msg');
  try {
    await api('POST', '/api/queue/reorder', fps);
    msg.textContent = '✓ saved'; msg.style.color = 'var(--ok)';
    setTimeout(() => { msg.textContent = ''; }, 2000);
    await loadQueue();
  } catch (e) { msg.textContent = e.message; msg.style.color = 'var(--danger)'; }
}

export async function muteItem(fp) {
  try {
    await api('PUT', '/api/queue/' + fp, { status: 'muted' });
    toast('Muted');
    loadQueue();
  } catch (e) { toast(e.message, false); }
}

export async function removeQueueItem(fp) {
  try {
    await api('DELETE', '/api/queue/' + fp);
    toast('Removed from queue');
    loadQueue();
  } catch (e) { toast(e.message, false); }
}

Object.assign(window, { saveQueueOrder, muteItem, removeQueueItem });
