# Make every Hands Camoufox runtime visibly headed on the Windows desktop

> Read-only exploration result. Goal: ship the smallest correct change so
> that **every** Camoufox launch under `ollie-hands` is both `headless=False`
> AND renders its top-level window on the Source user's interactive
> console session (not on session 0 / not on a hidden station). Includes
> the precise tests + live HWND verifications that prove the window is
> actually visible, not just that headless was flipped.

---

## 1. Current state — what exists vs what's missing

### What exists
- `ollie-hands/ollie_hands/browser.py`
  - `_ensure_started()` instantiates `camoufox.async_api.AsyncCamoufox(
      headless=True, persistent_context=True, user_data_dir=PROFILE_DIR,
      os=["windows"], humanize=True, locale="en-US")` (lines 110-117).
  - `PROFILE_DIR = r"C:\OllieChrome\camoufox-profile"` (line 16).
  - `status()` returns `{started, url, profile}` only — no window/HWND info.
  - Engine runs under the venv in session 1 (interactive) via
    `scripts/run-host.bat` → `scripts/supervisor.py`, launched by the
    `OllieHands` scheduled task (`setup-host-task.ps1`,
    `setup-engine-restart.ps1`). The session is already set up correctly
    for a visible desktop (keep-awake, console auto-reattach on RDP).
- `scripts/camoufox-login.py` already uses `headless=False` with the same
  PROFILE_DIR / fingerprint as the engine (lines 39-41). This is the
  template for what headed launch must look like.
- `ollie_hands/observe.py:window_list()` returns
  `{hwnd, title, pid, process, rect, minimized, foreground}` for every
  visible top-level window via `EnumWindows` + `IsWindowVisible` (lines
  41-73). The HWND probe primitive already exists; we just don't run it
  after a Camoufox launch.
- `ollie_hands/conditions.py` already supports `window_exists(title)` and
  `foreground(process?, title?)` (lines 35-54) — so act-script
  preconditions can already assert "Camoufox window is visible" once the
  engine exposes the right title + there is a window on screen.
- `tests/test_browser_lifecycle.py` — pure mock tests (FakeCamoufox) that
  verify loop discipline + retry safety. None assert headless=False.
- `tests/test_browser_camoufox_integration.py` — real Camoufox on a
  loopback HTML form; currently asserts `started=True` and `url`. It does
  NOT inspect any window / HWND. Currently skipped on non-Windows.
- `scripts/smoke-browser-reddit.py` — pre-submit live-Reddit smoke;
  read-only against a `.invalid` address. No visibility assertion.
- `scripts/restart-host.ps1` — already kills `camoufox` and clears
  `C:\OllieChrome\camoufox-profile\parent.lock` on a restart (lines
  39-40). So the restart path is safe for headed re-launches.

### What's missing (the gap)
1. `browser.py` launches with `headless=True`. There is no code path
   that opens a visible Firefox window on the desktop.
2. Even if `headless=False`, on Windows Camoufox opens a window on the
   **session/station the parent process is attached to**. If the engine
   is somehow started over a non-interactive channel (any future code
   change that bypasses the scheduled task), the window will exist but
   be invisible (Windows session 0 / a non-interactive window station).
   We have to prove the visible attachment at runtime, not assume it.
3. No test asserts `headless=False` after launch.
4. No test asserts a Camoufox top-level window is enumerable by
   `IsWindowVisible` + has the expected title and a non-zero rect.
5. No live HWND assertion in the engine itself — `status()` returns no
   HWND, so even a smoke check would have to shell out to
   `observe.window_list()` separately.
6. `setup-host-task.ps1` already registers the engine to run in session
   1 as `Source` / `LogonType Interactive` / `RunLevel Highest`
   (lines 30-31, 33-34). So the host session preconditions are
   correct — but they are not enforced; nothing checks that the actual
   process holding :3200 has `SessionId=1`.

---

## 2. Minimal complete change

### 2.1 Code: `ollie-hands/ollie_hands/browser.py`

Single behavioural change inside `_ensure_started()`:

- Switch `headless=True` → `headless=False`. Same `persistent_context`,
  same `user_data_dir=PROFILE_DIR`, same `os=["windows"]`, same
  `humanize`, same `locale`. This is sufficient to make Camoufox spawn
  a real Firefox top-level window on the host's interactive session,
  **because** the parent (the engine) is launched by `OllieHands`
  scheduled task with `LogonType Interactive` and the Source user —
  i.e. exactly the conditions the existing `camoufox-login.py` already
  relies on.
