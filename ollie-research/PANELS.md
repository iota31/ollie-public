# Ollie Mission Control — Drop-in Panel Contract

This document defines the minimal surface a new panel touches so another agent can add UI + data **without editing any shared shell files except one append-only import line**.

Everything else is auto-discovered:
- Frontend: panels self-register via side-effect imports.
- Backend: `mc/reads_*.py` (and `controls*.py`) are glob-imported by `mc.load_handlers()`.

---

## FRONTEND

### 1) Create a panel module

File: `dashboard/panels/<name>.js`

```js
import { api } from '../app.js';
import { statTile, barGauge, sparkline, heartbeatBar, ladder, esc, fmtNum } from '../components.js';

export function mount(el) {
  // Build your DOM into `el`. Use the shared helpers for consistent visuals.
  el.innerHTML = `
    <h2>My Panel</h2>
    <div id="my-stats"></div>
  `;
  refresh();
}

async function refresh() {
  const data = await api('GET', '/api/my/data');
  const box = document.getElementById('my-stats');
  if (!box) return;
  box.innerHTML = statTile(data.value, 'Some Metric', { state: data.state });
}

registerPanel({
  id: 'mypanel',
  title: 'My Panel',
  group: 'System',
  render: mount,
  refreshMs: 15000,     // poll only while this panel is visible; 0/omitted = no auto-refresh
  onActivate: () => {}, // optional hook each time the panel is shown
});
```

**Exact `registerPanel` contract (from `app.js`):**

```js
registerPanel({ id, title, group, render(el, api), refreshMs, onActivate })
// id        unique slug (also becomes body container id: panel-<id>)
// title     nav label (falls back to id)
// group     nav group heading (e.g. 'Curiosity', 'System'); optional
// render    (el, api) => void   build the panel into `el` (called once on first show, then on refresh cadence)
// refreshMs poll cadence in ms while ACTIVE (omit or 0 = no auto-refresh)
// onActivate(el, api) optional hook fired each time the panel becomes visible
```

**Exact `registerTile` contract (from `app.js`):**

```js
registerTile({ id, render(el, api), refreshMs })
// Overview grid card. Same lifecycle as panels; rendered into the overview tile grid.
// id        unique within tiles
// render    (el, api) => void
// refreshMs shared 15s cadence for the whole overview grid; tiles idle when not shown
```

### 2) Register your panel (the ONE shared edit)

Edit **only** `dashboard/panels/index.js`. Append a single import line:

```js
// ── System panels (added by parallel panel agents — append below) ───────────
import './mypanel.js';
```

- This is append-only and merge-safe.
- The imported module runs its registration at import time (side-effect).
- No other shared file is touched.

### 3) Use shared visual vocabulary (from `components.js`)

All panels import the same helpers so the dashboard speaks one design language.

**Color/state ladder + glyph + word (locked):**

```js
// From components.js — LADDER and ladder(state)
LADDER = {
  ok:          { color: 'var(--ok)',     glyph: '●', word: 'OK' },
  warn:        { color: 'var(--warn)',   glyph: '▲', word: 'WARN' },
  critical:    { color: 'var(--danger)', glyph: '▲', word: 'CRITICAL' },
  maintenance: { color: 'var(--maint)',  glyph: '■', word: 'MAINT' },
  stale:       { color: 'var(--stale)',  glyph: '◌', word: 'N/A' },
};
ladder(state) // returns LADDER[state] || LADDER.stale
```

**Honest absence (stale → N/A):**
- Pass `null` / `undefined` / non-finite numbers to any formatter or component → you get a gray em-dash or "N/A", never a fabricated zero or green.
- `fmtNum(v, digits?)` → gray `<span class="mc-na">—</span>` for null/undefined/NaN/Infinity.
- `barGauge(value, max, {state, label})` → empty gray track + "N/A" when value is absent.
- `sparkline(numbers[])` → gray dash when <2 valid numbers.
- `heartbeatBar(buckets[])` → gray cells for `null`/`undefined` entries.

**Helper signatures (real, from `components.js`):**

```js
statTile(value, label, opts = {})
// opts: { trend: 'up'|'down'|'flat', deltaPct: number, sparkline: number[], state: '<ladder>' }

barGauge(value, max, opts = {})
// opts: { state: '<ladder>', label: string }
// Honest absence: value null/undefined → empty gray track + "N/A" label value.

sparkline(numbers, opts = {})
// opts: { width, height, color }
// Returns inline SVG <polyline>; <2 valid numbers → gray dash.

heartbeatBar(buckets, opts = {})
// opts: { width, height }
// buckets: array of number (0..1 intensity) | '<ladder>' state string | null
// Returns a <canvas> element (HiDPI scaled). Append it; draws on next frame.
// null/undefined buckets render gray (stale).

ladder(state) // state → {color, glyph, word}
esc(s)        // HTML escape
fmtNum(v, digits?) // number or gray em-dash
```

**Example using the ladder + a state dot in a tile:**

```js
import { statTile, ladder } from '../components.js';
el.innerHTML = statTile(42, 'Widgets', { state: 'warn' });
// renders a value with an amber ▲ dot and "WARN" semantics.
```

---

## BACKEND

### 1) Create a read handler module (auto-discovered)

File: `mc/reads_<name>.py`

