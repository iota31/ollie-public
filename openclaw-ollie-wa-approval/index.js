// @ts-check
// =============================================================================
// @ollie/openclaw-wa-approval
//
// The owner-consent path for Ollie. Every approve/deny the owner makes flows
// through here. Two approval families share the router:
//
//   H-<ref>  ollie-hands actions (mouse/keyboard/shell/browser on the box)
//   W-<ref>  WhatsApp contact gating (may Ollie talk to this stranger at all)
//
// Hooks (verified against live gateway source on box <TAILSCALE_IP>):
//   - before_agent_run      -> block when sender is not in approved[]
//                             (see dist/plugin-sdk/hook-types-B_5108I1.d.ts:868
//                              and dist/hook-runner-global-BdHeqZIb.js:676-707)
//   - message_sending       -> cancel when recipient is not in approved[]
//                             (see dist/plugin-sdk/hook-types-B_5108I1.d.ts:190
//                              and dist/deliver-B_snf0tE.js:729-748)
//   - message_received      -> inert (observation-only; it cannot block)
//
// Telegram send uses the canonical pattern from
// dist/extensions/device-pair/index.js:556-560:
//   (await api.runtime.channel.outbound.loadAdapter("telegram"))?.sendText({
//     cfg: api.config, to, text, ...accountId ? { accountId } : {}
//   })
//
// Feature flag: config.enabled gates WhatsApp contact exposure only.
// Owner approve/deny interception remains active pre-LLM whenever the plugin
// is loaded, so authenticated owner commands never spill into the brain.
//
// Fail-safe: if the state file is unreadable on startup, we DENY unknowns
// (never silently approve). The plugin never crashes the host.
//
// -----------------------------------------------------------------------------
// ROUTING DOCTRINE (settled with the owner; enforced below)
// -----------------------------------------------------------------------------
//   * Autonomous hands use (heartbeat, scheduled jobs, anything Ollie starts
//     itself) -> approval prompt ALWAYS Telegram. The rationale is
//     reachability, not secrecy: WhatsApp's 24h window can be shut at 3 AM, and
//     a consent channel that can silently fail to reach the owner is not a
//     consent channel.
//   * Interactive, admin-initiated hands use -> the prompt may go to the
//     channel already in use, WhatsApp included, because the owner's own
//     message opened the window.
//   * Detached actions route by IMMEDIATE INITIATOR. A WhatsApp request that
//     becomes a scheduled job needing hands two hours later routes to Telegram,
//     because the origin is the job, not the conversation.
//   * Delivery failure -> Telegram. Not merely unknown origin.
//   * Contact gating (W-) -> Telegram, BOTH directions. Prompts go to Telegram
//     and commands are accepted ONLY from Telegram. The trigger is a stranger's
//     message, not the owner's, so there is no window guarantee; WhatsApp must
//     never be the surface for deciding whether Ollie talks to a stranger.
//   * H- commands from owner WhatsApp are legitimate (the interactive case).
//   * Identity resolution fails closed: an unresolvable sender gets guest
//     treatment, never owner.
//   * T3 is NOT channel-separated (decided 2026-07-26): the dangerous shell
//     tier routes by origin like every other tier.
//
// See the ORIGIN-MARKER SEAM block further down for the one piece of this
// doctrine that is NOT yet implementable here and why.
// =============================================================================

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { randomBytes } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { readFileSync as readFileSyncNode, statSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { parseApprovalCommand, selectPending } from "./approval-command.js";

// ---------------------------------------------------------------------------
// Schema (mirrors openclaw.plugin.json#configSchema)
// ---------------------------------------------------------------------------
const DEFAULT_CONFIG = Object.freeze({
  enabled: false,
  ownerTelegramChatId: "<OWNER_TELEGRAM_CHAT_ID>",
  ownerWhatsAppNumber: "+<OWNER_PHONE>",
  approvalsFile: "~/.openclaw/workspace/whatsapp-contacts.json",
  requestTimeoutMinutes: 60,
  hookTimeoutMs: 5000,
});

function resolveConfig(pluginConfig) {
  const cfg = pluginConfig && typeof pluginConfig === "object" ? pluginConfig : {};
  return {
    enabled: cfg.enabled === true,
    ownerTelegramChatId:
      typeof cfg.ownerTelegramChatId === "string" && cfg.ownerTelegramChatId.trim()
        ? cfg.ownerTelegramChatId.trim()
        : DEFAULT_CONFIG.ownerTelegramChatId,
    ownerWhatsAppNumber: normalizePhone(
      typeof cfg.ownerWhatsAppNumber === "string" && cfg.ownerWhatsAppNumber.trim()
        ? cfg.ownerWhatsAppNumber.trim()
        : DEFAULT_CONFIG.ownerWhatsAppNumber,
    ),
    approvalsFile: expandHome(
      typeof cfg.approvalsFile === "string" && cfg.approvalsFile.trim()
        ? cfg.approvalsFile.trim()
        : DEFAULT_CONFIG.approvalsFile,
    ),
    requestTimeoutMinutes: Number.isFinite(cfg.requestTimeoutMinutes)
      ? cfg.requestTimeoutMinutes
      : DEFAULT_CONFIG.requestTimeoutMinutes,
    hookTimeoutMs: Number.isFinite(cfg.hookTimeoutMs)
      ? cfg.hookTimeoutMs
      : DEFAULT_CONFIG.hookTimeoutMs,
  };
}

function expandHome(p) {
  if (typeof p !== "string") return p;
  if (p === "~") return homedir();
  if (p.startsWith("~/") || p.startsWith("~\\")) return join(homedir(), p.slice(2));
  return p;
}

// ---------------------------------------------------------------------------
// State store
// ---------------------------------------------------------------------------
// File shape (compatible with the existing
// /home/openclaw/.openclaw/workspace/whatsapp-contacts.json):
//
//   {
//     "_schema": "whatsapp-contacts.v1",     // preserved, not used
//     "_purpose": "...",                     // preserved, not used
//     "owner":   "+<OWNER_PHONE>",            // preserved
//     "approved": ["+<OWNER_PHONE>", ...],
//     "blocked":  ["...", ...],
//     "pending":  { "W-<ref>": { from, preview, kind, requestedAt } },
//     "updated":   "ISO-8601",
//     "updatedBy": "<plugin-id>",
//     "notes":     [...]                     // preserved
//   }
//
// We treat unknown fields as opaque and pass them through. This keeps the
// existing file working with the openclaw installer and any other tools that
// also write to it.
// ---------------------------------------------------------------------------

const PLUGIN_ID = "ollie-wa-approval";
const UPDATED_BY = `${PLUGIN_ID}@0.1.0`;

// ---------------------------------------------------------------------------
// Observability
//
// Two complementary emitters, deliberately kept distinct (see
// /tmp/wa-approval-merge-report.md for the reasoning):
//
//   logApprovalEvent(api, level, fields)
//       Staged, structured audit stream across the whole approval lifecycle.
//       Only whitelisted keys survive; every string value is passed through
//       sanitizeForLog and clamped, so an attacker-influenced value can neither
//       leak a credential nor blow up a log line.
//
//   logCallback(api, ctxLike, fields)
//       Exactly ONE bounded `cb {...}` correlation line per inline-button
//       callback, on stderr and the logger. This is the line an operator greps
//       when a button tap misbehaves; the one-line-per-tap invariant is what
//       makes it usable, so it must not be folded into the staged stream.
//
// Both funnel through sanitizeForLog, so redaction behaviour is single-sourced.
// ---------------------------------------------------------------------------

const MAX_LOG_CHARS = 200;
const MAX_LOG_FIELD_CHARS = 80;

/** Bounded, redacting sanitizer for free-form strings that may reach a log line.
 *  It must NEVER emit the bot token, approval credential, action digest, or any
 *  other secret, and output is clamped so a maliciously-large value cannot blow
 *  up the line.
 *    - "Bearer <token>"          -> "Bearer[redacted]"
 *    - JWT-like "a.b.c"          -> first 4 chars of the header only
 *    - http(s) URLs              -> "[url]"
 *    - long hex strings (>=40)   -> "[hex]"
 */
function sanitizeForLog(input) {
  if (typeof input !== "string") return "";
  let s = input;
  s = s.replace(/Bearer\s+[A-Za-z0-9._\-]+/g, "Bearer[redacted]");
  // JWT-like: redact the whole token, keep just the first 4 chars of the header
  // so an operator can tell it was a JWT, not a Bearer header.
  s = s.replace(
    /\b([A-Za-z0-9_\-]{8,})\.([A-Za-z0-9_\-]{8,})\.([A-Za-z0-9_\-]{8,})\b/g,
    (_m, a, _b, _c) => `${a.slice(0, 4)}.[redacted-jwt]`,
  );
  s = s.replace(/\b[0-9a-fA-F]{40,}\b/g, "[hex]");
  s = s.replace(/https?:\/\/\S+/g, "[url]");
  if (s.length > MAX_LOG_CHARS) {
    s = s.slice(0, MAX_LOG_CHARS) + "\u2026";
  }
  return s;
}

const APPROVAL_LOG_FIELDS = new Set([
  "event", "direction", "kind", "decision", "reason", "outcome", "ref",
  "dedup", "backend", "handled", "ok", "status", "error", "error_code",
  "method", "channel", "message_id", "update_id", "callback_id", "parse",
  "auth",
]);

/** Emit only explicitly whitelisted approval metadata. Never pass user text or
 *  secrets; string values are redacted and clamped regardless. */
function logApprovalEvent(api, level, fields) {
  const safe = {};
  for (const [key, value] of Object.entries(fields ?? {})) {
    if (!APPROVAL_LOG_FIELDS.has(key)) continue;
    if (typeof value === "boolean" || typeof value === "number" || value === null) {
      safe[key] = value;
    } else if (typeof value === "string") {
      const clean = sanitizeForLog(value);
      safe[key] = clean.length > MAX_LOG_FIELD_CHARS ? clean.slice(0, MAX_LOG_FIELD_CHARS) : clean;
    }
  }
  const write = api?.logger?.[level] ?? api?.logger?.info;
  write?.(`${PLUGIN_ID} ${JSON.stringify(safe)}`);
}

/** Emit one bounded correlation log line per approval callback event. */
function logCallback(api, ctxLike, fields) {
  const safe = {
    message_id:
      typeof ctxLike?.messageId === "string" || typeof ctxLike?.messageId === "number"
        ? ctxLike.messageId
        : null,
    ref: typeof fields.ref === "string" ? fields.ref : null,
    decision: typeof fields.decision === "string" ? fields.decision : null,
    cb_ns: typeof fields.cb_ns === "string" ? fields.cb_ns : null,
    cb_ver: typeof fields.cb_ver === "string" ? fields.cb_ver : null,
    auth: typeof fields.auth === "string" ? fields.auth : null,
    backend_status:
      typeof fields.backend_status === "number" ? fields.backend_status : null,
    backend_error_code:
      typeof fields.backend_error_code === "string" ? fields.backend_error_code : null,
    edit_result: typeof fields.edit_result === "string" ? fields.edit_result : null,
  };
  const line = `cb ${sanitizeForLog(JSON.stringify(safe))}`;
  try {
    process.stderr.write(`[ollie-wa-approval] ${line}\n`);
  } catch {}
  api?.logger?.info?.(`${PLUGIN_ID}: ${line}`);
}

/** @typedef {{ from: string, preview: string, kind: "inbound"|"outbound", requestedAt: string, expiresAt: string }} PendingEntry */
/** @typedef {{ owner?: string, approved: string[], blocked: string[], pending: Record<string, PendingEntry>, _passthrough?: Record<string, unknown> }} State */

function emptyState() {
  return { owner: undefined, approved: [], blocked: [], pending: {} };
}

async function readState(filePath, logger) {
  try {
    const raw = await readFile(filePath, "utf8");
    const parsed = JSON.parse(raw);
    return normalizeState(parsed);
  } catch (err) {
    if (err && err.code === "ENOENT") {
      // First run — return a fresh state. The caller decides whether to seed.
      return emptyState();
    }
    logger?.warn?.(
      `${PLUGIN_ID}: state file unreadable (${filePath}): ${err?.message ?? err}. Failing safe: denying unknowns.`,
    );
    // Fail-safe: return a state that DENIES everyone. We do not want a
    // missing/broken file to ever cause the gate to silently pass.
    return { owner: undefined, approved: [], blocked: [], pending: {}, _unreadable: true };
  }
}

function normalizeState(obj) {
  const out = emptyState();
  if (!obj || typeof obj !== "object") return out;
  if (typeof obj.owner === "string") out.owner = obj.owner;
  if (Array.isArray(obj.approved)) {
    out.approved = obj.approved.filter((n) => typeof n === "string" && n.trim()).map((n) => n.trim());
  }
  if (Array.isArray(obj.blocked)) {
    out.blocked = obj.blocked.filter((n) => typeof n === "string" && n.trim()).map((n) => n.trim());
  }
  if (obj.pending && typeof obj.pending === "object") {
    for (const [ref, entry] of Object.entries(obj.pending)) {
      if (entry && typeof entry === "object" && typeof entry.from === "string") {
        out.pending[ref] = {
          from: entry.from,
          preview: typeof entry.preview === "string" ? entry.preview : "",
          kind: entry.kind === "outbound" ? "outbound" : "inbound",
          requestedAt: typeof entry.requestedAt === "string" ? entry.requestedAt : new Date().toISOString(),
          expiresAt:
            typeof entry.expiresAt === "string"
              ? entry.expiresAt
              : new Date(Date.now() + 60 * 60 * 1000).toISOString(),
        };
      }
    }
  }
  return out;
}

// In-process mutex. The plugin is single-process; cross-process safety comes
// from the atomic-rename write pattern.
let writeChain = Promise.resolve();
function withLock(fn) {
  const next = writeChain.then(() => fn(), () => fn());
  writeChain = next.catch(() => undefined);
  return next;
}

async function writeState(filePath, state, logger) {
  // Re-read the original file (best-effort) so we preserve any sibling
  // metadata fields the installer or other tools wrote.
  let passthrough = {};
  try {
    const raw = await readFile(filePath, "utf8");
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object") {
      // Pull through everything except the fields we own.
      for (const [k, v] of Object.entries(parsed)) {
        if (
          k === "approved" ||
          k === "blocked" ||
          k === "pending" ||
          k === "updated" ||
          k === "updatedBy"
        ) continue;
        passthrough[k] = v;
      }
    }
  } catch {
    // No prior file or unreadable — that's fine, passthrough stays empty.
  }

  const out = {
    ...passthrough,
    ...(state.owner ? { owner: state.owner } : {}),
    approved: dedupStrings(state.approved),
    blocked: dedupStrings(state.blocked),
    pending: state.pending,
    updated: new Date().toISOString(),
    updatedBy: UPDATED_BY,
  };

  await mkdir(dirname(filePath), { recursive: true });
  const tmp = `${filePath}.tmp.${process.pid}.${randomBytes(4).toString("hex")}`;
  await writeFile(tmp, JSON.stringify(out, null, 2) + "\n", { encoding: "utf8", mode: 0o600 });
  await rename(tmp, filePath);
  logger?.info?.(`${PLUGIN_ID}: state file updated (${filePath})`);
}

