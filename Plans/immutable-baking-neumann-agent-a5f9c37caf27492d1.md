# Hands Telegram approval incident — live vs repo byte comparison

**Date:** 2026-07-16
**Mode:** Read-only evidence gathering, no edits / restarts / messages / API calls.
**Box access:** SSH to `source@<TAILSCALE_IP>` (OpenSSH on Windows → WSL bridge via `wsl.exe -d OpenClawGateway`).
**Primary repo:** `./`
**Active Hands path:** `C:\ollie-hands\ollie_hands\` (Windows host running `python -m ollie_hands.server`)
**Active plugin path (WSL):** `/home/openclaw/.openclaw/plugins/ollie-wa-approval/` (OpenClawGateway distro)

---

## 1. Active processes (loaded modules)

Captured via `Get-WmiObject Win32_Process -Filter "Name = 'python.exe'"`:

| PID  | Command line                                                                                       | Started           |
|------|----------------------------------------------------------------------------------------------------|-------------------|
| 7100 | `venv\Scripts\python.exe  scripts\supervisor.py`                                                   | 7/15/2026 9:29 PM |
| 17304| `venv\Scripts\python.exe  scripts\supervisor.py`                                                   | 7/15/2026 9:29 PM |
| 14916| `C:\ollie-hands\venv\Scripts\python.exe -m ollie_hands.server`                                     | 7/15/2026 9:29 PM |
| 20416| `C:\ollie-hands\venv\Scripts\python.exe -m ollie_hands.server`                                     | 7/15/2026 9:29 PM |

Server process CWD + module source = `C:\ollie-hands\venv\...\ollie_hands\server.py` → loads `C:\ollie-hands\ollie_hands\consent.py` (the **active** 20,927-byte file, not the repo copy).

OpenClawGateway runs as `openclaw-gateway.service` (systemd --user):
`ExecStart=/home/openclaw/.openclaw/tools/node-v22.22.0/bin/node /home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/index.js gateway --port 18789`
Plugins loaded from `~/.openclaw/plugins/` per `~/.openclaw/openclaw.json`; the `ollie-wa-approval` entry points to `/home/openclaw/.openclaw/plugins/ollie-wa-approval/` (the live `index.js`, 52,367 B).

---

## 2. Bytes/hashes table (active live vs repo)

### 2a. Windows Hands (`C:\ollie-hands\ollie_hands\` vs repo `ollie-hands/ollie_hands/`)

| File          | Live path                                       | Live size | Live SHA256                                                          | Repo path                                                                  | Repo size | Repo SHA256                                                          | Match |
|---------------|------------------------------------------------|-----------|----------------------------------------------------------------------|----------------------------------------------------------------------------|-----------|----------------------------------------------------------------------|-------|
| consent.py    | `C:\ollie-hands\ollie_hands\consent.py`        | 20927     | `79CB8622D9D3B587F3AB870AF568FD617428C373C872A0FB4B5258B03805E790` | `./ollie-hands/ollie_hands/consent.py`  | (483 L)   | `8635831d7c79d708760a3a926951c9e91c4097e7d28c2c1070d62e57944d867b` | NO    |
| server.py     | `C:\ollie-hands\ollie_hands\server.py`         | 13379     | `0AFDB056B9B045470DA00246AFECCE79409C76BE3ADDED603088EADAB2D46D48` | `./ollie-hands/ollie_hands/server.py`   | (415 L)   | `7525cbd5c48249b1271d81f36a0007ffe3af3245c54b98fd70260ba635e7d1f3` | NO    |
| auth.py       | `C:\ollie-hands\ollie_hands\auth.py`           | 1560      | `8267373726426D8D317CC1D80E91F3411B6187DF37940E1BC006F587DD2C0AE8` | `./ollie-hands/ollie_hands/auth.py`     | 1560      | `8267373726426d8d317cc1d80e91f3411b6187df37940e1bc006f587dd2c0ae8` | YES   |
| config.py     | `C:\ollie-hands\ollie_hands\config.py`         | 4444      | `3778BAC3742B579EC25761A4F3AD15B427D9155469AE9CA8D6C2520F92F9B61E` | `./ollie-hands/ollie_hands/config.py`   | 4444      | `3778bac3742b579ec25761a4f3ad15b427d9155469ae9ca8d6c2520f92f9b61e` | YES   |
| executor.py   | `C:\ollie-hands\ollie_hands\executor.py`       | 26356     | `6220E6D2E040309FFE698FDCA587636239D28A8C518B3F4D73DB410E547FC4CF` | `./ollie-hands/ollie_hands/executor.py` | (493 L)   | `1b5dc4bba1981472c762632166626a59160b0d5df374a6e94a649cfa1ba2c104` | NO    |
| policy.py     | `C:\ollie-hands\ollie_hands\policy.py`         | 16298     | `3A1EF759BA1C94A37DB1159B790698AE1877464350434A539F8558D0C651181B` | `./ollie-hands/ollie_hands/policy.py`   | (430 L)   | `232991bd96b223b3754a5f5dee34c223e44eb4e9495c0e07aad2d17e157b3d64` | NO    |
| engine.py     | `C:\ollie-hands\ollie_hands\engine.py`         | 16314     | `E59F487AA4D19CB648570BD496B8A1457399138305C5A315833C9CDAF51E92B0` | `./ollie-hands/ollie_hands/engine.py`   | (380 L)   | `4f4c50a8dd31b62f1b40e9a7141a0edd14c108aeb8f5f29c49d6da21988e3979` | NO    |
| browser.py    | `C:\ollie-hands\ollie_hands\browser.py`        | 8530      | `E4AAB086EC82C6D2121BB8A9E7596A71792A5DB7CCFB63C625863750E42C257B` | `./ollie-hands/ollie_hands/browser.py`  | (272 L)   | `aec56fdd80b731020bf4b58bd62dda093fdfc07d70deb01511e95867fafbe754` | NO    |
| actscript.py  | `C:\ollie-hands\ollie_hands\actscript.py`      | 12883     | `8885161C05A6D4D3FBBC8CD2BF2CDD32CC9A00F58E6DAA07194F1E2F976AE49D` | `./ollie-hands/ollie_hands/actscript.py`| (308 L)   | `16124f152d4ca69a9cb61e0d502f368c502075a004902e433318429271745a1d` | NO    |
| grants.py     | `C:\ollie-hands\ollie_hands\grants.py`         | 10063     | `9778645266CD39CFCB3B61F874BBA7FF9AE3534FCD054EBA540B951DC166F872` | `./ollie-hands/ollie_hands/grants.py`   | (282 L)   | `acb2151f686864e72a2d1710bd7fae9eb6982356990bdbba50a4313e410c9547` | NO    |

Last-write timestamps on the live files (Windows):
- consent.py — 7/15/2026 9:23:19 PM
- server.py  — 7/15/2026 7:22:54 PM
- engine.py  — 7/15/2026 12:51:21 AM
- (others)   — 7/11–7/12/2026

### 2b. WSL plugin (`/home/openclaw/.openclaw/plugins/ollie-wa-approval/` vs repo `openclaw-ollie-wa-approval/`)

| File                | Live size | Live SHA256                                                          | Repo size | Repo SHA256                                                          | Match |
|---------------------|-----------|----------------------------------------------------------------------|-----------|----------------------------------------------------------------------|-------|
| index.js            | 52367     | `3d7a895b63e851c57f1775b2c6ab36a70fc005a2ca715cd198075ac27e515114` | 40585     | `c412c2c996d831ed321ebe2b06edda426f43cdba2e6192120f74a99c0d9f7453` | NO    |
| openclaw.plugin.json| 3954      | `80f5e1eda75cdc89494f1ef908ca95b0f45ee33cc871ae2205425b8633b6ba78` | 3365      | `39502540393fbf1529dc361bec9c5b82c14e197fff53c803c1d4b6815aeef4fd` | NO    |
| package.json        | 845       | `920ff81fc5d774413a8ab6577bcf69c72d0d56a6235e9faf3af32ebbb4371ec2` | 786       | `7112f23123d773a4c6a7a407d8e07fb3b7ddf5e9a3f9ea24f17a8b0e301ad77e` | NO    |
| approval-command.js | 1442      | `be0c232ce21df40617d31f990ac8d4fd6e9cf492272018ffe3246be04a795b17` | 1442      | `be0c232ce21df40617d31f990ac8d4fd6e9cf492272018ffe3246be04a795b17` | YES   |

Live plugin dir LastWrite timestamps (WSL `ls -la`):
- index.js            — Jul 15 21:26 (latest of the live version)
- openclaw.plugin.json— Jul 12 20:59
- approval-command.js — Jul 12 20:59
- package.json        — Jul 12 20:59 (846 B; note the repo copy is 786 B and differs in hash)

Live directory also contains backups: `index.js.pre-deploy-20260715T192300Z` (48604 B, Jul 15 21:23), `index.js.live-20260714T150636` (48080 B, Jul 14 15:06), `index.js.bak-pre-deploy-20260712T141428Z` (35648 B, Jul 12 08:46), plus `pre-deploy-20260712T205003Z/` subdir. Active is the bare `index.js`.

Stale debug residue (unrelated to incident but visible): `/home/openclaw/live-index.js` (48604 B, Jul 15 21:20) and `/home/openclaw/live-index-patched.js` (1 B, Jul 15 21:24) — leftover dev artifacts.

### 2c. Plugin config (live WSL `openclaw.json`, `~/.openclaw/openclaw.json`)

`ollie-wa-approval` entry (live):
- `enabled: true`
- `ownerTelegramChatId: "<OWNER_TELEGRAM_CHAT_ID>"`
- `approvalsFile: "/home/openclaw/.openclaw/workspace/whatsapp-contacts.json"`
- `requestTimeoutMinutes: 60`
- `hookTimeoutMs: 10000`
- `handsConsentUrl: "http://<TAILSCALE_IP>:3200/consent"`
- `handsApprovalToken: "<BEARER_TOKEN_REDACTED>"`  *(treat as secret — exposed here only as evidence, not for reuse)*

Plugin manifest (`openclaw.plugin.json`) declares `id: "ollie-wa-approval"`, `activation.onStartup: false`, `enabledByDefault: false`. ConfigSchema is the same shape as repo but the manifest text differs (hash differs).

---

## 3. Semantic differences — what changes the approval flow

### 3a. H-ref creation

**Live `consent.py`** (file is 432 lines, 20,927 B; verbatim at `/tmp/consent_live.py`):
- `Consent.begin_confirm` (line 248) creates ref via `secrets.token_urlsafe(4).rstrip("=")` prefixed with `ref_prefix="H-"`. Raises `ValueError("approval requires an action/script digest")` if no digest — i.e., **H-ref is created**.
- `HANDS_REF_RE = re.compile(r"^H-[A-Za-z0-9_-]{1,61}$")` (line 34) is used by both `resolve()` and `consent_post_response()` to validate ref.
- `Consent.resolve` (line 371) atomically consumes the pending entry, sets `pc.approved = approve`, signals `pc.event`.
- `consent_post_response` (line 420) takes `{approve: bool, ref: str}`, validates ref via `HANDS_REF_RE.fullmatch(ref)`, then calls `consent_obj.resolve(...)`.

**Repo `consent.py`** (483 lines): has the same `HANDS_REF_RE` and similar `begin_confirm`/`resolve`/`consent_post_response` surface, but with several renamed parameters (e.g. repo `await_confirm` accepts `pending.expires_at - time.monotonic()` directly, live uses `self.cfg.confirm_timeout`; repo `confirm` has `script_hash` as positional, live has it keyword-only; repo's `confirm` returns `tuple[bool, str]`, live also `tuple[bool, str]` but with a different code path). Both versions create H-refs; the claim that live is legacy cannot be inferred from these files alone (see §4).

### 3b. Inline keyboard send (Telegram)

**Live `consent.py`** (lines 174–198, 267–352):
- `_build_approval_callback(ref, approve)` builds `ollie_approval:v1:a:<ref>` or `:d:<ref>` with `APPROVAL_CALLBACK_MAX_BYTES = 64`; returns `""` if too long.
- `_build_approval_keyboard(ref)` returns `{ "inline_keyboard": [[ {"text": "Approve", "callback_data": approve_cb}, {"text": "Deny", "callback_data": deny_cb} ]] }` or `None` when ref too long.
- `deliver_pending(ref)` first tries keyboarded send via `_send_with_result(preview, reply_markup=kb)`. On `definitive_rejection` (Telegram 4xx with reply_markup/inline keyboard/callback_data signal), it falls back to a plain-text send (`_send(preview)`). On ambiguous failure (network/5xx), it does NOT retry and does NOT auto-approve. Audit events distinguish `keyboard_accepted`, `keyboard_rejected`, `plain_accepted`, `plain_no_keyboard`, `error`.

The live `_send_with_result` (lines 99–172) implements the deterministic 4xx classification used by the keyboard-fallback logic, inspecting `description` and `parameters` for `reply_markup`, `inline keyboard`, `callback_data`, `inline_keyboard`.

**Repo `consent.py`**: structurally similar — has `_build_approval_callback`, `_build_approval_keyboard`, `deliver_pending`, `_send_with_result` with the same markup-rejection classification — but the audit-line shape differs (e.g. live uses `args={"ref": ...}` and status names like `keyboard_accepted`; repo uses different status names). Both versions DO create and send inline keyboards with H-refs.

### 3c. Pending state

Live `_pending` is `dict[str, PendingConsent]` guarded by `_lock = threading.Lock()`; `pending_inventory()` (line 404) returns rows with `ref`, `preview[:300]`, `expires_in` (no digests exposed). Repo has the same structure. Both expose the same inventory to the plugin over `GET /consent`.

### 3d. /consent payload + auth

Live `server.py` (lines 273–307):
```python
async def consent_endpoint(request):
    if request.method == "GET":
        return consent_mod.consent_inventory_response(consent)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    return consent_mod.consent_post_response(
        payload, consent,
        client_id=(request.client.host if request.client else "unknown"),
    )