```python
from . import route
from .io import _load
from .cache import TTLCache

_TTL = TTLCache(ttl=5.0)  # process-wide, thread-safe TTL memo for expensive reads

@route("GET", "/api/my/data")
def get_my_data(handler):
    # Cheap or cached reads only. Never cross the 2222 hop here.
    data = _TTL.get_or_set("my:expensive", _compute_expensive)
    handler._json(200, data)

def _compute_expensive():
    # Example: subprocess, large log scan, or a stat that is safe to memoize briefly.
    return {"value": 42, "state": "ok"}
```

**Handler contract (real, from existing reads):**
- Signature: `def handler(handler, **path_params) -> None`
  - `handler` is the live `BaseHTTPRequestHandler`.
  - Use its response helpers: `handler._json(code, payload)`, `handler._err(code, msg)`.
  - For path params (e.g. `/api/foo/{id}`), declare them in the pattern; they are passed as kwargs.
- Return by **writing the response** via the handler; do not return a value to the framework.
- Reads are create-tolerant: a missing file yields honest absence (`None` / `[]`), never a fabricated green/zero. See `mc/io.py`: `_load(path) → None` on `FileNotFoundError`; `_tail(path, n) → []` on missing.

**Auto-registration:**
- `mc.load_handlers()` (called at server import time) does:
  ```python
  for pattern in ("reads_*.py", "controls*.py"):
      for path in sorted(glob...):
          importlib.import_module(f".{mod}", __name__)
  ```
- A new `mc/reads_jobs.py` registers its `@route` endpoints with **no edit** to any central file.
- Idempotent: re-import is a no-op; a broken panel module is caught and logged but does not crash the server.

### 2) Caching for expensive reads

Use `mc/cache.py` `TTLCache`:

```python
from .cache import TTLCache
_CACHE = TTLCache(ttl=20.0)

def get_liveness_cached():
    return _CACHE.get_or_set("liveness", _compute_liveness)
```

- Thread-safe; `get_or_set(key, producer)` computes once per TTL window.
- Current liveness endpoint uses a 20s TTL because it does port probes + `/proc` + `df`.

### 3) Color/state ladder expectations (server → UI)

The 5-color ladder is defined in two places that must stay in sync:

- **UI** — `dashboard/components.js` `LADDER` and `ladder(state)` (glyph + word + CSS var color).
- **Server** — `mc/reads_system.py` documents the reduction and verdict rollup:

```
"ok"          green   — alive / healthy
"warn"        amber   — degraded but functioning (e.g., watchdog non-critical, soft/stale)
"critical"    red     — hard failure (process/port down, or watchdog critical)
"maintenance" blue    — intentionally off / paused (off-heat, not a fault)
"stale"       gray    — no data / unknown (e.g., hands on Windows unreachable from WSL)
```

Reduction rules (both for pills and the strip verdict) live in `_worst` + `_RANK` + `_VERDICT_FOR_STATE`:

- A pill is the **worst-of-its-children** (liveness states + watchdog signal for that subsystem).
- Overall strip verdict:
  - any `critical` → `CRITICAL`
  - else any `warn` → `ATTENTION`
  - else any `stale`/`degraded` (maintenance is intentionally below) → `DEGRADED`
  - else any `maintenance` → `MAINTENANCE`
  - else → `NOMINAL`

Panels must render using `ladder(state)` so the glyph/word/color are consistent and accessible. Never invent a sixth state.

---

## Tiny Worked Example (end-to-end drop-in)

**Backend** — `mc/reads_ping.py`

```python
from . import route

@route("GET", "/api/ping")
def get_ping(handler):
    handler._json(200, {"pong": True, "at": __import__("time").time()})
```

No central edit. The route is live after restart (or after `load_handlers` re-import in dev).

**Frontend** — `dashboard/panels/ping.js`

```js
import { api } from '../app.js';
import { statTile } from '../components.js';

function render(el) {
  el.innerHTML = '<div id="ping-tile"></div>';
  refresh();
}
async function refresh() {
  const d = await api('GET', '/api/ping');
  const t = document.getElementById('ping-tile');
  if (t) t.innerHTML = statTile(d.pong ? 'pong' : '—', 'Ping', { state: d.pong ? 'ok' : 'stale' });
}

registerPanel({ id: 'ping', title: 'Ping', group: 'System', render, refreshMs: 10000 });
```

**Manifest** — `dashboard/panels/index.js` (append-only)

```js
import './ping.js';
```

Result:
- Nav gains a "System → Ping" entry.
- Overview can get a tile if you also call `registerTile`.
- The endpoint is served by the same bearer-gated server; no core changes.

---

## Conventions & Guardrails

- **One shared edit only**: `dashboard/panels/index.js` (append one import). Everything else is new files.
- **No shared file edits for backend**: drop `mc/reads_*.py`; glob import discovers it.
- **Create-tolerant reads**: missing files → `None`/`[]`/honest absence; never fabricate green or zero.
- **Color is semantic**: every ladder state carries glyph + word; use `ladder(state)` and the shared components.
- **Cache, don't hammer**: wrap expensive probes in `TTLCache` (see liveness as the canonical example).
- **No 2222 hop in the shell**: Mission Control reads are local (ports, `/proc`, files under `$HOME/.openclaw`). Cross-hop is a higher layer concern.
- **Keep diffs small**: a panel agent should add files and one line; the manager reviews and lands.

If the contract here is insufficient for a new need, coordinate with the shell owner — do not mutate `app.js`, `components.js`, or `mc/__init__.py` from a panel change.