function dedupStrings(arr) {
  return [...new Set(arr.filter((s) => typeof s === "string" && s.trim()))].map((s) => s.trim());
}

// ---------------------------------------------------------------------------
// WORK_DIGEST injection (before_prompt_build)
//
// Ground-truth work digest injection. The ollie-jobs runners regenerate
// /home/openclaw/.openclaw/workspace/WORK_DIGEST.md out-of-band; we inject its
// contents into the agent system prompt on EVERY session so the brain always
// knows the current state of work — and it survives /reset (a fresh system
// prompt is rebuilt, and this hook re-appends). Because the digest is a file,
// the plugin needs NO restart for digest *content* changes (only a restart for
// plugin *code* changes). The result is appended via appendSystemContext, which
// is provider-cacheable.
// ---------------------------------------------------------------------------

const WORK_DIGEST_PATH = "/home/openclaw/.openclaw/workspace/WORK_DIGEST.md";
const WORK_DIGEST_MAX_CHARS = 4000;

// mtime-keyed cache so we only touch disk when the digest actually changes
// (the hook fires per prompt build; a re-read every turn would be wasteful).
let workDigestCache = { mtimeMs: -1, text: "" };

/** Read WORK_DIGEST.md, caching by mtime. Never throws; "" on any problem. */
function readWorkDigest(logger) {
  try {
    const st = statSync(WORK_DIGEST_PATH);
    if (st.mtimeMs === workDigestCache.mtimeMs) {
      return workDigestCache.text;
    }
    let text = readFileSyncNode(WORK_DIGEST_PATH, "utf8");
    if (typeof text !== "string") text = "";
    if (text.length > WORK_DIGEST_MAX_CHARS) {
      text = text.slice(0, WORK_DIGEST_MAX_CHARS);
    }
    workDigestCache = { mtimeMs: st.mtimeMs, text };
    return text;
  } catch (err) {
    // Missing/unreadable digest is normal (file may not be regenerated yet) —
    // return "" so the hook injects nothing. One statSync per turn is the
    // cheapest possible existence probe; the cache only avoids re-READS.
    if (!err || err.code !== "ENOENT") {
      logger?.warn?.(`${PLUGIN_ID}: WORK_DIGEST read failed (${WORK_DIGEST_PATH}): ${err?.message ?? err}`);
    }
    workDigestCache = { mtimeMs: -1, text: "" };
    return "";
  }
}