def main():
    ...
    token = cfg.bearer_token()             # MCP bearer
    approval_token = cfg.approval_token()   # /consent-only credential
    if hmac.compare_digest(token, approval_token):
        raise RuntimeError("approval token must differ from MCP bearer token")
    inner = mcp.streamable_http_app()
    inner.router.routes.append(
        Route("/consent", consent_endpoint, methods=["GET", "POST"]))
    app = BearerMiddleware(inner, token, approval_token, audit=audit)
```

Live `auth.py` (40 lines, sha match with repo):
- `BearerMiddleware.__call__` reads `authorization` header and picks expected token based on path:
  - `path.rstrip("/") == "/consent"` → expects `Bearer <approval_token>`
  - otherwise → expects `Bearer <bearer_token>` (MCP)
- On mismatch: `audit.event("auth", status="denied", ...)` + `401 unauthorized`.
- This is the ONLY auth boundary for /consent; the MCP bearer cannot ride on it.

Repo `auth.py` and `server.py` are structurally identical for this contract (same hashes for `auth.py`; `server.py` differs in unrelated audit/log strings).

### 3e. Callback parsing + owner authorization (plugin side)

Live `index.js` (1124 lines) — relevant functions:

- `parseApprovalCallback(ctxLike)` (line 762):
  - Requires `channel === "telegram"` (else `{handled:false}`).
  - `data = ctxLike.payload?.data ?? ctxLike.callback_data` (string).
  - `parts = data.split(":")` must have length 4.
  - `parts[0] === "ollie_approval"` AND `parts[1] === "v1"` (else `{handled:false}`).
  - `dec ∈ {"a","d"}`, `ref = parts[3]`, validated against `/^H-[A-Za-z0-9_-]{1,61}$/`.
  - Returns `{handled:true, malformed:true}` on parse-shape failure (so plugin can answer the button), or `{handled:true, ref, approve}`.

- `isAuthorizedOwnerCallback(cfg, ctxLike)` (line 793) — ALL four must hold:
  1. `cfg.ownerTelegramChatId` truthy.
  2. `senderId === ownerId` (exact string).
  3. `chatId === ownerId` (exact string).
  4. `ctxLike.auth.isAuthorizedSender === true` (set by gateway core).
  Otherwise returns `false` (plugin never calls Hands).

- `handleApprovalCallback(api, ctxLike)` (line 883):
  1. Parses; if not ours → return.
  2. If malformed → reply "❗ Invalid approval button." + log.
  3. If unauthorized → reply "❗ Not authorized." + log.
  4. Calls `postHandsConsent(api, parsed.ref, parsed.approve)` which POSTs `{ref, approve}` to `cfg.handsConsentUrl` with `Authorization: Bearer <handsApprovalToken>` and 5–10s timeout.
  5. On 200 → `editMessage` "✅ Approved H-XXX" / "⛔ Denied H-XXX"; else fallback `reply` text.
  6. Emits bounded correlation log line `cb {JSON}` via `logCallback` (line 859) which sanitizes Bearer/JWT/URLs/hex.

- `routeOwnerApproval(api, cmd)` (line 695): for typed-text "approve H-XXXX" replies. Lists `listHandsPending(api)` + `listContactPending(api)`, picks the matching ref, calls `handleHandsApproval` → `postHandsConsent`.

Repo `index.js` (40,585 B) also has `parseApprovalCallback`, `isAuthorizedOwnerCallback`, `routeOwnerApproval`, but the implementations differ in size and content. The repo's `parseApprovalCallback` line range is shorter; both versions filter on `ollie_approval:v1`. The repo version is missing some of the live defensive checks (e.g., `auth.isAuthorizedSender === true` enforcement, the `ctxLike.payload` nested form, the bounded fetch timeout via `AbortController`, the sanitized correlation log). Semantic differences are real but the repo still parses H-ref callbacks correctly.

---

## 4. Verdict on the claim "live consent.py is legacy and cannot create H refs"

**Not proven.** Evidence against:
1. Active `python -m ollie_hands.server` is invoked from `C:\ollie-hands\venv\...\python.exe` and resolves `ollie_hands` from the directory holding the active `consent.py` (the loader uses `__init__.py` next to the imported modules). The active consent.py is `C:\ollie-hands\ollie_hands\consent.py` (sha `79CB86...`, 20,927 B, modified 7/15/2026 9:23:19 PM) — NOT the repo copy.
2. The active consent.py contains the full H-ref creation path: `HANDS_REF_RE`, `Consent.begin_confirm` (creates `f"H-{secrets.token_urlsafe(4).rstrip('=')}"`), `Consent.resolve` (atomically consumes the pending), `consent_post_response` (validates + resolves), `_build_approval_callback` + `_build_approval_keyboard` + `deliver_pending` (sends inline keyboard via Telegram sendMessage).
3. The repo `consent.py` has the same H-ref machinery (slightly different audit-log/status names, same `HANDS_REF_RE`).
4. Both files were modified in the same 7/11–7/15 window. The active file is **newer** (LastWriteTime 7/15/2026 9:23 PM) than the repo working copy.
5. The claim "legacy and cannot create H refs" is contradicted by direct reading of the active `consent.py`. If the claim is based on a stale cached Python file or an old `__pycache__`, the user's own memory note (`project_ollie_hands_restart.md`) flags this as a known footgun: `__pycache__` at `C:\ollie-hands\ollie_hands\__pycache__` was modified 7/15/2026 9:29:26 PM, after the .py rewrite (9:23:19 PM), so .pyc should be in sync.

What I can NOT prove from this comparison alone: whether an incident-triggering divergence is at the .pyc layer (loader picked a stale module) or at the process layer (a different process is bound to :3200 than the one I sampled). The two `ollie_hands.server` PIDs (14916, 20416) started together; both load the same `C:\ollie-hands\ollie_hands\server.py` per `python -m ollie_hands.server`. There is no evidence of a second, older Python listener on :3200.

---

## 5. Repo vs live drift (post-deploy)

The repo is **not** in sync with the box on any Hands file other than `auth.py` and `config.py`. The local working tree is dirty (`M` status on all Hands files in `git status`) and the local hashes do not match the live hashes. This is consistent with `feedback_repo_box_drift.md` from memory: the deployed box is newer than the repo on the approval-critical paths.

Concretely, the live box has additional defensive logic that the repo copy does not have:
- consent.py live: explicit `_SendResult` type with `definitive_rejection` vs `ambiguous_failure` classification (audit `keyboard_rejected`/`keyboard_send_failed`).
- index.js live: explicit `auth.isAuthorizedSender === true` enforcement in `isAuthorizedOwnerCallback`, AbortController-bounded fetch in `postHandsConsent`, `sanitizeForLog` for correlation logs, `logCallback` correlation line per event, editMessage→reply fallback in `handleApprovalCallback`.
- index.js live: routes via `routeOwnerApproval` with inventory-style disambiguation when owner types just "approve" (no ref).
- These features are present in the repo too, but with smaller code volume; the live was likely re-deployed after additional hardening (timestamps Jul 14–Jul 15 on the backup chain: `.live-20260714T150636` 48080 B → `.pre-deploy-20260715T192300Z` 48604 B → live `index.js` 52367 B Jul 15 21:26).

---

## 6. Proven / Not proven — strict separation

### PROVEN
- Active Hands server processes: `python -m ollie_hands.server` from `C:\ollie-hands\venv\Scripts\python.exe`, started 7/15/2026 9:29 PM, loading `C:\ollie-hands\ollie_hands\consent.py` (sha `79CB86...`, 20,927 B, 7/15/2026 9:23:19 PM).
- The active `consent.py` implements the full H-ref creation, inline keyboard send, pending storage, /consent GET/POST resolution, callback-style ref validation, and owner-side bearer auth.
- `auth.py` is identical between live and repo (same sha) and enforces path-scoped bearer (approval token for `/consent`, MCP bearer for everything else).
- `config.py` is identical between live and repo (same sha); both reference `approval_token_file` resolved at boot from a path next to `config.json`.
- The active WSL plugin `/home/openclaw/.openclaw/plugins/ollie-wa-approval/index.js` (sha `3d7a895b...`, 52,367 B, 7/15/2026 21:26) is the loaded module for `ollie-wa-approval`.
- The live plugin parses Telegram callbacks with namespace `ollie_approval:v1` and validates `H-[A-Za-z0-9_-]{1,61}` refs identically to how the live consent.py accepts them at `/consent` POST.
- The live plugin enforces ALL four conditions (channel, senderId == ownerTelegramChatId, chatId == ownerTelegramChatId, ctx.auth.isAuthorizedSender === true) before calling the Hands `/consent` endpoint. Owner chat id configured as `<OWNER_TELEGRAM_CHAT_ID>`.
- Plugin-to-Hands transport: POST `http://<TAILSCALE_IP>:3200/consent` with `Authorization: Bearer <BEARER_TOKEN_REDACTED>` and JSON body `{ref, approve}`.
- Repo working tree differs from the live box on 8/10 Hands files and on 3/4 plugin files (only `auth.py`, `config.py`, and `approval-command.js` match byte-for-byte).
- The `/home/openclaw/.openclaw/openclaw.json` plugin entry is enabled and has `handsConsentUrl` + `handsApprovalToken` populated; `openclaw-gateway.service` is the systemd user unit running the gateway on port 18789.
- The local repo's `openclaw.plugin.json` is a DIFFERENT file from the live one (3365 B vs 3954 B; different sha) — any plugin-manifest field changes in the repo have not been deployed to the box.