- Add a `firefox_user_pref` (or `args=["--window-size=W,H"]` /
  `geoip=True`) only if needed for positioning; do NOT add any new
  dependency, and keep `headless=False` as the only behavioural delta
  vs `camoufox-login.py`.
- After the `__aenter__` resolves, enumerate the just-created context's
  pages, query each page's `page.context.browser` for `new_context`-level
  HWND via Playwright's CDP `Target.getTargetInfo` (or simply record the
  page's `main_frame.url` and trust Camoufox's persistent_context to
  surface a single visible window — the test below verifies on the
  Win32 side). Do NOT add a new dependency on `pywin32`; reuse the
  already-imported `ctypes`/`user32` primitives the codebase uses in
  `observe.py`. Keep the lookup best-effort and never raise — the
  runtime test below is the proof.
- Extend `status()` (lines 268-275) to include the visibility probe so
  the engine self-reports whether it actually has a visible window:
  ```
  {
    "started": bool,
    "url": str | None,
    "profile": str,
    "window": {
       "hwnd": int | None,         # 0 / None if not enumerable
       "title": str | None,
       "pid": int | None,
       "rect": [l,t,r,b] | None,
       "visible": bool,            # IsWindowVisible(hwnd) AND rect != 0,0,0,0
       "foreground": bool
    }
  }
  ```
  Implementation note: the browser module is platform-pure by design
  (no `import ctypes` at module top — `observe.py` does the ctypes
  import lazily). Add a thin `_probe_window()` helper that imports
  `ollie_hands.observe` only on `sys.platform == "win32"` and returns
  `{"visible": False, ...}` elsewhere. This keeps the lifecycle test
  (which uses `FakeCamoufox`, not real Camoufox) working unchanged.

### 2.2 Config / flag (optional but recommended)

Add a single env-var override so we never re-fight headless=True
incidents: in `_ensure_started()`, default `headless = (os.environ.get(
"OLLIE_HANDS_CAMOUFOX_HEADLESS", "false").lower() == "true")`. Setting
this env var to `true` re-enables headless for one specific debugging
purpose, but the **default and the box install must be `false`**.
`install-host.ps1` already provisions environment for the venv via the
service task — extend it (or add a separate
`scripts/setup-browser-headless.ps1`) to set
`OLLIE_HANDS_CAMOUFOX_HEADLESS=false` as a machine env var on the box
so that even a manually-launched venv interpreter inherits the right
default.

### 2.3 Deploy artefacts

- `scripts/install-host.ps1` — add a step that writes
  `[Environment]::SetEnvironmentVariable("OLLIE_HANDS_CAMOUFOX_HEADLESS",
  "false", "Machine")` so the new default survives across reboots and
  any manually-launched venv. Idempotent (`if existing value differs,
  set it`).
- `scripts/restart-host.ps1` — already kills `camoufox` + clears
  `parent.lock` (lines 39-40). After this change, restart-host must
  also kill any lingering `firefox.exe` children (Camoufox is Firefox;
  on headed launches Firefox spawns additional helper processes whose
  parent.lock lives in the profile dir). Add `Stop-Process -Name
  firefox -Force` alongside the existing `camoufox` kill.
- `scripts/setup-host-task.ps1` and `scripts/setup-engine-restart.ps1`
  — **no change**. They already launch the supervisor under the Source
  user / Interactive logon / Highest run level, which is what makes
  the headed window visible on session 1.
- `scripts/run-host.bat` / `scripts/supervisor.py` — **no change**. They
  already inherit the venv's session-1 attachment.
- `scripts/camoufox-login.py` — **no change**. It already does the
  right thing; the new default aligns the engine with this script.

### 2.4 Documentation