// ---------------------------------------------------------------------------
// Phone-number helpers
// ---------------------------------------------------------------------------

/** Normalize a phone-ish string to E.164-ish. Best-effort — we never reject
 *  input for being messy, we just lower-case and trim and ensure a leading +. */
function normalizePhone(raw) {
  if (typeof raw !== "string") return "";
  let s = raw.trim();
  if (!s) return "";
  // strip whitespace and dashes inside (e.g. WhatsApp may send "+91 70 42 ...")
  s = s.replace(/[\s\-()]/g, "");
  if (!s.startsWith("+")) {
    // If it's all digits, prepend +. Otherwise leave as-is (it might be a JID
    // or some other identifier — we still try to match consistently).
    if (/^\d+$/.test(s)) s = "+" + s;
  }
  return s;
}

// Ref namespaces. Refs are opaque, case-sensitive strings; never upper-case or
// otherwise normalize them. The prefix is what selects the backend.
const CONTACT_REF_PREFIX = "W-";
const HANDS_REF_PREFIX = "H-";

/** Make a namespaced ref token: "<prefix>-<6 base64url chars>". */
function makeRef(prefix = "W") {
  const raw = randomBytes(6).toString("base64url").slice(0, 6);
  return `${prefix}-${raw}`;
}

/** Pull a likely phone number out of an opaque senderId.
 *  For WhatsApp the senderId is typically the E.164 number as a string. */
function senderIdToPhone(senderId) {
  if (typeof senderId !== "string") return "";
  return normalizePhone(senderId);
}

// ---------------------------------------------------------------------------
// Owner transports
// ---------------------------------------------------------------------------

async function sendOwnerTelegram(api, text) {
  const cfg = resolveConfig(api.config?.plugins?.entries?.[PLUGIN_ID]?.config);
  const body = String(text ?? "");
  try {
    const send = (await api.runtime?.channel?.outbound?.loadAdapter("telegram"))?.sendText;
    if (typeof send !== "function") {
      api.logger?.warn?.(
        `${PLUGIN_ID}: telegram sendText adapter not available; dropping message: ${body.slice(0, 80)}`,
      );
      return false;
    }
    await send({ cfg: api.config, to: cfg.ownerTelegramChatId, text: body });
    return true;
  } catch (err) {
    api.logger?.warn?.(`${PLUGIN_ID}: telegram send failed: ${sanitizeForLog(String(err?.message ?? err))}`);
    return false;
  }
}

/** Answer on the channel the owner's command arrived from.
 *
 *  Doctrine: "Delivery failure -> Telegram." Telegram is the floor for every
 *  owner-facing message, so both a WhatsApp send failure AND an unrecognised
 *  channel fall back to it rather than silently dropping the reply. */
async function sendOwnerResponse(api, channel, text) {
  const cfg = resolveConfig(api.config?.plugins?.entries?.[PLUGIN_ID]?.config);
  if (channel === "whatsapp") {
    try {
      const send = (await api.runtime?.channel?.outbound?.loadAdapter("whatsapp"))?.sendText;
      if (typeof send === "function") {
        await send({ cfg: api.config, to: cfg.ownerWhatsAppNumber, text: String(text ?? "") });
        return true;
      }
    } catch (err) {
      api.logger?.warn?.(
        `${PLUGIN_ID}: WhatsApp owner reply failed: ${sanitizeForLog(String(err?.message ?? err))}`,
      );
    }
    return sendOwnerTelegram(api, text);
  }
  return sendOwnerTelegram(api, text);
}

// ===========================================================================
// ORIGIN-MARKER SEAM  (deliberately NOT implemented — read before extending)
// ===========================================================================
// The doctrine above wants hands approval PROMPTS routed by initiation:
// autonomous -> Telegram always, interactive/admin-initiated -> the channel
// already in use, detached -> by immediate initiator. That routing is NOT
// implemented here, and it cannot be faked from what this plugin currently
// sees.
//
// What is missing, precisely:
//   * The plugin's own prompt-send points are evaluateInbound(api, phone,
//     preview) and evaluateOutbound(api, phone, preview). They receive a phone
//     number and a preview string. No provider, no session lineage, no
//     autonomous-vs-interactive flag, no owner-active-channel hint.
//   * The only channel context that exists upstream is ctx.messageProvider in
//     before_agent_run and ctx.channelId in message_sending — and on both of
//     those paths the value is necessarily "whatsapp", because that is the
//     STRANGER's channel. It says nothing about how the owner should be
//     reached, so it is useless for this decision.
//   * Hands approval prompts are not sent by this plugin at all. They are sent
//     by the ollie-hands engine; this plugin only relays the owner's answer
//     back to /consent. The initiation marker therefore has to originate in the
//     hands consent request and be plumbed out to whatever sends the prompt.
//     That is a separate change spanning both components.
//
// Until that marker exists, Telegram is the hard floor for every prompt:
// sendOwnerTelegram() is called directly at both contact-gating prompt sites.
// (For W- contact gating that is not a stopgap — it is permanent doctrine.)
//
// When the marker lands, it attaches here:
//   1. evaluateInbound/evaluateOutbound grow an `origin` argument carrying
//      { initiator: "autonomous" | "interactive", channel } from the caller.
//   2. A resolveOwnerPromptChannel(origin) helper lands next to
//      sendOwnerResponse and returns "telegram" for autonomous/detached/unknown
//      and origin.channel for interactive.
//   3. The two sendOwnerTelegram() prompt calls below become
//      sendOwnerResponse(api, resolveOwnerPromptChannel(origin), text) — and
//      only for H- flows. W- stays Telegram-only regardless of origin.
// Do not add a heuristic here in the meantime; guessing initiation wrong is
// exactly the failure mode the doctrine exists to prevent.
// ===========================================================================

// ---------------------------------------------------------------------------
// Decision helpers
// ---------------------------------------------------------------------------

/** Look up a pending entry by its E.164 phone (any ref). */
function findPendingByPhone(state, phone) {
  if (!phone) return null;
  for (const [ref, entry] of Object.entries(state.pending)) {
    if (entry.from === phone) return { ref, entry };
  }
  return null;
}

function isExpired(entry, nowMs) {
  if (!entry?.expiresAt) return false;
  const t = Date.parse(entry.expiresAt);
  return Number.isFinite(t) && t < nowMs;
}

function pendingSummary(ref, entry) {
  // Preserve the exact ref bytes; do not normalize case.
  return {
    ref,
    backend: "contact",
    summary: `WhatsApp ${entry.kind} ${entry.from}`,
  };
}

/** Prune expired contact pendings and summarize the rest. Caller holds the lock. */
async function listContactPendingLocked(state, now = Date.now()) {
  let changed = false;
  for (const [ref, entry] of Object.entries(state.pending)) {
    if (isExpired(entry, now)) {
      delete state.pending[ref];
      changed = true;
    }
  }
  const pending = Object.entries(state.pending).map(([ref, entry]) => pendingSummary(ref, entry));
  return { pending, changed };
}

function previewFor(text, max = 120) {
  if (typeof text !== "string") return "";
  // Collapse newlines for the Telegram push.
  const flat = text.replace(/\s+/g, " ").trim();
  if (flat.length <= max) return flat;
  return flat.slice(0, max - 1) + "\u2026";
}

// ---------------------------------------------------------------------------
// Hook handlers
// ---------------------------------------------------------------------------

/** Common gate: returns { allowed, action, ref?, state } */
async function evaluateInbound(api, phone, preview) {
  const cfg = resolveConfig(api.config?.plugins?.entries?.[PLUGIN_ID]?.config);
  const filePath = cfg.approvalsFile;
  return withLock(async () => {
    const state = await readState(filePath, api.logger);
    const now = Date.now();

    if (state.approved.includes(phone)) {
      logApprovalEvent(api, "info", { event: "wa_request_decision", direction: "inbound", decision: "allow", reason: "approved" });
      return { allowed: true, reason: "approved" };
    }
    if (state.blocked.includes(phone)) {
      logApprovalEvent(api, "info", { event: "wa_request_decision", direction: "inbound", decision: "block", reason: "blocked" });
      return { allowed: false, reason: "blocked", silent: true };
    }

    // Unknown. Dedupe on pending.
    const existing = findPendingByPhone(state, phone);
    if (existing && !isExpired(existing.entry, now)) {
      logApprovalEvent(api, "info", { event: "wa_request_dedup", direction: "inbound", decision: "block", reason: "pending", ref: existing.ref, dedup: true });
      return { allowed: false, reason: "pending", ref: existing.ref, dedup: true };
    }
    if (existing && isExpired(existing.entry, now)) {
      // Drop expired, then create fresh.
      delete state.pending[existing.ref];
    }
    const ref = makeRef("W");
    const requestedAt = new Date(now).toISOString();
    const expiresAt = new Date(now + cfg.requestTimeoutMinutes * 60_000).toISOString();
    state.pending[ref] = { from: phone, preview, kind: "inbound", requestedAt, expiresAt };

    let writeOutcome = "ok";
    try {
      await writeState(filePath, state, api.logger);
    } catch (err) {
      writeOutcome = "failed";
      api.logger?.warn?.(`${PLUGIN_ID}: state write failed (${filePath}): ${err?.message ?? err}`);
      // Continue — we still block + push the request.
    }
    logApprovalEvent(api, "info", { event: "wa_request_create", direction: "inbound", ref, outcome: writeOutcome });

    // Push to owner (best-effort; gate is still closed).
    // W- contact gating is Telegram-only BY DOCTRINE, permanently — not because
    // of the missing origin marker. See the ORIGIN-MARKER SEAM block.
    const senderLabel = preview ? `'<${previewFor(preview, 80)}>'` : "(no preview)";
    const sent = await sendOwnerTelegram(
      api,
      `\ud83d\udcec New WhatsApp from ${phone} ${senderLabel} (ref ${ref})\nReply approve or deny. If several requests are pending, include the ref.`,
    );
    logApprovalEvent(api, "info", { event: "wa_request_send", direction: "inbound", ref, outcome: sent ? "sent" : "failed" });

    return { allowed: false, reason: "pending", ref, dedup: false };
  });
}