### NOT PROVEN
- That the live `consent.py` is "legacy and cannot create H refs". Direct reading of the live file contradicts this; if a sub-claim was about a different path (e.g. a stale `.pyc`, a different process bound to :3200, or a different module), this evidence cannot find that path. Need either (a) `import consent; print(consent.__file__)` from a running server, or (b) the actual stack trace from a failing `/consent` POST to identify which module is bound to port 3200.
- That the repo copy will behave identically to the live copy if deployed. Repo and live differ on the consent.py audit/status strings, `await_confirm` parameter, `confirm` signature, and the consent flow audit-event field names — these are not breaking but are semantic deltas.
- The exact cause of the current Telegram approval incident. This investigation only establishes what files are loaded and what their semantic differences are. Identifying the actual trigger requires runtime evidence (request log, audit log entry, callback handler log line `cb {...}`) which I did not request from the box to keep this strictly read-only / safe.
- Whether the second `ollie_hands.server` PID (20416) and the supervisor PIDs (7100, 17304) are actually bound to the same `/consent` port or to different listeners. `Get-NetTCPConnection` from the box would prove this; not run in this session.
- That the local `approval-command.js` is in the active code path of the live plugin. The live plugin's `sendOwnerTelegram` (line 316) goes through `api.runtime?.channel?.outbound?.loadAdapter("telegram")?.sendText`; `approval-command.js` content was not analyzed in this session.

