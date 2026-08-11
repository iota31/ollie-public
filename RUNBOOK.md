# Runbook

Operational notes for keeping Ollie alive. If something is broken,
start at the top and work down.

---

## Reach the box

The Windows box (`mbd25-30`, Tailscale IP `<TAILSCALE_IP>`) is reached
over Tailscale SSH.

```bash
ssh -i ~/.ssh/id_ed25519 source@<TAILSCALE_IP>
```

Quirks (from `reference_windows_box.md`):

- **Use the IP, not the hostname.** `known_hosts` is keyed by IP;
  the `mbd25-30` alias fails host-key verification.
- **User is `source`** (Windows local account). NOT `tushu311095`
  (that's the Tailscale account owner). Wrong user → "Permission
  denied (publickey)".
- **Default remote shell is cmd.exe.** Run PowerShell via
  `powershell -NoProfile -NonInteractive -Command "..."`.
- **One command per SSH call.** `cmd` doesn't treat `;` as a
  separator — use `&` or send separate invocations.
- **PowerShell quoting is fragile over SSH.** EncodedCommand transit
  has failed before. Most reliable pattern: have the script write
  output to a temp file, then read it back with a separate
  `cmd /c type %USERPROFILE%\file.txt`.
- **Strip SSH banner noise:**
  `grep -vE "post-quantum|store now|may need|openssh.com|^\*\*$"`
- **`del` is flaky over SSH.** Use PowerShell `Remove-Item`.
- **GUI apps launched over SSH render in a hidden session.** Watch
  via RDP, not over SSH.

Hardware/storage references:

- Dell Pro 16 laptop, ~31.5 GB RAM, Windows 11 build `26300.8553`.
- `D:\leads-warehouse\` — lead data. Drive D: ("TUSHAR", exFAT) has
  ~742 GB free. **Separate physical drive from C:**, so an OS
  reinstall of C: does not wipe it.
- C: only ~135 GB free — write heavy data to D:.
- Python on the box has duckdb + pandas.
- 7-Zip at `C:\Program Files\7-Zip\7z.exe`.

---

## ⚠️ Gateway distro keepalive (terminate footgun)

The `OpenClawGateway` distro is held alive ONLY by a Windows-side WSL
client process. `wsl --terminate OpenClawGateway` kills that holder —
afterwards the distro idle-shuts ~45s after each transient `wsl` command
and everything inside crashloops (tell: repeated kernel-boot lines in
`dmesg`, gateway restarting every ~45s, agent turns dying mid-reply;
caused a Telegram outage on 2026-06-12). **After ANY terminate or reboot
of the distro, re-run the keepalive:**

```
wsl --terminate OpenClawGateway && schtasks /run /tn OllieGatewayKeepalive
```

Two tasks exist (both ONLOGON, InteractiveToken):
- **`OllieGatewayKeepalive`** (the real holder, created 2026-06-12,
  mirrors OllieLabKeepalive): powershell retry loop
  `while(1){ wsl -d OpenClawGateway --exec sleep infinity; sleep 20 }`,
  no ExecutionTimeLimit, survives on battery
  (DisallowStartIfOnBatteries/StopIfGoingOnBatteries = false).
  Definition source: created from XML at `C:\Users\Source\ollie-gateway-keepalive.xml`.
- **`OllieGatewayBoot`** (legacy, plain `wsl.exe -d OpenClawGateway`):
  only holds the distro when started with an interactive console at
  logon; `schtasks /run` over SSH does NOT produce a durable holder, and
  it has a 72h ExecutionTimeLimit. Do not rely on it for recovery.

NOTE: `OllieLabKeepalive` has StopIfGoingOnBatteries=true — OllieLab
dies when the laptop is unplugged (gateway keepalive deliberately does
not).

---

## Inside the box: where the configs live

| Path (Windows host)                                                                 | What                                                  |
|-------------------------------------------------------------------------------------|-------------------------------------------------------|
| `C:\Users\Source\AppData\Local\OpenClawTray\OpenClaw.Tray.WinUI.exe`                | The OpenClaw Windows Companion (tray app)             |
| `C:\Users\Source\AppData\Local\OpenClawTray\exec-policy.json`                       | Companion exec-policy (default `deny`, 21 default rules) |
| `C:\Users\Source\AppData\Local\OpenClawTray\tools\mxc\x64\wxc-exec.exe`            | MXC executor binary (used by `MxcCommandRunner`)      |
| `C:\Users\Source\AppData\Local\OpenClawTray\openclaw-tray.log`                      | Companion log (look here for exec-policy + sandbox denies) |

| Path (inside WSL distro `OpenClawGateway`) | What                              |
|--------------------------------------------|-----------------------------------|
| `~/.openclaw/openclaw.json`                | **All Ollie config lives here.**  |
| `~/.openclaw/`                             | Working dir for the gateway       |

Gateway token auth is on `127.0.0.1:18789` inside the WSL distro.

---

## Check health

### 1. Is the box reachable?

```bash
ssh -i ~/.ssh/id_ed25519 source@<TAILSCALE_IP> whoami
# expect: mbd25-30\source
```

If this fails and the box was rebooted: Tailscale did not come
back up. **Physical or RDP access is required to bring Tailscale
online** — see "Known gotchas" below.

### 2. Is the Companion running in session 1?

Over SSH (commands run in session 0, so this is a peek, not a
control):

```bash
powershell -NoProfile -NonInteractive -Command "Get-Process OpenClaw.Tray.WinUI -ErrorAction SilentlyContinue | Select-Object Id,SessionId,ProcessName"
# expect: SessionId == 1
```

If `SessionId == 0` or no process: Companion is not running. See
"Restart the Companion" below.

### 3. Is the gateway reachable from WSL?

```bash
wsl -d OpenClawGateway -- bash -lc 'openclaw gateway ping'
# expect: pong
```

### 4. Is the node paired to the gateway?

```bash
wsl -d OpenClawGateway -- bash -lc 'openclaw nodes status'
# expect: Windows Node (MBD25-30) ... paired · connected
```

### 5. Can Ollie actually do a tool call? (end-to-end smoke)

Send a message to `@SonOfTushar_bot` from the owner's Telegram
(chat id <OWNER_TELEGRAM_CHAT_ID>) — "echo hello" or similar. You should see a
reply within a few seconds.

If no reply, walk down:

1. Did the message reach Telegram? Check `@BotFather` / your client.
2. Did the gateway see it? Check the WSL gateway log:
   `wsl -d OpenClawGateway -- bash -lc 'tail -50 ~/.openclaw/gateway.log'`
3. Did the LLM call succeed? Look for HTTP errors against
   `https://api.minimax.io/anthropic/v1`.
4. Is the owner chat id still in `allowFrom`? Check
   `~/.openclaw/openclaw.json` → `channels.telegram.allowFrom`.

---

## Restart the gateway

`openclaw gateway` reloads hot. From inside the WSL distro:

```bash
wsl -d OpenClawGateway -- bash -lc 'openclaw gateway reload'
```

If that hangs or the config is wedged, a hard restart is the
nuclear option:

```bash
wsl -d OpenClawGateway -- bash -lc 'openclaw gateway stop && openclaw gateway start'
```

For deeper issues, recycle the WSL distro itself:

```bash
wsl --shutdown OpenClawGateway
wsl -d OpenClawGateway -- bash -lc 'openclaw gateway start'
```

---

## Restart the Companion

The Companion is the tray app that runs the node on the host and
pairs to the gateway. It autostarts via `HKCU:\...\Run`, so it
should be running as the user at logon.

If you need to relaunch it from SSH (it'll spawn in session 0
invisibly, but you can re-pair):

```bash
powershell -NoProfile -NonInteractive -Command "Start-Process 'C:\Users\Source\AppData\Local\OpenClawTray\OpenClaw.Tray.WinUI.exe'"
```

For a clean session-1 relaunch (the right thing to do after a
reboot), use the scheduled task `OpenClawCompanionManual` that was
created during the M0/M1 POC. From PowerShell:

```powershell
Register-ScheduledTask -TaskName OpenClawCompanionManual `
  -Trigger (New-ScheduledTaskTrigger -AtLogOn) `
  -Action (New-ScheduledTaskAction `
    -Execute "C:\Users\Source\AppData\Local\OpenClawTray\OpenClaw.Tray.WinUI.exe") `
  -RunLevel Highest
Start-ScheduledTask -TaskName OpenClawCompanionManual
```