async function evaluateOutbound(api, phone, preview) {
  const cfg = resolveConfig(api.config?.plugins?.entries?.[PLUGIN_ID]?.config);
  const filePath = cfg.approvalsFile;
  return withLock(async () => {
    const state = await readState(filePath, api.logger);
    const now = Date.now();

    if (state.approved.includes(phone)) {
      logApprovalEvent(api, "info", { event: "wa_request_decision", direction: "outbound", decision: "allow", reason: "approved" });
      return { allowed: true, reason: "approved" };
    }
    if (state.blocked.includes(phone)) {
      logApprovalEvent(api, "info", { event: "wa_request_decision", direction: "outbound", decision: "block", reason: "blocked" });
      return { allowed: false, reason: "blocked" };
    }

    const existing = findPendingByPhone(state, phone);
    if (existing && !isExpired(existing.entry, now)) {
      logApprovalEvent(api, "info", { event: "wa_request_dedup", direction: "outbound", decision: "block", reason: "pending", ref: existing.ref, dedup: true });
      return { allowed: false, reason: "pending", ref: existing.ref, dedup: true };
    }
    if (existing && isExpired(existing.entry, now)) {
      delete state.pending[existing.ref];
    }
    const ref = makeRef("W");
    const requestedAt = new Date(now).toISOString();
    const expiresAt = new Date(now + cfg.requestTimeoutMinutes * 60_000).toISOString();
    state.pending[ref] = { from: phone, preview, kind: "outbound", requestedAt, expiresAt };

    let writeOutcome = "ok";
    try {
      await writeState(filePath, state, api.logger);
    } catch (err) {
      writeOutcome = "failed";
      api.logger?.warn?.(`${PLUGIN_ID}: state write failed (${filePath}): ${err?.message ?? err}`);
    }
    logApprovalEvent(api, "info", { event: "wa_request_create", direction: "outbound", ref, outcome: writeOutcome });

    // Telegram-only by doctrine; see evaluateInbound and the seam block.
    const senderLabel = preview ? `'<${previewFor(preview, 80)}>'` : "(no preview)";
    const sent = await sendOwnerTelegram(
      api,
      `\ud83d\udcec Ollie wants to send WhatsApp to ${phone} ${senderLabel} (ref ${ref})\nReply approve or deny. If several requests are pending, include the ref.`,
    );
    logApprovalEvent(api, "info", { event: "wa_request_send", direction: "outbound", ref, outcome: sent ? "sent" : "failed" });

    return { allowed: false, reason: "pending", ref, dedup: false };
  });
}

async function applyOwnerReply(api, ref, decision /* "approve" | "deny" */) {
  const cfg = resolveConfig(api.config?.plugins?.entries?.[PLUGIN_ID]?.config);
  const filePath = cfg.approvalsFile;
  return withLock(async () => {
    const state = await readState(filePath, api.logger);
    const entry = state.pending[ref];
    if (!entry) {
      logApprovalEvent(api, "info", { event: "wa_owner_result", ref, decision, outcome: "no_such_ref" });
      return { ok: false, reason: "no-such-ref" };
    }
    // Atomic expiry check under the lock (shares semantics with the button path).
    if (isExpired(entry, Date.now())) {
      delete state.pending[ref];
      try { await writeState(filePath, state, api.logger); } catch {}
      logApprovalEvent(api, "info", { event: "wa_owner_result", ref, decision, outcome: "expired" });
      return { ok: false, reason: "expired" };
    }
    const phone = entry.from;
    delete state.pending[ref];
    if (decision === "approve") {
      if (!state.approved.includes(phone)) state.approved.push(phone);
      state.blocked = state.blocked.filter((p) => p !== phone);
    } else {
      if (!state.blocked.includes(phone)) state.blocked.push(phone);
      state.approved = state.approved.filter((p) => p !== phone);
    }
    try {
      await writeState(filePath, state, api.logger);
    } catch (err) {
      api.logger?.warn?.(`${PLUGIN_ID}: state write failed on owner reply: ${err?.message ?? err}`);
      logApprovalEvent(api, "warn", { event: "wa_owner_result", direction: entry.kind ?? "inbound", ref, decision, outcome: "failed" });
      return { ok: false, reason: "write-failed" };
    }
    logApprovalEvent(api, "info", { event: "wa_owner_write", direction: entry.kind ?? "inbound", ref, decision, outcome: "ok" });
    logApprovalEvent(api, "info", { event: "wa_owner_result", direction: entry.kind ?? "inbound", ref, decision, outcome: "applied" });
    return { ok: true, phone, decision };
  });
}

// ---------------------------------------------------------------------------
// Owner command language
//
// The base grammar lives in ./approval-command.js (shared, byte-identical with
// the box): `approve|deny [H-xxxx|W-xxxx]`. An omitted ref is legal and is
// resolved against the pending inventory.
//
// On top of that we accept ONE optional trailing argument: the action digest,
// as `approve <ref> <digest>`. Button taps carry the digest implicitly (the
// relay reads it from the engine inventory), but Telegram's 64-byte callback
// payload cannot hold it — so when buttons misbehave, typing the digest is the
// degradation path that keeps digest-bound resolution available by hand.
// ---------------------------------------------------------------------------

const DIGEST_SUFFIX_RE = /\s+([a-fA-F0-9]{16,64})\s*$/;

/** @returns {{ decision: "approve"|"deny", ref: string|null, digest: string }|null} */
function parseOwnerCommand(text) {
  if (typeof text !== "string") return null;
  const trimmed = text.trim();
  const digestMatch = trimmed.match(DIGEST_SUFFIX_RE);
  const digest = digestMatch ? digestMatch[1] : "";
  const withoutDigest = digestMatch ? trimmed.slice(0, digestMatch.index) : trimmed;
  const base = parseApprovalCommand(withoutDigest);
  if (!base) return null;
  return { decision: base.decision, ref: base.ref, digest };
}

function parseHandsModeCommand(text) {
  if (typeof text !== "string") return null;
  const command = text.trim();
  if (command === "hands mode") return { operation: "status" };
  if (command === "hands normal") return { operation: "set", mode: "normal" };
  if (command === "hands bypass") return { operation: "set", mode: "bypass" };
  return null;
}

// ---------------------------------------------------------------------------
// Unified owner-approval router
//
// ONE path for every approve/deny the owner sends, on any accepted channel.
// The ref prefix names the backend (W- contact gating, H- hands actions); a
// bare command is resolved against the live pending inventory. The caller
// blocks the agent run, so the brain is never in the approval path.
// ---------------------------------------------------------------------------

const HANDS_DEFAULT_CONSENT_URL = "http://<TAILSCALE_IP>:3200/consent";

/** Hands engine /consent target + independent approval credential. */
function resolveHandsConfig(api) {
  const cfg = api?.config?.plugins?.entries?.[PLUGIN_ID]?.config ?? {};
  const approvalToken =
    typeof cfg.handsApprovalToken === "string" ? cfg.handsApprovalToken.trim() : "";
  const url =
    typeof cfg.handsConsentUrl === "string" && cfg.handsConsentUrl.trim()
      ? cfg.handsConsentUrl.trim()
      : HANDS_DEFAULT_CONSENT_URL;
  return { url, approvalToken };
}

