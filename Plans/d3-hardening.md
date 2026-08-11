# D3 Hardening Plan — close the vault exfil channels (structural, not whack-a-mole)

> Status: **PLAN — nothing executed yet.** Grounded in: the security review
> (CRITICAL-1/2/3, HIGH-1/2), live verification on the box, a code read of
> `shell.py`/`vault.py`/`policy.py`, box recon (2026-06-15), and a Windows
> privilege-separation mechanism study.

## Problem (recap, verified live)
The D3 vault's audit-masking is solid, but the brain can still **obtain** a
stored secret through auto-tier side channels:
- **CRITICAL-1** — brain-controlled `cwd` + bare relative read (`cwd=…\vault`,
  `Get-Content x.bin`) → classified T0 AUTO → returns DPAPI ciphertext.
- **CRITICAL-2** — `[…ProtectedData]::Unprotect(...)` only NOTIFY → decrypts in
  process (brain shell runs as `Source`, the vault's own principal).
- **CRITICAL-3** — uia `type_text` clipboard fallback leaves the secret on the
  clipboard; `clipboard read` is T0 AUTO.
- **HIGH-1** — after typing a secret into a field, `uia get_text` (T0) reads it.
- **HIGH-2** — audit-dir relocation defeats the hardcoded audit block.

**Root cause:** the brain's L0 shell runs as `Source` (elevated) — the *same
principal* that owns + can DPAPI-decrypt the vault. String-based policy can't fix
this (verified: the cwd block is bypassed by `/` vs `\` and `..`). The real close
is **OS-level privilege separation of the shell**.

## Box facts (recon 2026-06-15)
- Win11 (build 26300). Engine = `OllieHands` scheduled task, user **`Source`**,
  LogonType Interactive, RunLevel Highest, session 1. Engine spawns
  `powershell.exe` via `subprocess.run` → **runs as `Source`**.
- Elevated context has `SeImpersonate` + `SeIncreaseQuota` but **NOT
  `SeAssignPrimaryToken`/`SeTcb`** → `CreateProcessAsUser` likely fails;
  **`CreateProcessWithLogonW`** (needs only the target password) is the robust path.
- **Vault ACL is wide open:** `BUILTIN\Users:(RX)` — any user can *read* the
  `.bin` files (DPAPI still blocks a *different user* from decrypting). Must tighten.
- Vault uses DPAPI user-scope, **no `pOptionalEntropy`** (cheap extra layer
  available). Secrets dir `C:\ProgramData\ollie-hands\vault`.
- Pre-existing local users: `CodexSandboxOffline`, `CodexSandboxOnline`, `Source`
  (will create a dedicated `OllieShell` rather than repurpose unknowns).
- **The earlier "deploy failure" was a stale process** — the hardening code IS on
  disk (DPAPI block + taint present, mtime 21:45) but the running engine started
  21:27, *before* the deploy, and was never restarted after. Fix = deploy→restart
  ordering + assert `engine start-time > max(file mtime)`.

## Mechanism decision (from the research)
Only a **separate user account + NTFS deny ACE** defeats *both* the file read
*and* DPAPI user-scope decryption. Restricted tokens / low-IL / AppContainer all
keep the same user's DPAPI master key → still decrypt. Confirmed: a different
user (different SID + logon session) gets a different master key and
`CryptUnprotectData` fails on `Source`'s blobs.

## The architecture (the close)
1. **De-privilege the L0 shell** — brain PowerShell runs as standard user
   **`OllieShell`** (not `Source`), spawned via **`CreateProcessWithLogonW`**.
2. **Tighten vault + audit ACLs** — remove inherited `BUILTIN\Users`; grant only
   `Source`/SYSTEM/Admins; explicit **Deny-read** for `OllieShell`.
3. **DPAPI different-user** — `OllieShell ≠ Source` → can't decrypt even with the
   ciphertext (research-confirmed).
4. **In-process readback** (`get_text`, clipboard) — closed by the already-coded
   **taint-tracking** + **no-clipboard-for-secrets** (engine-internal, unaffected
   by the shell user).
5. **Defense-in-depth pragmatic patches** — DPAPI-text block, cwd block
   (normalized), atomic vault, `_canon` JSON-native, test rewrites.

Surgical: changes are confined to `shell.py` (spawn), ACLs, a setup script, and
small policy/test fixes. UIA (L1), pixels (L3), grounding, act-scripts unchanged.
Bonus: the brain's shell stops being elevated `Source` — a broad win beyond the vault.

## Phasing

### Phase H1 — Land the solid pragmatic patches ✅ DONE 2026-06-16
**Verified live on the fresh engine (pid 5352, FRESH=True: start-time > all deployed file mtimes):**
- ✅ **CRITICAL-1 string-bypass closed** — cwd vault read BLOCKED across backslash,
  forward-slash, and dot-dot addressings (`_norm_path` canonicalizes); benign reads
  stay AUTO (no over-block; the OllieShell `work` sibling dir is not blocked).
- ✅ **CRITICAL-2 closed** — `ProtectedData::Unprotect` BLOCKED live.
- ✅ Pure suites green incl. a new path-normalization regression test (commit 5e3d4d2).
- ◑ **CRITICAL-3 (clipboard) + HIGH-1 (get_text taint)** — unit-tested (pytest) +
  deployed-fresh + correct by inspection (engine.py taint set/refuse paths). Live
  behavioral confirmation deferred to H2's harness (Win11 Notepad UIA targeting is
  flaky/hangs — a test-harness issue, not a security gap; engine stays healthy).
- ✅ **HIGH-2** — audit/vault blocked dirs derived from config (`set_blocked_dirs`),
  unit-tested for a relocated audit dir.
- **Remaining for full close:** CRITICAL-1 *file read* (copy-then-read still works
  via shell-as-Source) → **H2 privilege separation** is the real close.

---
(original H1 plan below)

### Phase H1 — Land the solid pragmatic patches (fast, low-risk) — closes 3 of 4 channels
The hardening code is already on disk; this is mostly *deploy correctly + verify*.
- Deploy ALL changed files; run `restart-host.ps1`; **assert engine start-time >
  file mtimes** (the check that would have caught the stale-process bug).
- Fix the **cwd-block normalization** (`os.path.normcase(os.path.normpath(...))`
  both sides) so `/` and `..` no longer bypass — defense-in-depth.
- Confirm live (coordinator-run): `ProtectedData::Unprotect` → BLOCKED; clipboard
  empty after secret type; `get_text` on tainted target → refused; cwd block fires
  on `/`-and-`..` variants. Re-run pure suites on-host.
- After H1: CRITICAL-2, CRITICAL-3, HIGH-1, HIGH-2 closed. **CRITICAL-1 (file
  read) only partially mitigated** (cwd block raises the bar; copy-then-read still
  works) → H2 is the real close.

### Phase H2 — Privilege separation ✅ DONE 2026-06-16 (the real CRITICAL-1/2 close)
**Implemented + verified live (engine pid 19144):**
- `scripts/setup-shell-user.ps1` provisioned `OllieShell` (standard, never-admin),
  cred stored DPAPI-under-Source, **vault/audit ACLs tightened** (the open
  `BUILTIN\Users:(RX)` removed; `OllieShell` deny-read; engine-user/SYSTEM/Admins
  full), neutral `work` dir, seclogon available.
- `shell.py` spawns the brain's PowerShell as `OllieShell` via
  `CreateProcessWithLogonW` (output captured via `work/` files — handle
  inheritance doesn't survive the secondary-logon boundary; PS 5.1 UTF-16 decoded);
  **fail-closed**; gated on `shelluser.cred` (dev/mac keep the legacy path).
- **Live proof:** shell `whoami` = `mbd25-30\ollieshell`; a **policy-EVADING**
  (string-concat path) direct read, copy-out, and dir-list of the vault were all
  **ACL-DENIED** (no leak) — so even when the brain dodges policy, the OS denies
  it. Engine (Source) still resolves `secret_ref` (browser fill ok). Legit tasks
  (cmdlets, read `C:\OllieWork`, write `work`, web 200, TEMP set) all work. Temp
  files cleaned up (work dir empty after).
- **Rollback proven-simple:** remove `shelluser.cred` → engine reverts to the
  legacy in-process shell.
- **Outstanding (minor):** the H1 `get_text`/clipboard taint (HIGH-1) live
  behavioral check is still unit-tested-only — Win11 Notepad UIA is too flaky to
  drive a secret-type for the live observation; revisit with a reliable UIA target.

(original H2 plan below)

### Phase H2 — Privilege separation (the real CRITICAL-1/2 close)
- **`scripts/setup-shell-user.ps1`** (one-time, owner-run, idempotent):
  - Create `OllieShell` standard user, strong random password, `SeBatchLogonRight`
    only, **not** in Administrators.
  - Store password DPAPI-encrypted under `Source` at
    `C:\ProgramData\ollie-hands\shelluser.cred`, ACL'd Source/SYSTEM-only +
    Deny OllieShell.
  - Tighten ACLs on `vault` + `audit`: strip inherited Users, grant
    Source/SYSTEM/Admins, **Deny-read OllieShell**.
  - Create neutral working dir `C:\ProgramData\ollie-hands\work` (OllieShell RW).
  - Ensure `seclogon` service is running (required by `CreateProcessWithLogonW`).
- **`shell.py`** — spawn as `OllieShell` via `CreateProcessWithLogonW` (ctypes):
  - Decrypt the OllieShell password (as `Source`) from `shelluser.cred`.
  - Redirect child stdout/stderr to temp files in `work` (robust capture across
    the logon boundary — handle-inheritance is fiddly), wait w/ timeout + kill
    tree, read the files back as `Source`, return exit code via
    `GetExitCodeProcess`.
  - Set `TEMP`/`TMP` → `work` in the env block (pitfall: profile not loaded);
    `workingDir = work`; `CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT`;
    `lpDesktop = NULL` (non-UI).
  - **FAIL CLOSED**: if the de-priv spawn fails, return an error — never silently
    fall back to a `Source` shell (that reopens the hole). Gated on
    `shelluser.cred` existing (so dev/mac + pre-setup stays on legacy path).
- **Live verify (the real proof):** as the brain (MCP `act` shell), attempt to
  read a stored secret's `.bin` via (a) absolute path, (b) relative cwd, (c)
  forward-slash, (d) **copy-out-then-read** → ALL must return access-denied
  (OllieShell denied). Attempt DPAPI decrypt → fails (different user) + blocked.
  Confirm the engine still resolves `secret_ref` and types correctly (function
  preserved). Confirm a *legit* OllieShell shell task still works (read
  `C:\OllieWork`, write in `work`).

### Phase H3 — Optional extra hardening (after H2)
- Add engine-held `pOptionalEntropy` to DPAPI (kills any residual same-user read).
- Process Monitor pass to catch legit brain-shell tasks that broke under OllieShell.

## Test matrix
- **Pure** (mac): existing suites + new cwd-normalization tests (`/`, `..`,
  uppercase, trailing-slash all BLOCKED). The masking test must call the real
  `engine._audit_args`/`_preview` (the strawman is being replaced).
- **Live** (box, coordinator-verified): the H1 + H2 checks above. The headline is
  the H2 "brain shell cannot read the vault by any addressing" proof.

## Deploy + verify procedure (must not repeat the stale-process bug)
1. scp ALL changed files to `C:\ollie-hands\...`.
2. Run `setup-shell-user.ps1` (H2, one-time).
3. `restart-host.ps1`.
4. **Assert** `(engine proc start-time) > max(deployed file mtime)` before trusting any live result.
5. Run live verification; coordinator independently re-runs the CRITICAL bypasses.

## Rollback / safety
- ACL + user creation are reversible (remove Deny ACE, `Remove-LocalUser`).
- `shell.py` keeps the legacy `Source` path behind the `shelluser.cred` gate, but
  reverting to it **reopens CRITICAL-1/2** — so rollback = investigate, not a
  silent fall-back.
- **No real credentials in the vault until H2 lands** (current vault is empty;
  the only secret was a deleted test one → no live exposure today).

## Risks / open items
- **Output capture across the logon boundary** — temp-file redirection in `work`
  (chosen for robustness); the fiddliest implementation bit.
- **Legit brain-shell tasks needing Source/admin** — de-privileging may break
  some; enumerate + Process-Monitor on first run (research pitfall #4). Main
  functional risk.
- **`seclogon` disabled** → `CreateProcessWithLogonW` fails; setup ensures it runs.
- **Per-command logon overhead** (~tens of ms) — acceptable; per-call (no token cache) for simplicity/safety.

## Ownership (given grok's 3 soft-passes on verification)
- Implementation may be delegated, but **the coordinator owns live verification of
  every security claim** (grok does not grade its own security homework). H2's
  spawn code is the delicate part — implement carefully + verify each bypass live.