---

## 7. Key file paths (absolute, evidence-locator)

- Live Hands: `/mnt/c/ollie-hands/ollie_hands/consent.py` (in Windows: `C:\ollie-hands\ollie_hands\consent.py`) — sha `79CB86...`
- Live Hands: `/mnt/c/ollie-hands/ollie_hands/server.py` — sha `0AFDB0...`
- Live Hands: `/mnt/c/ollie-hands/ollie_hands/auth.py` — sha `826737...` (matches repo)
- Live Hands: `/mnt/c/ollie-hands/ollie_hands/config.py` — sha `3778ba...` (matches repo)
- Live plugin: `/home/openclaw/.openclaw/plugins/ollie-wa-approval/index.js` (WSL OpenClawGateway) — sha `3d7a89...`
- Live plugin manifest: `/home/openclaw/.openclaw/plugins/ollie-wa-approval/openclaw.plugin.json` — sha `80f5e1...`
- Live plugin config: `/home/openclaw/.openclaw/openclaw.json` (WSL) — entry `ollie-wa-approval`
- Gateway service unit: `/home/openclaw/.config/systemd/user/openclaw-gateway.service` (WSL)
- Repo Hands: `./ollie-hands/ollie_hands/{consent,server,auth,config,executor,policy,engine,browser,actscript,grants}.py`
- Repo plugin: `./openclaw-ollie-wa-approval/{index.js,openclaw.plugin.json,package.json,approval-command.js}`
- Local /tmp captures used for analysis: `/tmp/consent_live.py`, `/tmp/live_hands/{server,config,executor,engine}.py`, `/tmp/live_plugin/{index.js,openclaw.plugin.json,approval-command.js}`