async function requestHands(api, url, options = {}) {
  const { approvalToken } = resolveHandsConfig(api);
  if (!approvalToken) return { ok: false, handled: true,
    error: "hands approval token not configured" };

  const cfg = resolveConfig(api.config?.plugins?.entries?.[PLUGIN_ID]?.config);
  const timeoutMs = Number.isFinite(cfg.hookTimeoutMs) && cfg.hookTimeoutMs > 1000
    ? Math.min(cfg.hookTimeoutMs, 10_000)
    : 5000;
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  const timer = controller
    ? setTimeout(() => controller.abort(new Error("hands fetch timeout")), timeoutMs)
    : null;
  let resp;
  try {
    resp = await fetch(url, {
      ...options,
      headers: { ...(options.headers ?? {}), Authorization: `Bearer ${approvalToken}` },
      ...(controller ? { signal: controller.signal } : {}),
    });
  } catch {
    return { ok: false, handled: true, transient: true,
      error: "network error contacting hands backend" };
  } finally {
    if (timer) clearTimeout(timer);
  }
  let body;
  try {
    body = await resp.json();
  } catch {
    return { ok: false, handled: true, transient: true,
      error: "malformed response from hands backend", status: resp.status };
  }
  const ok = resp.status === 200 && body?.ok === true;
  return { ok, handled: true, transient: !ok && resp.status !== 404,
    body, status: resp.status };
}

async function postHandsConsent(api, ref, approve, enableBypass = false, scriptHash = "") {
  const { url } = resolveHandsConfig(api);
  return requestHands(api, url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ref,
      approve,
      ...(scriptHash ? { script_hash: scriptHash } : {}),
      ...(enableBypass ? { enable_bypass: true } : {}),
    }),
  });
}

/** Fetch the engine's pending-approval inventory (approval-token-authed).
 *  Button-tap approvals cannot carry the bound action digest in Telegram's
 *  64-byte callback payload, so the relay reads it from the inventory and
 *  attaches it to the /consent POST — keeping digest-bound resolution strict
 *  for both the typed and the tap path.
 */
async function fetchHandsPending(api) {
  const { url } = resolveHandsConfig(api);
  const result = await requestHands(api, url, { method: "GET" });
  if (!result.ok) {
    return { ok: false, error: result.body?.error ?? result.error ?? "inventory request failed" };
  }
  const pending = Array.isArray(result.body?.pending) ? result.body.pending : [];
  return { ok: true, pending };
}

/** Inventory rows shaped for the ambiguity prompt. Throws when unreachable so
 *  routeOwnerApproval can refuse rather than guess. */
async function listHandsPending(api) {
  const inv = await fetchHandsPending(api);
  if (!inv.ok) throw new Error(inv.error);
  // Preserve exact ref bytes from the backend; refs are opaque, case-sensitive.
  return inv.pending
    .filter((item) => item && typeof item.ref === "string")
    .map((item) => ({ ref: item.ref, backend: "hands", summary: item.preview ?? "Hands action" }));
}

async function listContactPending(api) {
  const cfg = resolveConfig(api.config?.plugins?.entries?.[PLUGIN_ID]?.config);
  const filePath = cfg.approvalsFile;
  return withLock(async () => {
    const state = await readState(filePath, api.logger);
    const { pending, changed } = await listContactPendingLocked(state);
    if (changed) {
      try {
        await writeState(filePath, state, api.logger);
      } catch (err) {
        api.logger?.warn?.(`${PLUGIN_ID}: state write failed while pruning pending inventory: ${err?.message ?? err}`);
      }
    }
    return pending;
  });
}

async function handleHandsModeCommand(api, command) {
  const { url } = resolveHandsConfig(api);
  const modeUrl = new URL(url);
  modeUrl.pathname = modeUrl.pathname.replace(/\/consent\/?$/, "/mode");
  const result = await requestHands(api, modeUrl.toString(), command.operation === "status" ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode: command.mode }),
  });
  if (!result.ok) return `❗ Hands mode request failed: ${result.body?.error ?? result.error ?? "unknown error"}.`;
  if (result.body?.mode === "bypass") {
    return "✅ Hands mode: bypass. Permitted sends, posts, deletes, and purchases can execute without another approval. Send “hands normal” to stop; restart also resets to normal.";
  }
  return "✅ Hands mode: normal. Consequential actions require owner approval.";
}

async function handleContactApproval(api, ref, decision) {
  const r = await applyOwnerReply(api, ref, decision);
  if (r.reason === "no-such-ref") return { handled: false };
  if (r.ok) {
    const verb = decision === "approve" ? "✅ Approved" : "⛔ Denied";
    return {
      handled: true,
      message: `${verb} ${r.phone} (ref ${ref}). ${
        decision === "approve"
          ? "Next message from this number will reach Ollie."
          : "Future messages from this number will be blocked."
      }`,
    };
  }
  return { handled: true, message: `❗ Couldn't apply ref ${ref} (${r.reason}).` };
}

/** @param digest owner-typed action digest, "" when the owner did not supply one. */
async function handleHandsApproval(api, ref, decision, digest = "") {
  const r = await postHandsConsent(api, ref, decision === "approve", false, digest);
  if (!r.ok) {
    if (!r.handled) return { handled: false };
    return {
      handled: true,
      message: `❗ Hands approval rejected: ${r.body?.error ?? r.error ?? "unknown error"}.`,
    };
  }
  return {
    handled: true,
    message: `${decision === "approve" ? "✅ Approved" : "⛔ Denied"} ref ${ref}. Ollie ${
      decision === "approve" ? "is proceeding." : "stopped."
    }`,
  };
}

function inventoryPrompt(decision, pending) {
  const lines = pending.map((item) => `- ${item.ref}: ${item.summary}`);
  return `❗ There are ${pending.length} pending approvals. Reply ${decision} H-XXXX or W-XXXX.\n${lines.join("\n")}`;
}

/** Doctrine refusal for a contact decision attempted on a non-Telegram channel. */
const CONTACT_WRONG_CHANNEL_MESSAGE =
  "❗ WhatsApp contact approvals (W-) can only be decided on Telegram. The request was triggered by a stranger's message, so there is no guaranteed WhatsApp window — open Telegram and answer there.";

/**
 * Route one owner approve/deny command to the backend named by its typed ref.
 *
 * @param options.allowContact  When false, this channel may NOT decide W-
 *   contact gating: an explicit W- ref is refused before any state is touched,
 *   and contact pendings are excluded from the ambiguity inventory so a bare
 *   `approve` can never auto-select one (which would also leak a stranger's
 *   number onto that channel). Doctrine: contact gating is Telegram-only, both
 *   directions.
 * @returns the owner-facing reply string.
 */
async function routeOwnerApproval(api, cmd, options = {}) {
  const allowContact = options.allowContact !== false;
  const digest = typeof cmd.digest === "string" ? cmd.digest : "";
  let selected = cmd.ref;
  let pending = null;

  // Refuse an explicit out-of-channel contact decision up front, before we read
  // or write any contact state at all.
  if (selected && selected.startsWith(CONTACT_REF_PREFIX) && !allowContact) {
    logApprovalEvent(api, "warn", {
      event: "command_channel_refused", ref: selected, decision: cmd.decision,
      backend: "contact", outcome: "wrong_channel",
    });
    return CONTACT_WRONG_CHANNEL_MESSAGE;
  }

  if (!selected) {
    try {
      const handsPendingPromise = listHandsPending(api).then(
        (value) => ({ ok: true, value }),
        (error) => ({ ok: false, error }),
      );
      pending = await withLock(async () => {
        let contactPending = [];
        if (allowContact) {
          const cfg = resolveConfig(api.config?.plugins?.entries?.[PLUGIN_ID]?.config);
          const state = await readState(cfg.approvalsFile, api.logger);
          const listed = await listContactPendingLocked(state);
          contactPending = listed.pending;
          if (listed.changed) {
            try {
              await writeState(cfg.approvalsFile, state, api.logger);
            } catch (err) {
              api.logger?.warn?.(`${PLUGIN_ID}: state write failed while pruning pending inventory: ${err?.message ?? err}`);
            }
          }
        }
        const handsPendingResult = await handsPendingPromise;
        if (!handsPendingResult.ok) throw handsPendingResult.error;
        return [...contactPending, ...handsPendingResult.value];
      });
    } catch (err) {
      api.logger?.warn?.(`${PLUGIN_ID}: pending approval inventory failed: ${err?.message ?? err}`);
      logApprovalEvent(api, "warn", {
        event: "command_inventory_failed", decision: cmd.decision, outcome: "unavailable",
        error: String(err?.message ?? err),
      });
      return "❗ I couldn't verify the pending approval inventory. Reply approve H-XXXX, deny H-XXXX, approve W-XXXX, or deny W-XXXX.";
    }
    const choice = selectPending(cmd, pending);
    if (choice.error) {
      if (pending.length > 1) return inventoryPrompt(cmd.decision, pending);
      return `❗ ${choice.error}`;
    }
    selected = choice.ref;
  }

  logApprovalEvent(api, "info", {
    event: "command_backend_attempt", ref: selected, decision: cmd.decision,
    backend: selected.startsWith(HANDS_REF_PREFIX) ? "hands" : "contact",
  });

  if (selected.startsWith(CONTACT_REF_PREFIX)) {
    // Single enforcement point: even a ref chosen from the inventory is checked.
    if (!allowContact) {
      logApprovalEvent(api, "warn", {
        event: "command_channel_refused", ref: selected, decision: cmd.decision,
        backend: "contact", outcome: "wrong_channel",
      });
      return CONTACT_WRONG_CHANNEL_MESSAGE;
    }
    const result = await handleContactApproval(api, selected, cmd.decision);
    logApprovalEvent(api, "info", {
      event: "command_final_result", ref: selected, decision: cmd.decision,
      backend: "contact", handled: result.handled === true,
    });
    return result.handled
      ? result.message
      : `❗ No pending approval with ref "${selected}". It may have timed out.`;
  }
  if (selected.startsWith(HANDS_REF_PREFIX)) {
    const result = await handleHandsApproval(api, selected, cmd.decision, digest);
    logApprovalEvent(api, "info", {
      event: "command_final_result", ref: selected, decision: cmd.decision,
      backend: "hands", handled: result.handled === true,
    });
    return result.handled
      ? result.message
      : `❗ No pending approval with ref "${selected}". It may have timed out.`;
  }

  const known = pending ?? [];
  if (known.length > 0) return inventoryPrompt(cmd.decision, known);
  return "❗ Approval refs must start with H- or W-.";
}