To remove it:

```powershell
Unregister-ScheduledTask -TaskName OpenClawCompanionManual
```

---

## The Companion's exec-policy regenerates

On launch, the Companion regenerates the **21 default exec-policy
rules** into `exec-policy.json`. If you ever edited that file
manually and lost the defaults (e.g. earlier in this project a
bad edit left it at `{"defaultAction":"deny","rules":[]}`), just
restart the Companion and the defaults come back. Hot-reload
behavior: the Companion picks up changes to `exec-policy.json` on
its next `system.run` invocation.

To set exec-policy from the WSL CLI you would normally run
`openclaw approvals set --node 'Windows Node (MBD25-30)'`, but
**this is broken from WSL** — the CLI passes a WSL file path that
the gateway can't read. Workarounds:

- Write the file directly to the Windows host path
  (`\\wsl$\OpenClawGateway\...` from PowerShell, or push via
  scheduled task).
- Set it via a scheduled-task-launched Companion.

---

## Known gotchas

### Tailscale does not auto-reconnect after reboot

After a reboot, this box does **not reliably** come back to
Tailscale on its own. Remote SSH and RDP (both ride Tailscale at
`<TAILSCALE_IP>`) go dark until someone is physically present or
already on RDP to bring Tailscale up.

**Fix (deferred):** configure Tailscale to auto-start reliably
as a Windows service. Until that is done, plan for physical/RDP
recovery on every reboot.

