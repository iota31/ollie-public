# Config

The concrete config Ollie runs on. **All real secret values live in
`secrets.local.md` (gitignored).** This file references them by name
and shows the *shape* of the config blocks.

The single source of truth is
`~/.openclaw/openclaw.json` inside the WSL distro
`OpenClawGateway`.

---

## Top-level shape

```jsonc
{
  "providers": { /* LLM providers */ },
  "channels":  { /* surface channels */ },
  "mcp":       { "servers": { /* MCP clients */ } },
  "tools":     { /* built-in tools */ },
  "nodes":     { /* paired host nodes */ }
}
```

---

## LLM provider: MiniMax M3

**File path:** `~/.openclaw/openclaw.json` → `providers.minimax`

```jsonc
{
  "providers": {
    "minimax": {
      "type":    "anthropic",
      "baseUrl": "https://api.minimax.io/anthropic/v1",  // NOTE: trailing /v1
      "model":   "MiniMax-M3",
      "apiKey":  "${MINIMAX_API_KEY}"
    }
  }
}
```

| Field    | Value                                                                                       |
|----------|---------------------------------------------------------------------------------------------|
| type     | `anthropic` (it's an Anthropic-format endpoint)                                             |
| baseUrl  | `https://api.minimax.io/anthropic/v1` — **trailing `/v1` is required** (docs omit it → 404) |
| model    | `MiniMax-M3`                                                                                |
| apiKey   | env ref `MINIMAX_API_KEY` (real value in `secrets.local.md`)                                |

**Env on the box:** `MINIMAX_API_KEY` exported in the WSL distro's
init for the gateway process. Provider block reads it via
`${MINIMAX_API_KEY}` interpolation.

**Optional:** point at the local Portkey-style AI gateway
(`<LOCAL_GATEWAY_PATH>`) instead of the raw
provider. The user has it; not yet wired in Ollie. Would be:

```jsonc
"baseUrl": "http://<TAILSCALE_IP_VPS>:<port>/anthropic"
```

(Use the gateway IP, not localhost, since OpenClaw is in WSL but
the gateway runs on the host's Tailscale interface.)

---

## Channel: Telegram

**File path:** `~/.openclaw/openclaw.json` → `channels.telegram`

```jsonc
{
  "channels": {
    "telegram": {
      "enabled":  true,
      "botToken": "${TELEGRAM_BOT_TOKEN}",
      "allowFrom": [<OWNER_TELEGRAM_CHAT_ID>],
      "dmPolicy":  "allowlist"
    }
  }
}
```

| Field     | Value                                                |
|-----------|------------------------------------------------------|
| botToken  | env ref `TELEGRAM_BOT_TOKEN` (real value in `secrets.local.md`) — bot handle `@SonOfTushar_bot` |
| allowFrom | `[<OWNER_TELEGRAM_CHAT_ID>]` — owner chat id; only this chat can reach Ollie |
| dmPolicy  | `"allowlist"` — required for the `allowFrom` to actually gate |

The same bot token is reused by the `canada-to-usa` project
(send-only). Token reuse is fine; per-project lock is
`allowFrom`, not the token.

**Env on the box:** `TELEGRAM_BOT_TOKEN` exported alongside
`MINIMAX_API_KEY`.

---

## MCP server: 4DPocket

**File path:** `~/.openclaw/openclaw.json` → `mcp.servers.4dpocket`

```jsonc
{
  "mcp": {
    "servers": {
      "4dpocket": {
        "type":    "http",
        "url":     "http://<TAILSCALE_IP_VPS>:4040/mcp/",   // NOTE: trailing slash
        "headers": {
          "Authorization": "Bearer ${FOURDPOCKET_PAT}"
        }
      }
    }
  }
}
```

| Field          | Value                                                                                       |
|----------------|---------------------------------------------------------------------------------------------|
| type           | `http`                                                                                      |
| url            | `http://<TAILSCALE_IP_VPS>:4040/mcp/` — **trailing slash required** (without it the host proxy 421s) |
| Authorization  | `Bearer ${FOURDPOCKET_PAT}` — scoped editor-PAT (NOT admin; real value in `secrets.local.md`) |

**Why cleartext HTTP is OK:** both the box (`<TAILSCALE_IP>`) and the
VPS (`<TAILSCALE_IP_VPS>`) are Tailscale nodes, so the PAT rides
encrypted WireGuard even though 4DPocket itself has no TLS.

**Proxy quirk (worth re-stating):** the host proxy on
`<TAILSCALE_IP_VPS>:4040` returns **421 (Misdirected Request)** if the
request omits the trailing `/` on `/mcp/`. Always include it.

---

## Retired MCP server: Tier-2 computer-use POC

The old `computer-use-mcp` service on ports 3100/3101 and its `desktop` MCP
entry were retired after `ollie-hands` replaced them. The snippet below is
historical and must not be deployed.

**File path:** `~/.openclaw/openclaw.json` → `mcp.servers.tier2`

```jsonc
{
  "mcp": {
    "servers": {
      "tier2": {
        "type":    "http",
        "url":     "http://<TAILSCALE_IP>:3100/mcp",     // host process, in session 1
        "headers": {
          "Authorization": "Bearer ${TIER2_BEARER}"   // TODO: bearer auth
        }
      }
    }
  }
}
```

| Field          | Value                                                                                       |
|----------------|---------------------------------------------------------------------------------------------|
| url            | `http://<TAILSCALE_IP>:3100/mcp` — host Tailscale IP, port 3100. The engine is an unsandboxed host process. |
| Authorization  | Bearer auth (the engine has no built-in auth — TODO before exposing it beyond loopback)      |

Current host actions use the bearer-authenticated `hands` MCP server on port
3200. See `ARCHITECTURE.md` and `ollie-hands/README.md` for current behavior.

---

## Built-in tool: Groq STT (audio)

**File path:** `~/.openclaw/openclaw.json` → `tools.media.audio`

```jsonc
{
  "tools": {
    "media": {
      "audio": {
        "provider": "groq",
        "model":    "whisper-large-v3-turbo",
        "apiKey":   "${GROQ_API_KEY}"
      }
    }
  }
}
```

| Field    | Value                                                                                       |
|----------|---------------------------------------------------------------------------------------------|
| provider | `groq`                                                                                      |
| model    | `whisper-large-v3-turbo`                                                                    |
| apiKey   | env ref `GROQ_API_KEY` (real value in `secrets.local.md`)                                   |
| Status   | **Inactive until the user wires the key into the WSL distro env.** Block is configured; activation is a one-line env export. |

---

## Paired node: Windows host (Companion)

**File path:** `~/.openclaw/openclaw.json` → `nodes`

```jsonc
{
  "nodes": {
    "allowCommands": [
      "system.run",
      "system.which",
      "system.run.prepare",
      "system.execApprovals.get",
      "system.execApprovals.set"
    ],
    "paired": [
      {
        "id":   "Windows Node (MBD25-30)",
        "role": "operator.write",
        "execPolicyFile": "C:\\Users\\Source\\AppData\\Local\\OpenClawTray\\exec-policy.json"
      }
    ]
  }
}
```

The Companion is the only paired node today. Role is
`operator.write` (read + write, not admin). The 21-rule default
exec-policy is regenerated on Companion launch.

---

## Where the env vars are set

In the WSL distro's init (not committed). For reference, the
expected env when the gateway starts:

```bash
MINIMAX_API_KEY=...
TELEGRAM_BOT_TOKEN=...
FOURDPOCKET_PAT=...
GROQ_API_KEY=...        # when user adds it
TIER2_BEARER=...        # when the engine is exposed
```