// ---------------------------------------------------------------------------
// Telegram inline-button callback path.
// Callback refs are opaque and case-sensitive; the payload never carries a
// digest (it does not fit in Telegram's 64-byte limit).
// ---------------------------------------------------------------------------

const APPROVAL_CALLBACK_NS = "ollie_approval";
const APPROVAL_CALLBACK_VERSION = "v1";

function parseApprovalCallback(ctxLike) {
  if (!ctxLike || typeof ctxLike !== "object") return { handled: false, stage: "no_ctx" };
  if (ctxLike.channel && ctxLike.channel !== "telegram") return { handled: false, stage: "wrong_channel" };
  const data = typeof ctxLike.data === "string"
    ? ctxLike.data
    : (typeof ctxLike.callback_data === "string" ? ctxLike.callback_data : "");
  if (!data) return { handled: false, stage: "no_data" };
  const parts = data.split(":");
  if (parts.length !== 4 || parts[0] !== APPROVAL_CALLBACK_NS || parts[1] !== APPROVAL_CALLBACK_VERSION) {
    return { handled: false, stage: "wrong_namespace_version" };
  }
  const decision = parts[2];
  const ref = parts[3];
  if (decision !== "a" && decision !== "b" && decision !== "d") {
    return { handled: true, malformed: true, stage: "bad_decision" };
  }
  // Only the uppercase H- namespace reaches the button path; contact gating has
  // no buttons. Mixed/lowercase prefixes are rejected here.
  if (!/^H-[A-Za-z0-9_-]{1,61}$/.test(ref)) {
    return { handled: true, malformed: true, stage: "bad_ref_shape" };
  }
  return {
    handled: true,
    ref,
    approve: decision !== "d",
    enableBypass: decision === "b",
    stage: decision === "a" ? "approve" : decision === "b" ? "bypass" : "deny",
  };
}

/**
 * Verify the callback is from the authorized owner per the live OpenClaw
 * 2026.5.28 contract. Must satisfy ALL of: channel telegram (enforced by
 * registration + parser), senderId === ownerTelegramChatId, chatId ===
 * ownerTelegramChatId, and auth.isAuthorizedSender === true. Any missing or
 * mismatched field rejects; we never call Hands on failure.
 */
function isAuthorizedOwnerCallback(cfg, ctxLike) {
  const ownerId = String(cfg?.ownerTelegramChatId ?? "");
  return !!ownerId &&
    ctxLike?.senderId === ownerId &&
    ctxLike?.chatId === ownerId &&
    ctxLike?.auth?.isAuthorizedSender === true;
}

async function respondToCallback(api, ctxLike, method, payload) {
  let outcome = "unavailable";
  if (typeof ctxLike?.respond?.[method] === "function") {
    try {
      await ctxLike.respond[method](payload);
      outcome = "ok";
    } catch (err) {
      outcome = "exception";
      api?.logger?.warn?.(
        `${PLUGIN_ID}: callback ${method} error: ${sanitizeForLog(String(err?.message ?? err))}`,
      );
    }
  }
  logApprovalEvent(api, outcome === "exception" ? "warn" : "info", {
    event: "callback_telegram_result", method, outcome,
    message_id: ctxLike?.messageId ?? null,
    update_id: ctxLike?.updateId ?? null,
    callback_id: ctxLike?.callbackId ?? null,
  });
  return outcome === "ok";
}

async function handleApprovalCallback(api, ctxLike) {
  const ids = {
    message_id: ctxLike?.messageId ?? null,
    update_id: ctxLike?.updateId ?? null,
    callback_id: ctxLike?.callbackId ?? null,
  };
  const parsed = parseApprovalCallback(ctxLike);
  logApprovalEvent(api, "info", {
    event: "callback_parse", ...ids, parse: parsed.stage ?? "unknown",
    ref: parsed.ref ?? null,
    decision: parsed.handled && !parsed.malformed
      ? (parsed.enableBypass ? "bypass" : parsed.approve ? "approve" : "deny")
      : null,
    outcome: parsed.handled ? (parsed.malformed ? "malformed" : "matched") : "ignored",
  });
  if (!parsed.handled) return { handled: false };
  if (parsed.malformed) {
    await respondToCallback(api, ctxLike, "reply", { text: "❗ Invalid approval button." });
    logApprovalEvent(api, "info", { event: "callback_complete", ...ids, outcome: "malformed" });
    logCallback(api, ctxLike, {
      ref: null, decision: null,
      cb_ns: APPROVAL_CALLBACK_NS, cb_ver: APPROVAL_CALLBACK_VERSION,
      auth: "malformed", backend_status: null, backend_error_code: null,
      edit_result: "reply",
    });
    return { handled: true, status: "malformed" };
  }

  const decision = parsed.enableBypass ? "bypass" : parsed.approve ? "approve" : "deny";
  const cfg = resolveConfig(api.config?.plugins?.entries?.[PLUGIN_ID]?.config);
  const ownerAuth = isAuthorizedOwnerCallback(cfg, ctxLike) ? "ok" : "denied";
  logApprovalEvent(api, "info", {
    event: "callback_owner_auth", ...ids, ref: parsed.ref, decision, auth: ownerAuth,
  });
  if (ownerAuth !== "ok") {
    await respondToCallback(api, ctxLike, "reply", { text: "❗ Not authorized." });
    logApprovalEvent(api, "info", {
      event: "callback_complete", ...ids, ref: parsed.ref, decision, outcome: "unauthorized",
    });
    logCallback(api, ctxLike, {
      ref: parsed.ref, decision,
      cb_ns: APPROVAL_CALLBACK_NS, cb_ver: APPROVAL_CALLBACK_VERSION,
      auth: "unauthorized", backend_status: null, backend_error_code: null,
      edit_result: "reply",
    });
    return { handled: true, status: "unauthorized" };
  }

  logApprovalEvent(api, "info", {
    event: "callback_hands_start", ...ids, ref: parsed.ref, decision,
  });
  let result;
  try {
    // Strict digest-bound resolution: attach the exact digest bound to this
    // ref. The button payload cannot carry it, so read it from the engine's
    // pending inventory. If the inventory is unreachable, fail closed —
    // nothing is approved and the owner can retry.
    const inv = await fetchHandsPending(api);
    if (!inv.ok) {
      logApprovalEvent(api, "warn", {
        event: "callback_inventory_failed", ...ids, ref: parsed.ref, decision,
        error: inv.error,
      });
      const replied = await respondToCallback(api, ctxLike, "reply", {
        text: "❗ Approval failed: the approval backend is unreachable, so nothing was approved. Please try again.",
      });
      logApprovalEvent(api, "info", {
        event: "callback_complete", ...ids, ref: parsed.ref, decision,
        outcome: "inventory_unavailable",
      });
      logCallback(api, ctxLike, {
        ref: parsed.ref, decision,
        cb_ns: APPROVAL_CALLBACK_NS, cb_ver: APPROVAL_CALLBACK_VERSION,
        auth: "ok", backend_status: null, backend_error_code: "inventory_unavailable",
        edit_result: replied ? "reply_only" : "reply_failed",
      });
      return { handled: true, status: "error" };
    }
    const row = inv.pending.find((r) => r && r.ref === parsed.ref);
    const scriptHash = row && typeof row.script_hash === "string" ? row.script_hash : "";
    result = await postHandsConsent(api, parsed.ref, parsed.approve, parsed.enableBypass, scriptHash);
    logApprovalEvent(api, "info", {
      event: "callback_hands_result", ...ids, ref: parsed.ref, decision,
      outcome: result.ok ? "ok" : "failed", status: result.status ?? null,
      error_code: result.body?.error_code ?? result.body?.code ?? null,
    });
  } catch (err) {
    logApprovalEvent(api, "warn", {
      event: "callback_hands_exception", ...ids, ref: parsed.ref, decision, outcome: "exception",
    });
    throw err;
  }
  const bodyCode = result.body?.error_code ?? result.body?.code;
  const terminal = result.ok || result.status === 404 ||
    bodyCode === "unknown_or_expired" || bodyCode === "malformed_ref";
  let editResult = "none";
  let completion = "error";
  if (terminal) {
    const text = result.ok
      ? parsed.enableBypass
        ? `✅ Bypass enabled and ${parsed.ref} approved. Permitted sends, posts, deletes, and purchases can now run without another approval. Send “hands normal” to stop; restart also resets to normal.`
        : `${parsed.approve ? "✅ Approved" : "⛔ Denied"} ${parsed.ref}. Ollie ${parsed.approve ? "is proceeding." : "stopped."}`
      : `❗ Unknown or expired ref ${parsed.ref}.`;
    const edited = await respondToCallback(api, ctxLike, "editMessage", { text, buttons: [] });
    if (edited) {
      editResult = "editMessage_ok";
    } else {
      const replied = await respondToCallback(api, ctxLike, "reply", { text, buttons: [] });
      editResult = replied ? "reply_fallback" : "editMessage_failed";
    }
    completion = result.ok ? "applied" : "expired";
  } else {
    const replied = await respondToCallback(api, ctxLike, "reply", {
      text: `❗ Approval failed: ${result.body?.error ?? result.error ?? "transient error"}.`,
    });
    editResult = replied ? "reply_only" : "reply_failed";
    completion = "transient";
  }
  logApprovalEvent(api, "info", {
    event: "callback_complete", ...ids, ref: parsed.ref, decision,
    outcome: completion, status: result.status ?? null, error_code: bodyCode ?? null,
  });
  logCallback(api, ctxLike, {
    ref: parsed.ref, decision,
    cb_ns: APPROVAL_CALLBACK_NS, cb_ver: APPROVAL_CALLBACK_VERSION,
    auth: "ok",
    backend_status: typeof result.status === "number" ? result.status : null,
    backend_error_code: bodyCode ?? null,
    edit_result: editResult,
  });
  return { handled: true, status: result.ok ? "ok" : terminal ? "expired" : "error", body: result.body };
}