### Desktop input injection needs interactive session 1

This is the single biggest footgun. Processes launched over plain
non-interactive SSH run in **session 0** where:

- `SendKeys.SendWait` → "Access is denied"
- `Graphics.CopyFromScreen` → "handle is invalid"
- `$env:SESSIONNAME` is empty

So the architecture's premise: a non-interactive SSH shell cannot
drive the desktop.

The fix is to launch the Tier-2 engine via a scheduled task that
runs in the logged-in user's interactive session 1, e.g.:

```powershell
schtasks /Create /SC ONCE /ST <time> /RL HIGHEST /IT `
  /TN "tier2-engine" /TR "node C:\path\to\engine.js"
```

(`/IT` = interactive, `/RL HIGHEST` = run with highest privs.)

The MXC-sandboxed Companion has the same limitation: the
`mxc-direct-appc` sandbox in which `system.run` executes has
hard-coded `policyJson.ui = { allowInputInjection: false }` —
input injection is disabled by default. The POC path bypasses the
Companion's sandbox entirely (the engine runs as a separate host
process, not via the Companion).

### MXC's input-injection block

Independent of the session issue: the **shipping OpenClaw Windows
Companion does not allow input injection at all**. To get
input-injection through the Companion, you would have to
configure the `mxc` policy to allow it (registry/JSON outside the
gateway's reach). The current design sidesteps this by running
the Tier-2 engine as a separate, non-Companion host process.

### 4DPocket host proxy: trailing slash on `/mcp/`

`http://<TAILSCALE_IP_VPS>:4040/mcp` returns **421** (Misdirected Request)
because the host proxy keys the Host header on the URL. The fix
is the trailing slash: `http://<TAILSCALE_IP_VPS>:4040/mcp/`. Same for
`/api/v1` (works without slash, but be consistent).

### `del` is flaky, `;` is not a separator, banner noise

Already covered in "Reach the box" above. The mental model:
treat every SSH call as a fresh cmd.exe session with a noisy
banner, and avoid shell tricks.

### Telemetry is Full (AllowTelemetry=3)

Set over SSH on 2026-06-04 at
`HKLM\...\CurrentVersion\Policies\DataCollection`. Insider
enrollment requires at least 1 (Required). Revert to 1 if ever
desired:

