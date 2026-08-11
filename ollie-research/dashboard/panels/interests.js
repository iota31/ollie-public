// Interests panel — load/save the three textareas (domains / keywords / anti).
// Re-homed under the Mission Control shell as a "Curiosity" drill-down.
import { api, toast, registerPanel } from '../app.js';

const MARKUP = `
  <div class="panel-hdr"><h2>INTERESTS PROFILE</h2></div>
  <div class="panel-body">
    <p style="font-size:12px;color:var(--muted);margin-bottom:14px">
      One item per line. The relevance gate uses these to score incoming items.
    </p>
    <div class="interests-grid">
      <label>Domains <span>(one per line)</span>
        <textarea id="i-domains" rows="10" placeholder="ai&#10;machine-learning&#10;developer-tools"></textarea>
      </label>
      <label>Keywords boost <span>(one per line)</span>
        <textarea id="i-keywords" rows="10" placeholder="embedding&#10;fine-tuning&#10;latency"></textarea>
      </label>
      <label>Anti-interests <span>(one per line — drop these)</span>
        <textarea id="i-anti" rows="10" placeholder="crypto&#10;sports&#10;celebrity"></textarea>
      </label>
    </div>
    <div class="form-actions mt8">
      <button class="btn btn-ok" onclick="saveInterests()">Save interests</button>
      <span id="int-msg"></span>
    </div>
  </div>`;

registerPanel({
  id: 'interests', title: 'Interests', group: 'Curiosity', refreshMs: 0,
  render(el) {
    if (!el.dataset.built) { el.innerHTML = MARKUP; el.dataset.built = '1'; }
    loadInterests();
  },
});

export async function loadInterests() {
  try {
    const d = await api('GET', '/api/interests');
    document.getElementById('i-domains').value  = (d.domains || []).join('\n');
    document.getElementById('i-keywords').value = (d.keywords_boost || []).join('\n');
    document.getElementById('i-anti').value     = (d.anti_interests || []).join('\n');
  } catch (e) { toast('Interests: ' + e.message, false); }
}

export async function saveInterests() {
  const parse = id => document.getElementById(id).value.split('\n').map(s => s.trim()).filter(Boolean);
  const body = {
    domains:        parse('i-domains'),
    keywords_boost: parse('i-keywords'),
    anti_interests: parse('i-anti')
  };
  const msg = document.getElementById('int-msg');
  try {
    await api('PUT', '/api/interests', body);
    msg.textContent = '✓ saved'; msg.style.color = 'var(--ok)';
    setTimeout(() => { msg.textContent = ''; }, 2000);
  } catch (e) { msg.textContent = e.message; msg.style.color = 'var(--danger)'; }
}

Object.assign(window, { saveInterests });