// ---------------------------------------------------------------------------
// Plugin entry
// ---------------------------------------------------------------------------

const ollie_wa_approval_default = definePluginEntry({
  id: PLUGIN_ID,
  name: "Ollie Owner-Approval Router",
  description:
    "Single pre-LLM owner-approval path. Gates new WhatsApp contacts (Telegram-only, both directions) AND relays computer-use (hands) action approvals to the engine /consent endpoint with the bound action digest. Hands approvals are also accepted from owner WhatsApp; the brain is never in the approval path.",
  register(api) {
    const cfg0 = resolveConfig(api.config?.plugins?.entries?.[PLUGIN_ID]?.config);
    api.logger?.info?.(
      `${PLUGIN_ID}: loaded (enabled=${cfg0.enabled}, ownerTelegramChatId=${cfg0.ownerTelegramChatId}, approvalsFile=${cfg0.approvalsFile})`,
    );

    // ----- before_prompt_build (WORK_DIGEST ground-truth injection) ---------
    // Registered UNCONDITIONALLY — independent of cfg.enabled, which only gates
    // the WhatsApp approval hooks (a safety switch about exposing Ollie to
    // unapproved senders). Digest injection is pure context and must run every
    // session regardless. Per PluginHookBeforePromptBuildResult, returning
    // { appendSystemContext } appends to the agent system prompt (provider-
    // cacheable). The digest file is regenerated out-of-band by ollie-jobs
    // runners; this hook re-reads on mtime change, so the plugin needs NO
    // restart for digest *content* changes — only for plugin *code* changes.
    // It survives /reset because the system prompt is rebuilt and re-appended.
    // Same priority/timeout conventions as the other hooks; never throws.
    api.on(
      "before_prompt_build",
      async () => {
        try {
          const digest = readWorkDigest(api.logger);
          if (!digest) return undefined; // nothing to inject
          return { appendSystemContext: digest };
        } catch (err) {
          api.logger?.warn?.(`${PLUGIN_ID}: before_prompt_build hook error: ${err?.message ?? err}`);
          return undefined; // never block prompt building
        }
      },
      { priority: 50, timeoutMs: cfg0.hookTimeoutMs },
    );

    if (!cfg0.enabled) {
      api.logger?.info?.(
        `${PLUGIN_ID}: config.enabled=false -> owner approval interception stays active, while WhatsApp contact gating and outbound cancellation remain inert.`,
      );
    }

    // ----- before_agent_run (HARD pre-LLM gate) ----------------------------
    // Per hook-types-B_5108I1.d.ts:868-877, the event carries channelId,
    // senderId, accountId, prompt, messages, systemPrompt, senderIsOwner.
    // The ctx (PluginHookAgentContext, line 274) carries messageProvider
    // ("whatsapp" / "telegram" / "discord" / etc.).
    // Per hook-runner-global-BdHeqZIb.js:676, returning a result with
    // outcome:"block" stops the run. Returning void/undefined is pass.
    api.on(
      "before_agent_run",
      async (event, ctx) => {
        try {
          const provider = (ctx && typeof ctx.messageProvider === "string") ? ctx.messageProvider : "";

          // Unified owner-approval routing. Owner commands on Telegram (all
          // refs) or owner WhatsApp (H- only) are handled HERE, pre-LLM, and
          // the run is blocked, so the brain is never in the approval path.
          if (provider === "telegram" || provider === "whatsapp") {
            const cfgNow = resolveConfig(api.config?.plugins?.entries?.[PLUGIN_ID]?.config);
            const rawSender = typeof event?.senderId === "string"
              ? event.senderId
              : (typeof ctx?.senderId === "string" ? ctx.senderId : "");

            if (provider === "telegram") {
              // Strict owner boundary for the Telegram text fallback (OpenClaw
              // 2026.5.28 BeforeAgentRun). Require: exact owner sender ID,
              // exact private chat (channelId), and the runtime owner signal.
              // Missing evidence fails closed: identity that cannot be resolved
              // gets guest treatment, never owner.
              const isStrictTelegramOwner =
                !!cfgNow.ownerTelegramChatId &&
                rawSender === cfgNow.ownerTelegramChatId &&
                event?.channelId === cfgNow.ownerTelegramChatId &&
                event?.senderIsOwner === true;

              if (isStrictTelegramOwner) {
                const modeCommand = parseHandsModeCommand(event?.prompt ?? "");
                if (modeCommand) {
                  logApprovalEvent(api, "info", { event: "mode_command_ingress",
                    channel: provider, decision: modeCommand.mode ?? "status" });
                  const reply = await handleHandsModeCommand(api, modeCommand);
                  await sendOwnerResponse(api, provider, reply);
                  return { outcome: "block", reason: "hands-mode-handled" };
                }
                const cmd = parseOwnerCommand(event?.prompt ?? "");
                if (cmd) {
                  logApprovalEvent(api, "info", { event: "command_ingress",
                    channel: provider, ref: cmd.ref ?? null, decision: cmd.decision });
                  // Telegram is the full-authority surface: W- and H- both.
                  const reply = await routeOwnerApproval(api, cmd, { allowContact: true });
                  await sendOwnerResponse(api, provider, reply);
                  return { outcome: "block", reason: "owner-approval-handled" };
                }
                // Ordinary non-approval message from the verified owner: unaffected.
              } else {
                // Not strictly verified as owner. If this looks like an
                // owner-control command, fail closed: block it without calling
                // any backend and without letting it reach the LLM. The notice
                // goes to the OWNER's chat (this plugin has no transport for
                // replying to an arbitrary Telegram chat), so it doubles as an
                // attempt notification.
                const modeCommand = parseHandsModeCommand(event?.prompt ?? "");
                const cmd = parseOwnerCommand(event?.prompt ?? "");
                if (modeCommand || cmd) {
                  logApprovalEvent(api, "warn", { event: "command_ingress",
                    channel: provider, ref: cmd?.ref ?? null,
                    decision: cmd?.decision ?? "mode", outcome: "unauthorized" });
                  await sendOwnerResponse(api, provider, "❗ Not authorized.");
                  return { outcome: "block", reason: "owner-auth-failed" };
                }
                // Ordinary non-approval message from a non-verified source: unaffected.
              }
            } else if (provider === "whatsapp") {
              // WhatsApp owner text is authenticated on the identity fields
              // WhatsApp actually has; do not require Telegram-only fields.
              // Fail closed: an empty/unresolvable phone is never the owner,
              // even if the configured owner number were somehow blank.
              const ownerWhatsApp = cfgNow.ownerWhatsAppNumber;
              const senderPhone = senderIdToPhone(rawSender);
              const isOwner = !!ownerWhatsApp && !!senderPhone && senderPhone === ownerWhatsApp;
              if (isOwner) {
                const cmd = parseOwnerCommand(event?.prompt ?? "");
                if (cmd) {
                  logApprovalEvent(api, "info", { event: "command_ingress",
                    channel: provider, ref: cmd.ref ?? null, decision: cmd.decision });
                  // DOCTRINE: H- (hands) commands are legitimate here — the
                  // owner's own message opened the 24h window, which is the
                  // interactive case. W- (contact gating) is NOT: that request
                  // was triggered by a STRANGER's message, so there is no
                  // window guarantee, and WhatsApp must never be the surface
                  // for deciding whether Ollie talks to a stranger.
                  // allowContact:false enforces both the explicit-W- refusal
                  // and the exclusion of W- rows from bare-command resolution.
                  const reply = await routeOwnerApproval(api, cmd, { allowContact: false });
                  await sendOwnerResponse(api, provider, reply);
                  return { outcome: "block", reason: "owner-approval-handled" };
                }
              }
              // Non-owner WhatsApp senders get NO approval parsing and no
              // "not authorized" reply — answering would mean messaging a
              // stranger. They fall through to the contact gate below.
            }
          }

          if (provider !== "whatsapp") return undefined; // pass-through for non-WA
          if (!cfg0.enabled) return undefined; // contact gate disabled; owner interception above still applies
          const phone = senderIdToPhone(event?.senderId ?? ctx?.senderId);
          if (!phone) {
            // No identifiable sender — fail closed.
            return { outcome: "block", reason: "no-sender", message: "Unable to identify sender." };
          }
          // Owner is implicitly approved (also in the file, but short-circuit
          // here to avoid a disk read on the hot path).
          const ownerWhatsApp = resolveConfig(
            api.config?.plugins?.entries?.[PLUGIN_ID]?.config,
          ).ownerWhatsAppNumber;
          if (ownerWhatsApp && phone === ownerWhatsApp) return undefined;

          const result = await evaluateInbound(api, phone, previewFor(event?.prompt ?? ""));
          if (result.allowed) return undefined;
          if (result.reason === "blocked") {
            return { outcome: "block", reason: "blocked", message: "" };
          }
          // pending
          const dedupNote = result.dedup ? " (already pending)" : "";
          return {
            outcome: "block",
            reason: "pending-approval",
            message: `Awaiting owner approval (ref ${result.ref ?? "?"})${dedupNote}.`,
          };
        } catch (err) {
          api.logger?.warn?.(`${PLUGIN_ID}: before_agent_run hook error: ${err?.message ?? err}`);
          // Fail-closed: the host treats invalid decisions as block.
          return { outcome: "block", reason: "hook-error" };
        }
      },
      { priority: 50, timeoutMs: cfg0.hookTimeoutMs },
    );

    // ----- message_sending (outbound cancel) --------------------------------
    // Per hook-types-B_5108I1.d.ts:190, event has to, content, replyToId,
    // threadId, metadata. The ctx (PluginHookMessageContext, line 94) has
    // channelId which the runtime sets to the provider name (verified in
    // deliver-B_snf0tE.js:738-748: `channelId: params.channel`).
    api.on(
      "message_sending",
      async (event, ctx) => {
        try {
          const channel = (ctx && typeof ctx.channelId === "string") ? ctx.channelId : "";
          if (channel !== "whatsapp") return undefined;
          if (!cfg0.enabled) return undefined; // outbound contact gate disabled
          const phone = senderIdToPhone(event?.to);
          if (!phone) return undefined;
          const ownerWhatsApp = resolveConfig(
            api.config?.plugins?.entries?.[PLUGIN_ID]?.config,
          ).ownerWhatsAppNumber;
          if (ownerWhatsApp && phone === ownerWhatsApp) return undefined; // owner always allowed

          const result = await evaluateOutbound(api, phone, previewFor(event?.content ?? ""));
          if (result.allowed) return undefined;
          return {
            cancel: true,
            cancelReason: result.reason === "blocked" ? "blocked" : `pending-approval:${result.ref ?? "?"}`,
          };
        } catch (err) {
          api.logger?.warn?.(`${PLUGIN_ID}: message_sending hook error: ${err?.message ?? err}`);
          // Fail-closed: cancel when in doubt.
          return { cancel: true, cancelReason: "hook-error" };
        }
      },
      { priority: 50, timeoutMs: cfg0.hookTimeoutMs },
    );

    // Keep message_received inert. Owner approve/deny replies are handled in
    // before_agent_run so they block before the brain sees them; message_received
    // is observation-only and cannot stop a message reaching the brain, which is
    // exactly the bug that dragged the LLM into the approval path.
    api.on("message_received", async () => undefined, { priority: 50, timeoutMs: 1000 });

    // ----- Telegram inline approval callback handler (structural path) -----
    // Registered unconditionally (owner path). Contract per live OpenClaw
    // 2026.5.28: api.registerInteractiveHandler({ channel, namespace, handler })
    // and the handler receives ctx with senderId, callback:{payload|data, chatId,
    // messageId}, auth:{isAuthorizedSender}, respond:{reply, editMessage, ...}.
    // Namespace filtering and callback-id dedupe come from the core; the core
    // auto-answers the callback, so we never acknowledge it ourselves. We always
    // return { handled: true } for our namespace so events are consumed.
    if (typeof api.registerInteractiveHandler === "function") {
      api.registerInteractiveHandler({
        channel: "telegram",
        namespace: APPROVAL_CALLBACK_NS,
        handler: async (ctx) => {
          const callback = ctx?.callback;
          const data = callback && (callback.data || callback.payload);
          const dataKind = typeof data === "string"
            ? (data.length > 64 ? "oversized" : "present")
            : "absent";
          // Entry diagnostics: live button routing has failed silently before,
          // so record that the handler was reached at all — on stderr (survives
          // logger misconfiguration) and via the structured stream.
          try {
            process.stderr.write(
              `[ollie-wa-approval] callback received: hasSender=${typeof ctx?.senderId === "string"}, hasChat=${!!(callback && (callback.chatId || callback.chat_id))}, hasData=${!!data}, hasAuth=${!!(ctx?.auth && ctx.auth.isAuthorizedSender)}, ns=${(callback && callback.namespace) || "-"}\n`,
            );
          } catch {}
          try { api?.logger?.info?.(`${PLUGIN_ID}: callback received`); } catch {}
          logApprovalEvent(api, "info", {
            event: "callback_handler_entry",
            message_id: callback?.messageId ?? callback?.message_id ?? null,
            update_id: ctx?.updateId ?? callback?.updateId ?? null,
            callback_id: callback?.id ?? null,
            outcome: dataKind,
          });

          const norm = {
            channel: "telegram",
            senderId: typeof ctx?.senderId === "string" ? ctx.senderId : undefined,
            chatId: callback && (callback.chatId || callback.chat_id),
            data,
            auth: ctx?.auth,
            respond: ctx?.respond,
            messageId: callback && (callback.messageId || callback.message_id),
            updateId: ctx?.updateId ?? callback?.updateId,
            callbackId: callback?.id,
          };
          try {
            await handleApprovalCallback(api, norm);
          } catch (err) {
            try { process.stderr.write("[ollie-wa-approval] handler error\n"); } catch {}
            api?.logger?.error?.(
              `${PLUGIN_ID}: handler error: ${sanitizeForLog(String(err?.message ?? err))}`,
            );
            logApprovalEvent(api, "error", {
              event: "callback_complete",
              message_id: norm.messageId ?? null,
              update_id: norm.updateId ?? null,
              callback_id: norm.callbackId ?? null,
              outcome: "exception",
            });
          }
          // Always claim our namespace (malformed/unauthorized still consumed).
          return { handled: true };
        },
      });
    } else {
      api.logger?.warn?.(`${PLUGIN_ID}: registerInteractiveHandler not available; inline approval buttons will not work.`);
      logApprovalEvent(api, "warn", { event: "callback_handler_registration", outcome: "unavailable" });
    }
  },
});

export const __testHooks = {
  makeRef,
  parseOwnerCommand,
  parseHandsModeCommand,
  routeOwnerApproval,
  handleContactApproval,
  handleHandsApproval,
  handleHandsModeCommand,
  listContactPending,
  listHandsPending,
  fetchHandsPending,
  resolveHandsConfig,
  sendOwnerResponse,
  sendOwnerTelegram,
  readState,
  writeState,
  evaluateInbound,
  evaluateOutbound,
  applyOwnerReply,
  normalizePhone,
  previewFor,
  parseApprovalCallback,
  isAuthorizedOwnerCallback,
  handleApprovalCallback,
  sanitizeForLog,
  logCallback,
  logApprovalEvent,
};

export default ollie_wa_approval_default;