```powershell
Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\DataCollection" -Name AllowTelemetry -Value 1
```

### BitLocker is OFF on both drives

Important because there's no recovery-key lockout risk if the
OS ever needs to be reinstalled. If you turn BitLocker on, save
the recovery keys somewhere safe (NOT only on the box) before
rebooting.

---

## Disaster recovery / restore

Ollie's living state is **nightly snapshotted**, **age-encrypted
on the box**, and **pushed to a private GitHub repo**. The
private age key never lives on the box or in the repo — it
lives only on the Mac (and in the password manager). This
section is how to rebuild a fresh box from a backup.

### What's in scope

The backup tar (one per night) covers:

- `~/.openclaw/workspace/`, `memory/`, `agents/`,
  `credentials/`, `secrets/`
- `~/.openclaw/openclaw.json` (the main Ollie config)
- `~/.config/gh/` (the `ollie-onllm` GitHub auth)
- `~/.config/himalaya/`

It does **not** back up the rest of the home directory, the
WSL distro itself, the OpenClaw Companion, or any Windows host
state.

### Schedule + retention

- **When:** nightly at 03:30 box-local (CEST), via a WSL
  systemd --user timer (`ollie-state-backup.timer`,
  `ollie-state-backup.service`).
- **Where:** private GitHub repo `onllm-dev/ollie-state` on the
  `main` branch.
- **Format:** one `ollie-state-YYYYMMDDTHHMMSSZ.age` file per
  night. The script keeps only the last **7** in the repo
  (oldest are pruned at the end of each run). History can be
  squashed with `git filter-repo` later if the commit count
  becomes a concern.
- **Log:** `~/.openclaw/logs/state-backup.log` on the box.

### Verify a backup is current

```bash
gh api /repos/onllm-dev/ollie-state/contents \
  | python3 -c 'import json,sys; [print(d["name"], d["size"]) for d in json.load(sys.stdin)]'
```

You should see 1–7 `.age` files; the newest timestamp is the
last successful run.

If the timer has been silent for >24h, check `systemctl
--user list-timers --all` on the box, then look at
`~/.openclaw/logs/state-backup.log` for the last error.

### Restore (full box rebuild)

Run from the **Mac** (where the private key lives). Requires
`age` on PATH (`brew install age`).

1. **Clone the backup repo** somewhere temporary:

   ```bash
   git clone https://github.com/onllm-dev/ollie-state.git /tmp/ollie-state-restore
   cd /tmp/ollie-state-restore
   ls -1 ollie-state-*.age
   # pick the newest, e.g. ollie-state-20260607T210649Z.age
   ```

2. **Decrypt and untar to a staging dir** (DO NOT untar
   straight into a live `~/.openclaw/` — review first):

   ```bash
   LATEST=$(ls -1 ollie-state-*.age | tail -1)
   mkdir -p /tmp/ollie-state-staging
   age -d -i ./.age-staging/ollie-state.key \
     "$LATEST" | tar -xzf - -C /tmp/ollie-state-staging
   ```

3. **Inspect** what came out:

   ```bash
   ls /tmp/ollie-state-staging/
   cat /tmp/ollie-state-staging/.openclaw/openclaw.json | head -40
   ```

4. **Copy into the live box** (on a freshly rebuilt box, after
   WSL `OpenClawGateway` is installed and `source` user exists):

   ```bash
   # from the Mac
   rsync -av /tmp/ollie-state-staging/workspace/ \
     source@<TAILSCALE_IP>:/tmp/restore-workspace/
   # ...repeat for memory/ agents/ credentials/ secrets/ .config/
   ```

   Then from the box, place them where they belong:

   ```bash
   wsl -d OpenClawGateway -- bash -lc '
     set -e
     mkdir -p ~/.openclaw ~/.config
     cp -a /tmp/restore-workspace ~/.openclaw/workspace
     cp -a /tmp/restore-memory     ~/.openclaw/memory
     cp -a /tmp/restore-agents     ~/.openclaw/agents
     cp -a /tmp/restore-credentials ~/.openclaw/credentials
     cp -a /tmp/restore-secrets    ~/.openclaw/secrets
     cp -a /tmp/restore-config/*   ~/.config/
     cp /tmp/restore-config/openclaw.json ~/.openclaw/openclaw.json
     chmod 700 ~/.openclaw/bin/* ~/.openclaw/secrets/* 2>/dev/null || true
   '
   ```