- `RUNBOOK.md`:
  - Add a short paragraph to the "Restart the engine" or a new
    "Browser headed mode" section saying: the engine now always launches
    Camoufox headed; verify by RDP'ing in and seeing a Firefox window
    titled with the engine's profile path; `browser.status().window.
    visible` should be `true`; if it isn't, the engine session has
    detached from the console — re-run
    `setup-host-session-power.ps1` and confirm the OllieHands task
    principal still says `LogonType Interactive, RunLevel Highest`.
  - Add the live HWND check (in §"Check health") so the operator can
    curl `act browser status` and read `window.visible=true`.
- `ollie-hands/README.md` — single-line addition in §"Capability ladder
  status" / L2 bullet clarifying the engine launches Camoufox **headed**
  in session 1 (not headless), so Tushar can see what Ollie sees and so
  CAPTCHA/CAPTCHA-vendor interactions on sites that block headless
  Firefox work without spoofing the surface.
- `Plans/graceful-questing-oasis.md` and `Plans/hands-completion.md`
  do not need code changes; if desired, a one-line note in
  `hands-completion.md` §"Where we actually are" / L2 row can flip
  "self-heal" → "self-heal, headed, session-1 attached" so the matrix
  reflects reality.

### 2.5 Tests

#### Pure unit test (`tests/test_browser_lifecycle.py`)
Add a new test, alongside the existing ones, that uses the same
`FakeCamoufox` to assert:

- The `FakeCamoufox` constructor received `headless=False`. (Add
  `headless` to the kwargs the fake records; assert `kwargs.get(
  "headless") is False`.)
- After the first `browser.goto(...)`, `browser.status()["window"]`
  reports `visible=True` and a non-zero `hwnd` (have FakeCamoufox
  inject a fake HWND into the status probe so the test stays
  cross-platform).

#### Integration test (`tests/test_browser_camoufox_integration.py`)
Add a new test (still gated on `sys.platform == "win32"` and on
`importlib.util.find_spec("camoufox")`) that uses the existing
loopback HTML form plus `browser.status()` to assert:

- `status()["started"] is True`
- `status()["url"] == form_url`
- `status()["window"]["visible"] is True`
- `status()["window"]["rect"]` is not `[0, 0, 0, 0]` (a Firefox
  window with zero dimensions is by definition not visible)
- `status()["window"]["title"]` mentions "Firefox" or the configured
  window title (so we know it's the browser, not some orphan Chrome
  window from another tool)

#### Direct Win32 visibility test (new file
`tests/test_browser_window_attached.py`)
Gate on `sys.platform == "win32"` (skipped elsewhere). Reuses the
loopback form pattern from the integration test, then directly:

- Imports `ollie_hands.observe` and calls `window_list()`.
- Asserts there is at least one window whose `process` endswith
  `firefox.exe` AND whose `rect` is non-degenerate AND
  `IsWindowVisible == True`.
- Asserts that window's pid matches the Camoufox subprocess pid (use
  `browser.status()["window"]["pid"]` and cross-check via
  `psutil.Process(pid).name() == "firefox.exe"` — add `psutil` to
  `requirements.txt` only if not already pulled in transitively; if
  not, use the existing `_process_name(pid)` helper from `observe.py`).
- This is the **load-bearing** test that proves the window is actually
  attached to a rendering desktop, not just `headless=False`.

#### Restart/deploy test (new PowerShell smoke
`scripts/verify-browser-headed.ps1`)
Run on the box after deploy:

1. `powershell -ExecutionPolicy Bypass -File scripts\restart-host.ps1`
2. Wait for engine to bind :3200 (the script already polls 40s).
3. Call the MCP `act browser status` over HTTP via the existing bearer
   token (curl is enough) — assert `window.visible == true`,
   `window.rect != 0,0,0,0`, `window.pid` non-zero.
4. Also run `Get-Process -Id <pid>` and assert
   `SessionId -eq 1` — this is the unique proof that the window is
   attached to the Source interactive session, not session 0.
5. `Get-Process firefox` should be non-empty AND each instance should
   have `SessionId -eq 1`.
6. RDP in (or use `mstsc /v:127.0.0.1`) and eyeball: a Firefox window
   is on the desktop with the profile dir visible in `about:profiles`.

#### Live HWND / console-attached proofs (in `RUNBOOK.md` / new
`§ Live verification`)
After deploy, the owner (or subagent) must capture and paste into the
plan-as-evidence:

- `act browser status` output (proves the engine self-reports visible).
- `Get-Process -Id <pid>` output showing `SessionId=1` (proves the
  process is on the Source console, not session 0).
- A screenshot via `observe` showing the Firefox window with a
  recognizable title and rect — this is the proof Ollie can SEE
  what the engine sees, satisfying the "visibly attached" clause.
- `window_list()` JSON proving firefox.exe is in the
  `IsWindowVisible` set (proves the Win32 enumeration agrees).

If any of those four artifacts is missing, the change is **not
verified** — `headless=False` was flipped but the engine still ran in
session 0, or the window was minimised, or the parent.lock was stale
from a previous headless run.

---

## 3. Exact files touched (count: 6)

| File | Change |
|---|---|
| `ollie-hands/ollie_hands/browser.py` | `headless=True` → `headless=False`; env-var override; `status()` adds `window` block; helper probes via `observe.window_list()` on Windows only |
| `ollie-hands/tests/test_browser_lifecycle.py` | New test asserting `headless=False` kwarg + visible window in `status()` |
| `ollie-hands/tests/test_browser_camoufox_integration.py` | New test asserting `status()["window"]["visible"]` and non-zero rect |
| `ollie-hands/tests/test_browser_window_attached.py` (new) | Direct Win32 `EnumWindows` + `IsWindowVisible` + `firefox.exe` cross-check |
| `ollie-hands/scripts/install-host.ps1` | Set `OLLIE_HANDS_CAMOUFOX_HEADLESS=false` as Machine env var |
| `ollie-hands/scripts/restart-host.ps1` | `Stop-Process -Name firefox -Force` (in addition to existing `camoufox`) |
| `RUNBOOK.md` | New §"Browser headed mode / live verification" + curl `act browser status` snippet |
| `ollie-hands/README.md` | One-line L2 note that engine is headed + session-1 attached |
| `scripts/verify-browser-headed.ps1` (new) | PowerShell post-deploy gate: restart + curl status + assert SessionId=1 |

(The Plan documents are optional doc-only add-ons, not load-bearing.)

---

## 4. Risk + rollback

- **Risk: popup noise.** Headed Firefox on the Source desktop will be
  visible to anyone at the console or RDP'd in. Acceptable per the
  master plan (Option A, dedicated box; Tushar explicitly approved
  Ollie owning the box). The window can be minimised if needed by
  setting `--start-minimized` in `args` (NOT recommended by default —
  minimised windows are not enumerable as `IsWindowVisible=True` in
  every Windows version; verify first).
- **Risk: shared profile lock.** Firefox profiles are single-writer.
  `camoufox-login.py` already documents "do NOT run while the engine
  is mid browser task." The headed engine change does NOT alter this
  invariant; if anything it makes it more visible. No code change
  needed; the existing comment + `restart-host.ps1` parent.lock
  clearing is sufficient.
- **Risk: CAPTCHA-vendor interactions that detect Firefox + visible
  window.** Some anti-bot checks add friction when the browser is
  visibly headed but the OS-level canvas does not receive synthetic
  input. Mitigated automatically: this engine's design is "act like a
  human" — `SendInput` injection + a real visible window is the goal.
  If a site degrades the experience specifically because of visibility
  (rare; visibility is the normal-user state), the env var allows
  re-flips for debugging.
- **Rollback.** Flip the env var back to `true` (or remove the line
  from `install-host.ps1`), `restart-host.ps1` once, done. The
  `headless=False` default is also overridable at the call site by
  anyone forking `browser.py`. No schema, audit, or policy data
  changes — pure runtime + a Machine env var.

---

## 5. Done-when checklist

- [ ] `browser.py` ships with `headless=False` default and env-var override.
- [ ] `status()` returns `window.visible`, `window.hwnd`, `window.pid`,
      `window.rect`, `window.foreground`.
- [ ] `install-host.ps1` sets `OLLIE_HANDS_CAMOUFOX_HEADLESS=false`
      Machine-wide; idempotent.
- [ ] `restart-host.ps1` kills `firefox.exe` alongside `camoufox`.
- [ ] `tests/test_browser_lifecycle.py` has the `headless=False`
      kwarg + `status()["window"]["visible"]` assertions.
- [ ] `tests/test_browser_camoufox_integration.py` asserts visible +
      non-zero rect on real Camoufox (still skipped on non-Windows).
- [ ] `tests/test_browser_window_attached.py` (new) passes on the box:
      Win32 `EnumWindows` + `IsWindowVisible` finds a `firefox.exe`
      window with non-degenerate rect.
- [ ] `scripts/verify-browser-headed.ps1` post-deploy green:
      `SessionId=1` for both engine and `firefox.exe`; `window.visible
      == true`.
- [ ] RUNBOOK has the curl / RDP-in live verification recipe; owner
      pastes the `act browser status` + `Get-Process -Id <pid>` +
      `observe` screenshot evidence into this plan file as the
      proof-of-attached-runtime.