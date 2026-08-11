// @ts-check
// =============================================================================
// @ollie/openclaw-hands-approval
//
// Owner approval relay for the ollie-hands computer-use engine.
//
// When the engine hits a confirm-tier action (acts-as-Tushar / destructive /
// browser commit), it sends the owner a Telegram prompt with a 6-digit code
// and BLOCKS, denying on timeout. This plugin closes the loop: it intercepts
// the owner's Telegram reply ("approve <code>" / "deny <code>") PRE-LLM and
// POSTs the decision to the engine's bearer-authed /consent endpoint.
//
// Integrity: the brain (LLM) is never in this path — only a literal Telegram
// message from the owner's own chat id can approve, and approval is bound to
// the engine-issued code. A hijacked brain can neither forge nor suppress it.
//
// Modeled on the verified hook API of openclaw-ollie-wa-approval:
//   - message_received -> correlate owner Telegram reply
//   - telegram send via api.runtime.channel.outbound.loadAdapter("telegram")
//
// Inert when config.enabled is false. Never crashes the host.
// =============================================================================

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

const PLUGIN_ID = "ollie-hands-approval";

const DEFAULT_CONFIG = Object.freeze({
  enabled: false,
  ownerTelegramChatId: "<OWNER_TELEGRAM_CHAT_ID>",
  consentUrl: "http://<TAILSCALE_IP>:3200/consent",
  bearerToken: "", // if empty, reuse the `hands` MCP server bearer (no dup secret)
});

const APPROVE_RE = /^(?:approve|ok|yes|y)\s+(\d{6})$/i;
const DENY_RE = /^(?:deny|no|n)\s+(\d{6})$/i;

function resolveConfig(api) {
  const cfg = api?.config?.plugins?.entries?.[PLUGIN_ID]?.config ?? {};
  let bearer =
    typeof cfg.bearerToken === "string" && cfg.bearerToken.trim()
      ? cfg.bearerToken.trim()
      : "";
  if (!bearer) {
    // reuse the bearer the gateway already uses for the hands MCP server
    const auth = api?.config?.mcp?.servers?.hands?.headers?.Authorization;
    if (typeof auth === "string") bearer = auth.replace(/^Bearer\s+/i, "").trim();
  }
  return {
    enabled: cfg.enabled === true,
    ownerTelegramChatId:
      typeof cfg.ownerTelegramChatId === "string" && cfg.ownerTelegramChatId.trim()
        ? cfg.ownerTelegramChatId.trim()
        : DEFAULT_CONFIG.ownerTelegramChatId,
    consentUrl:
      typeof cfg.consentUrl === "string" && cfg.consentUrl.trim()
        ? cfg.consentUrl.trim()
        : DEFAULT_CONFIG.consentUrl,
    bearerToken: bearer,
  };
}

function parseOwnerCommand(text) {
  if (typeof text !== "string") return null;
  const t = text.trim();
  let m = t.match(APPROVE_RE);
  if (m) return { approve: true, code: m[1] };
  m = t.match(DENY_RE);
  if (m) return { approve: false, code: m[1] };
  return null;
}

async function postConsent(cfg, code, approve) {
  const resp = await fetch(cfg.consentUrl, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${cfg.bearerToken}`,
    },
    body: JSON.stringify({ code, approve }),
  });
  const body = await resp.json().catch(() => ({}));
  return { status: resp.status, body };
}

async function sendOwnerTelegram(api, to, text) {
  try {
    const send = (await api.runtime?.channel?.outbound?.loadAdapter("telegram"))
      ?.sendText;
    if (typeof send !== "function") return false;
    await send({ cfg: api.config, to, text });
    return true;
  } catch (err) {
    api.logger?.warn?.(`${PLUGIN_ID}: telegram send failed: ${err?.message ?? err}`);
    return false;
  }
}

const ollie_hands_approval_default = definePluginEntry({
  id: PLUGIN_ID,
  name: "Ollie Hands Approval Relay (RETIRED)",
  description:
    "RETIRED. Hands action approvals are now handled by the unified owner-approval router in ollie-wa-approval (one approve/deny <code> path on Telegram). This plugin registers no hooks.",
  register(api) {
    // Superseded by the unified owner-approval router in ollie-wa-approval.
    // Kept only as an inert tombstone so an existing config entry stays valid;
    // it registers NO hooks so it cannot collide with the router.
    api.logger?.info?.(
      `${PLUGIN_ID}: RETIRED no-op (hands approvals handled by ollie-wa-approval unified router)`,
    );
  },
});

export default ollie_hands_approval_default;