5. **Restart the gateway** (so it picks up the restored state):

   ```bash
   wsl -d OpenClawGateway -- bash -lc 'openclaw gateway stop && openclaw gateway start'
   ```

6. **Smoke test:** send `@SonOfTushar_bot` an `echo hello` and
   confirm it replies (see "Can Ollie actually do a tool
   call?" above).

### Restore (just one file)

If you only need a single config back, e.g. `openclaw.json`:

```bash
LATEST=$(gh api /repos/onllm-dev/ollie-state/contents \
  | python3 -c 'import json,sys; print(sorted([d["name"] for d in json.load(sys.stdin) if d["name"].endswith(".age")])[-1])')
gh api "/repos/onllm-dev/ollie-state/contents/$LATEST" \
  -H 'Accept: application/vnd.github.raw' > /tmp/$LATEST
age -d -i ./.age-staging/ollie-state.key \
  /tmp/$LATEST | tar -xzOf - .openclaw/openclaw.json
```

### Key custody (read this)

- The **age private key** lives only at
  `./.age-staging/ollie-state.key`
  on the Mac, and **only** in your password manager (e.g.
  1Password / Bitwarden). The path `.age-staging/` is
  `.gitignore`d.
- The **public recipient** (safe to share) is in
  `secrets.local.md` AND on the box at
  `~/.openclaw/secrets/backup-recipient.age`.
- **Never** copy the private key to the box, the WSL distro,
  the GitHub repo, or any cloud-synced directory. If you
  suspect it leaked, run `age-keygen` again to make a new
  keypair, re-encrypt everything from a fresh full backup,
  and update the recipient file on the box.

## OllieLab — the burnable POC sandbox (added 2026-06-11)

A second WSL distro where Ollie runs untrusted code (repos from Tushar's
4DPocket saves) inside per-POC podman containers. Architecture and
lifecycle: `Plans/transient-forging-lynx.md`. Interface: gateway-side
`~/bin/lab` CLI (spawn/exec/harvest/destroy/save/list/reap/rebuild-base).

Key facts:
- Distro `OllieLab`, user `lab`, sshd on **:2222**. WSL NAT-mode distros
  share one VM network namespace, so the gateway reaches it at
  `127.0.0.1:2222` — no portproxy. Key: gateway `~/.ssh/id_ollielab`.
- Hardening (`/etc/wsl.conf`): automount off, interop off, systemd on.
  Verified: no /mnt/c, no gateway fs, no powershell/cmd.
- **Zero secrets** live there. No tailscale. Nothing of value.
- Kept alive by host scheduled task `OllieLabKeepalive` (ONLOGON,
  `wsl -d OllieLab --exec sleep infinity`).
- Per-POC containers: `lab-base` image (Containerfile:
  `ollie-lab/base/Containerfile` in the repo, copy at OllieLab
  `/home/lab/Containerfile`), caps 4cpu/6g/512pids, 1 concurrent,
  6h TTL (hourly `ollie-lab-reaper.timer` on the gateway), audit at
  `workspace/lab/audit.log`.

### Break-glass reimage (~5 min, from the Windows host)

```
wsl --unregister OllieLab
wsl --install Ubuntu-24.04 --name OllieLab --no-launch
# then re-run the setup block (user lab, wsl.conf, sshd:2222, podman,
# authorized_keys from gateway ~/.ssh/id_ollielab.pub) — the exact
# commands are in the Phase-2 commit message / plan file — and
# wsl --terminate OllieLab && Start-ScheduledTask OllieLabKeepalive
# finally: lab rebuild-base
```
Nothing in OllieLab needs backing up — notes/artifacts/ledger all live on
the gateway side.
