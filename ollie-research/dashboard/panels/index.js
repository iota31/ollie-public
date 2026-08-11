// Mission Control — PANEL MANIFEST.
//
// This is the ONE shared file a panel agent touches: append a single import
// line for your module. Each imported module calls registerPanel()/
// registerTile() at import time (side-effect import), so the shell auto-builds
// its nav + body + overview from the registry. Append-only ⇒ merge-safe.
//
// To add a panel: see ../../PANELS.md.

// ── Curiosity group (existing engine, re-homed under the shell) ─────────────
import './sources.js';
import './interests.js';
import './queue.js';
import './budget.js';

// ── System panels (added by parallel panel agents — append below) ───────────
// import './state.js';
// import './jobs.js';
// import './watchdog.js';
// import './lab.js';
