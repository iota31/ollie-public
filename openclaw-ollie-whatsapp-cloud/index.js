// @ts-check
// =============================================================================
// @ollie/openclaw-whatsapp-cloud
//
// OpenClaw plugin: Meta WhatsApp Cloud API (Graph v21.0) as a channel for Ollie.
//
// WHAT THIS PLUGIN DOES
// ---------------------
// 1. Registers an HTTP route on the gateway loopback server:
//        GET  /plugins/whatsapp-cloud/webhook   -> Meta verification handshake
//        POST /plugins/whatsapp-cloud/webhook   -> inbound message events
// 2. On GET, validates `hub.mode=subscribe` + `hub.verify_token` against the
//    verify token in the secret file, then returns 200 with the raw
//    `hub.challenge` text (or 403 on mismatch).
// 3. On POST, validates `X-Hub-Signature-256` against the app secret with
//    HMAC-SHA256 + crypto.timingSafeEqual, parses the Meta webhook JSON,
//    dedupes by `messages[].id` using a small on-disk set, and ALWAYS
//    returns 200 quickly (Meta retries on non-2xx).
// 4. When `config.enabled` is true AND a secret file is present, well-formed
//    text messages are logged at info level with extracted fields (from E.164,
//    message id, body). Outbound replies are sent via Graph API directly
//    using the documented sendText adapter shape (see "OUTBOUND" below).
//
// CHANNEL ID
// ----------
// The channel id is `whatsapp-cloud`. The existing ollie-wa-approval plugin
// inspects `ctx.messageProvider` in its before_agent_run hook; in its current
// shape it only matches the literal string "whatsapp" (the Baileys channel
// id). To make the approval gate fire for this channel too, ollie-wa-approval
// needs a 2-line patch — see the GO-LIVE checklist in the report.
//
// APPROACH CHOSEN (and why)
// -------------------------
// I read the plugin SDK to compare two paths:
//
// (A) "Real channel plugin" via createChatChannelPlugin (core-CH2cl4po.d.ts:212-220)
//     or defineChannelPluginEntry (core-CH2cl4po.d.ts:141-150) which requires
//     the full security / pairing / threading / outbound adapter contract plus
//     a config schema and runtime exports. This is what the bundled Baileys
//     WhatsApp plugin (@openclaw/whatsapp) does, and what imessage/mattermost
//     do. It is a multi-file plugin with a setup-entry, a runtime-extension,
//     a configSchema, and a channel plugin object exported from
//     channel-plugin-api.js. Doing that correctly for Graph API is a
//     multi-day build (auth flow, account model, conversation bindings,
//     message lifecycle adapter via defineChannelMessageAdapter, durable
//     delivery via sendDurableMessageBatch, etc).
//
// (B) "registerHttpRoute + runChannelInboundEvent" fallback (the user-briefed
//     fallback). Even this requires implementing the 5-stage
//     ChannelTurnAdapter<TRaw> consumed by runChannelInboundEvent:
//     ingest / classify / preflight / resolveTurn / onFinalize, plus a
//     ChannelEventDeliveryAdapter with deliver() to actually send replies.
//     resolveTurn must return a complete AssembledChannelTurn with cfg,
//     channel, accountId, agentId, routeSessionKey, storePath, ctxPayload,
//     recordInboundSession, dispatchReplyWithBufferedBlockDispatcher, and
//     delivery. A half-correct one of these silently drops messages or
//     crashes the gateway.
//
// Given the task scope ("build files only, do NOT activate, do NOT edit
// openclaw.json, do NOT restart the gateway") the right deliverable is the
// HTTP route scaffold that:
//   - mounts at /plugins/whatsapp-cloud/webhook,
//   - implements GET handshake correctly,
//   - implements POST HMAC validation + dedupe + fast 200,
//   - logs the parsed inbound (when enabled) for inspection,
//   - has a documented `outbound` function that performs the Graph API POST
//     and a `notifyAgentInbound` function that, in a follow-up build, will
//     hand the parsed event off to the proper channel machinery
//     (api.runtime.channel.inbound.run) once the agent-injection path is
//     wired in.
//
// INERTNESS
// ---------
// If /home/openclaw/.openclaw/secrets/whatsapp-cloud.json is missing OR
// config.enabled is false, the plugin still mounts the route but every
// handler short-circuits with a single log line and returns 200 (or 403 for
// GET handshake) within a few ms. This guarantees the gateway boots cleanly
// with or without secrets, and never crashes on startup. The user flips
// config.enabled=true on the live box during STAGE 2 of go-live (see the
// checklist in the report).
//
// NO RESTART REQUIRED FOR THIS BUILD
// ----------------------------------
// This build only writes three files into the plugin dir. The gateway is
// NOT reloaded. The new files will not be picked up until the user adds
// /home/openclaw/.openclaw/plugins/ollie-whatsapp-cloud to
// plugins.load.paths in openclaw.json and restarts the gateway (see
// checklist).
// =============================================================================

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import {
  createHmac,
  timingSafeEqual,
  randomBytes,
} from "node:crypto";
import {
  mkdir,
  readFile,
  rename,
  unlink,
  writeFile,
} from "node:fs/promises";
import { existsSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { spawn } from "node:child_process";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PLUGIN_ID = "ollie-whatsapp-cloud";
const ROUTE_PATH = "/plugins/whatsapp-cloud/webhook";
const CHANNEL_ID = "whatsapp-cloud";
const GRAPH_API_VERSION = "v23.0";
const META_GRAPH_BASE = "https://graph.facebook.com";
const SIG_PREFIX = "sha256=";
const DEDUPE_MAX_IDS = 5000;
const DEDUPE_TRIM_TO = 4000;
const MAX_BODY_BYTES = 1024 * 1024; // 1 MiB cap on POST body (Meta won't send more)
const OPENCLAW_BIN = "/home/openclaw/.openclaw/bin/openclaw";
const AGENT_ID = "main";
const AGENT_TIMEOUT_MS = 240000; // 4 min — long enough for a fact-check turn

const DEFAULT_CONFIG = Object.freeze({
  enabled: false,
  secretFile: "~/.openclaw/secrets/whatsapp-cloud.json",
  dedupeFile: "~/.openclaw/plugin-state/ollie-whatsapp-cloud/dedupe.json",
  logEveryInbound: true,
});

// ---------------------------------------------------------------------------
// Tiny helpers
// ---------------------------------------------------------------------------

function expandHome(p) {
  if (typeof p !== "string") return p;
  if (p === "~") return homedir();
  if (p.startsWith("~/") || p.startsWith("~\\")) return join(homedir(), p.slice(2));
  return p;
}

function resolveConfig(pluginConfig) {
  const cfg = pluginConfig && typeof pluginConfig === "object" ? pluginConfig : {};
  return {
    enabled: cfg.enabled === true,
    secretFile: expandHome(
      typeof cfg.secretFile === "string" && cfg.secretFile.trim()
        ? cfg.secretFile.trim()
        : DEFAULT_CONFIG.secretFile,
    ),
    dedupeFile: expandHome(
      typeof cfg.dedupeFile === "string" && cfg.dedupeFile.trim()
        ? cfg.dedupeFile.trim()
        : DEFAULT_CONFIG.dedupeFile,
    ),
    logEveryInbound:
      cfg.logEveryInbound === undefined ? DEFAULT_CONFIG.logEveryInbound : cfg.logEveryInbound === true,
  };
}

/** Read and parse a JSON file; return null on missing/unreadable. */
async function tryReadJson(path) {
  try {
    const raw = await readFile(path, "utf8");
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Secret store
// ---------------------------------------------------------------------------
// File shape (mode 600 recommended):
//   {
//     "phoneNumberId": "<WHATSAPP_PHONE_NUMBER_ID>",
//     "wabaId":        "<WHATSAPP_WABA_ID>",
//     "accessToken":   "<system-user token>",
//     "appSecret":     "<app secret>",
//     "verifyToken":   "<verify token>"
//   }
//
// We never log the secret file contents. We never persist them in memory
// beyond the lifetime of a single request.
// ---------------------------------------------------------------------------

function validateSecret(secret) {
  if (!secret || typeof secret !== "object") return null;
  const required = ["phoneNumberId", "accessToken", "appSecret", "verifyToken"];
  for (const k of required) {
    if (typeof secret[k] !== "string" || !secret[k].trim()) return null;
  }
  return {
    phoneNumberId: secret.phoneNumberId.trim(),
    wabaId: typeof secret.wabaId === "string" ? secret.wabaId.trim() : "",
    accessToken: secret.accessToken.trim(),
    appSecret: secret.appSecret.trim(),
    verifyToken: secret.verifyToken.trim(),
    // Allowlist of permitted senders (E.164, any format). Normalized to digits.
    // Closed by default: if empty/missing, NO sender is answered.
    allowFrom: Array.isArray(secret.allowFrom)
      ? secret.allowFrom.map((s) => String(s).replace(/[^\d]/g, "")).filter(Boolean)
      : [],
    // Groq API key for Whisper STT (voice) + llama-4-scout vision (images).
    groqApiKey: typeof secret.groqApiKey === "string" ? secret.groqApiKey.trim() : "",
    // NVIDIA NIM key for vision fallback (llama-3.2-90b-vision) (optional).
    nvidiaApiKey: typeof secret.nvidiaApiKey === "string" ? secret.nvidiaApiKey.trim() : "",
    // Owner sender (digits): gets full `main` agent. Others -> restricted guest.
    ownerFrom: typeof secret.ownerFrom === "string" ? secret.ownerFrom.replace(/[^\d]/g, "") : "",
  };
}

/**
 * Transcribe a WhatsApp voice note / audio message via Groq Whisper.
 * 1) resolve the Meta media id -> download URL, 2) download the audio bytes
 * (Meta media URLs require the Bearer token), 3) POST to Groq Whisper.
 * Returns the transcript text, or "" on any failure.
 */
async function transcribeAudio(secret, mediaId, logger) {
  if (!secret.groqApiKey) {
    logger?.warn?.(`${PLUGIN_ID}: no groqApiKey in secret -> cannot transcribe voice`);
    return "";
  }
  try {
    const metaRes = await fetch(`${META_GRAPH_BASE}/${GRAPH_API_VERSION}/${encodeURIComponent(mediaId)}`, {
      headers: { Authorization: `Bearer ${secret.accessToken}` },
    });
    if (!metaRes.ok) throw new Error(`media meta ${metaRes.status}`);
    const meta = await metaRes.json();
    const url = meta && typeof meta.url === "string" ? meta.url : "";
    const mime = (meta && typeof meta.mime_type === "string" ? meta.mime_type : "audio/ogg").split(";")[0];
    if (!url) throw new Error("media meta missing url");
    const audioRes = await fetch(url, { headers: { Authorization: `Bearer ${secret.accessToken}` } });
    if (!audioRes.ok) throw new Error(`media download ${audioRes.status}`);
    const buf = Buffer.from(await audioRes.arrayBuffer());
    const ext = mime.includes("mpeg") ? "mp3" : mime.includes("mp4") || mime.includes("m4a") ? "m4a"
      : mime.includes("wav") ? "wav" : mime.includes("webm") ? "webm" : "ogg";
    const form = new FormData();
    form.append("file", new Blob([buf], { type: mime }), `voice.${ext}`);
    form.append("model", "whisper-large-v3-turbo");
    const groqRes = await fetch("https://api.groq.com/openai/v1/audio/transcriptions", {
      method: "POST",
      headers: { Authorization: `Bearer ${secret.groqApiKey}` },
      body: form,
    });
    if (!groqRes.ok) throw new Error(`groq ${groqRes.status}: ${(await groqRes.text().catch(() => "")).slice(0, 200)}`);
    const data = await groqRes.json();
    return (data && typeof data.text === "string" ? data.text : "").trim();
  } catch (e) {
    logger?.warn?.(`${PLUGIN_ID}: transcribe failed: ${e?.message ?? e}`);
    return "";
  }
}

/**
 * Describe a WhatsApp image via Groq llama-4-scout (multimodal, free tier).
 * Same media flow as transcribeAudio: media id -> URL -> bytes, then a vision
 * chat completion with the image as a base64 data URL. The description (with
 * verbatim OCR) becomes the only thing the agent "sees" of the image.
 * Returns "" on any failure.
 */
const VISION_PROMPT = "Describe this image precisely and factually for an assistant that cannot see it. Transcribe ALL visible text VERBATIM (headlines, captions, overlays, watermarks, usernames, dates, numbers). Identify recognizable people, places, logos, screenshots-of-apps. Note anything that looks edited or meme-like. Be complete but do not speculate beyond what is visible.";

/** One OpenAI-style vision chat completion. Returns text or throws. */
async function visionComplete(endpoint, apiKey, model, dataUrl) {
  const res = await fetch(endpoint, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      model,
      max_tokens: 700,
      messages: [{
        role: "user",
        content: [
          { type: "text", text: VISION_PROMPT },
          { type: "image_url", image_url: { url: dataUrl } },
        ],
      }],
    }),
  });
  if (!res.ok) throw new Error(`${res.status}: ${(await res.text().catch(() => "")).slice(0, 200)}`);
  const data = await res.json();
  const out = data?.choices?.[0]?.message?.content;
  return (typeof out === "string" ? out : "").trim();
}

/**
 * Describe a WhatsApp image. Same media flow as transcribeAudio (media id ->
 * URL -> bytes), then a vision chat completion. Tries Groq llama-4-scout first
 * (fast/free); on failure falls back to NVIDIA NIM llama-3.2-90b-vision (also
 * free) so vision survives a Groq outage/quota. Returns "" if both fail.
 */
async function describeImage(secret, mediaId, logger) {
  if (!secret.groqApiKey && !secret.nvidiaApiKey) {
    logger?.warn?.(`${PLUGIN_ID}: no vision key (groq/nvidia) in secret -> cannot describe image`);
    return "";
  }
  let dataUrl;
  try {
    const metaRes = await fetch(`${META_GRAPH_BASE}/${GRAPH_API_VERSION}/${encodeURIComponent(mediaId)}`, {
      headers: { Authorization: `Bearer ${secret.accessToken}` },
    });
    if (!metaRes.ok) throw new Error(`media meta ${metaRes.status}`);
    const meta = await metaRes.json();
    const url = meta && typeof meta.url === "string" ? meta.url : "";
    const mime = (meta && typeof meta.mime_type === "string" ? meta.mime_type : "image/jpeg").split(";")[0];
    if (!url) throw new Error("media meta missing url");
    const imgRes = await fetch(url, { headers: { Authorization: `Bearer ${secret.accessToken}` } });
    if (!imgRes.ok) throw new Error(`media download ${imgRes.status}`);
    const buf = Buffer.from(await imgRes.arrayBuffer());
    if (buf.length > 3.5 * 1024 * 1024) throw new Error(`image too large (${buf.length} bytes)`);
    dataUrl = `data:${mime};base64,${buf.toString("base64")}`;
  } catch (e) {
    logger?.warn?.(`${PLUGIN_ID}: image fetch failed: ${e?.message ?? e}`);
    return "";
  }
  const chain = [];
  if (secret.groqApiKey) chain.push(["groq", "https://api.groq.com/openai/v1/chat/completions", secret.groqApiKey, "meta-llama/llama-4-scout-17b-16e-instruct"]);
  if (secret.nvidiaApiKey) chain.push(["nvidia", "https://integrate.api.nvidia.com/v1/chat/completions", secret.nvidiaApiKey, "meta/llama-3.2-90b-vision-instruct"]);
  for (const [name, endpoint, key, model] of chain) {
    try {
      const out = await visionComplete(endpoint, key, model, dataUrl);
      if (out) return out;
      logger?.warn?.(`${PLUGIN_ID}: vision ${name} returned empty -> trying next`);
    } catch (e) {
      logger?.warn?.(`${PLUGIN_ID}: vision ${name} failed: ${e?.message ?? e} -> trying next`);
    }
  }
  return "";
}

/** Allowlist check: inbound `from` is digits-only (e.g. "<OWNER_PHONE>"). */
function isAllowed(from, allowFrom) {
  if (!Array.isArray(allowFrom) || allowFrom.length === 0) return false;
  return allowFrom.includes(String(from || "").replace(/[^\d]/g, ""));
}

/**
 * Strip CLI noise from captured agent stdout: ANSI color codes and any
 * subsystem log lines (e.g. "[plugins] ...") that leak onto stdout. Belt and
 * suspenders alongside --log-level silent.
 */
/**
 * Defense-in-depth: strip any leaked tool-call protocol blocks from a reply so
 * raw tool calls (e.g. a model emitting <TOOLCALL>[{...}]</TOOLCALL> in
 * prompt-tool mode, or ```json fences around a tool spec) can never reach the
 * user as chat text. Applied to the final body on every path.
 */
function stripToolCallLeak(s) {
  if (!s) return s;
  let out = s
    .replace(/<\/?TOOL_?CALLS?[^>]*>/gi, "")
    .replace(/<\/?function_?calls?[^>]*>/gi, "")
    .replace(/<\/?(antml:)?invoke[^>]*>/gi, "");
  // Drop a line that is purely a JSON tool-spec array like
  // [{"name":"exec","arguments":{...}}]
  out = out.split("\n").filter((ln) => {
    const l = ln.trim();
    return !/^\[?\s*\{\s*"name"\s*:\s*".+?"\s*,\s*"(arguments|input|parameters)"\s*:/.test(l);
  }).join("\n");
  return out.trim();
}

function sanitizeAgentOutput(s) {
  if (!s) return "";
  // eslint-disable-next-line no-control-regex
  const noAnsi = s.replace(/\x1b\[[0-9;]*m/g, "");
  const kept = noAnsi.split("\n").filter((ln) => {
    const l = ln.trim();
    if (!l) return true;
    if (/^\[(plugins|gateway|reload|ws|telegram|heartbeat|health-monitor|node|agent\/embedded|trace|diag)\]/i.test(l)) return false;
    if (/plugins\.allow is empty/i.test(l)) return false;
    return true;
  });
  return kept.join("\n").trim();
}

/**
 * Run one Ollie agent turn for a given sender, returning the reply text.
 * Spawns the openclaw CLI (with --log-level silent so logs don't pollute the
 * reply) and a per-sender session key. Best-effort: "" on failure/timeout.
 */
function runAgentTurn(text, sessionKey, logger, agentId = AGENT_ID) {
  return new Promise((resolve) => {
    let out = "";
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(sanitizeAgentOutput(v)); } };
    let child;
    try {
      child = spawn(
        OPENCLAW_BIN,
        ["--log-level", "silent", "agent", "--agent", agentId, "--session-key", sessionKey, "-m", text],
        { stdio: ["ignore", "pipe", "pipe"] },
      );
    } catch (e) {
      logger?.warn?.(`${PLUGIN_ID}: agent spawn failed: ${e?.message ?? e}`);
      finish("");
      return;
    }
    const timer = setTimeout(() => {
      logger?.warn?.(`${PLUGIN_ID}: agent turn timed out (${AGENT_TIMEOUT_MS}ms)`);
      try { child.kill("SIGKILL"); } catch { /* ignore */ }
      finish(out);
    }, AGENT_TIMEOUT_MS);
    child.stdout.on("data", (d) => { out += d.toString(); });
    child.stderr.on("data", () => { /* CLI logs -> ignore */ });
    child.on("error", (e) => {
      clearTimeout(timer);
      logger?.warn?.(`${PLUGIN_ID}: agent process error: ${e?.message ?? e}`);
      finish(out);
    });
    child.on("close", () => { clearTimeout(timer); finish(out); });
  });
}

/**
 * FAST path: run the turn in-process via the warm gateway runtime
 * (api.runtime.agent.runEmbeddedAgent) — no cold CLI start. Tools enabled,
 * Ollie's config/model (+ fallbacks)/personality. Per-sender session file for
 * conversation continuity. Returns reply text, or null on failure/empty so the
 * caller can fall back to the CLI spawn.
 */
async function runAgentEmbedded(ctx, text, digits, logger, epoch = 0) {
  const run = ctx?.runtime?.agent?.runEmbeddedAgent;
  const config = ctx?.config;
  if (typeof run !== "function") return null;
  try {
    const dir = join(homedir(), ".openclaw", "plugin-state", "ollie-whatsapp-cloud", "sessions");
    await mkdir(dir, { recursive: true }).catch(() => {});
    const suffix = epoch > 0 ? `-e${epoch}` : "";
    const sessionFile = join(dir, `${digits}${suffix}.json`);
    // Walk the configured model chain (primary + fallbacks) ourselves: the
    // embedded runner pins whatever provider/model we hand it, so a hard
    // provider error (e.g. MiniMax weekly token-plan exhausted) must be
    // failed-over here, not just dropped to the slow CLI path.
    const defModel = config?.agents?.defaults?.model;
    const chain = [
      defModel?.primary || "minimax/MiniMax-M3",
      ...(Array.isArray(defModel?.fallbacks) ? defModel.fallbacks : []),
    ];
    for (const modelKey of chain) {
      const slash = modelKey.indexOf("/");
      const provider = slash >= 0 ? modelKey.slice(0, slash) : "minimax";
      const model = slash >= 0 ? modelKey.slice(slash + 1) : modelKey;
      try {
        const result = await run({
          sessionId: `whatsapp-cloud-${digits}${suffix}`,
          sessionFile,
          workspaceDir: config?.agents?.defaults?.workspace ?? process.cwd(),
          config,
          prompt: text,
          timeoutMs: AGENT_TIMEOUT_MS,
          runId: `wac-${digits}-${Date.now()}`,
          provider,
          model,
          authProfileId: `${provider}:global`,
          authProfileIdSource: "auto",
        });
        const payloads = result && typeof result === "object" && "payloads" in result ? result.payloads : undefined;
        const out = (payloads ?? [])
          .filter((p) => p && !p.isError && !p.isReasoning && typeof p.text === "string")
          .map((p) => p.text)
          .join("\n")
          .trim();
        // Turn ran to completion: return its text (or null for an empty turn) —
        // an empty result is not a provider failure, so do not try the next model.
        return out || null;
      } catch (e) {
        logger?.warn?.(`${PLUGIN_ID}: runEmbeddedAgent ${modelKey} failed: ${e?.message ?? e} -> trying next model`);
      }
    }
    logger?.warn?.(`${PLUGIN_ID}: runEmbeddedAgent exhausted model chain (will fall back to CLI)`);
    return null;
  } catch (e) {
    logger?.warn?.(`${PLUGIN_ID}: runEmbeddedAgent failed (will fall back to CLI): ${e?.message ?? e}`);
    return null;
  }
}

// ---------------------------------------------------------------------------
// Dedupe store (small on-disk Set of seen message ids)
// ---------------------------------------------------------------------------
// File shape:
//   { "ids": ["wamid.HBgL...", "wamid.HBgL...", ...] }
//
// Writes are atomic-rename. On each write we trim to the most recent
// DEDUPE_TRIM_TO ids. This file is small (<200KB even for 5000 ids) and
// read+written in full each time, which is fine for our traffic.
// ---------------------------------------------------------------------------

async function loadDedupe(path) {
  const obj = await tryReadJson(path);
  if (obj && Array.isArray(obj.ids)) {
    return { ids: obj.ids.filter((s) => typeof s === "string").slice(-DEDUPE_MAX_IDS) };
  }
  return { ids: [] };
}

async function saveDedupe(path, state, logger) {
  try {
    await mkdir(dirname(path), { recursive: true });
    const trimmed = state.ids.length > DEDUPE_MAX_IDS
      ? state.ids.slice(-DEDUPE_TRIM_TO)
      : state.ids;
    const tmp = `${path}.tmp.${process.pid}.${randomBytes(4).toString("hex")}`;
    await writeFile(tmp, JSON.stringify({ ids: trimmed }, null, 2) + "\n", {
      encoding: "utf8",
      mode: 0o600,
    });
    await rename(tmp, path);
  } catch (err) {
    logger?.warn?.(`${PLUGIN_ID}: dedupe write failed (${path}): ${err?.message ?? err}`);
  }
}

function dedupeHas(state, id) {
  return state.ids.includes(id);
}

function dedupeAdd(state, id) {
  if (!state.ids.includes(id)) state.ids.push(id);
}

// ---------------------------------------------------------------------------
// Session epochs (/new support)
// ---------------------------------------------------------------------------
// Both agent paths treat inbound text as a plain prompt, so OpenClaw's
// built-in /new command never fires on this channel. Instead, /new bumps a
// per-sender epoch counter persisted here. The epoch is woven into the CLI
// session key AND the embedded session id/file, so bumping it starts a
// brand-new conversation on every path without touching OpenClaw internals.
// File shape: { "<digits>": <epoch int>, ... }
// ---------------------------------------------------------------------------

const EPOCH_FILE = join(
  homedir(), ".openclaw", "plugin-state", "ollie-whatsapp-cloud", "epochs.json",
);

async function loadEpoch(digits) {
  const obj = await tryReadJson(EPOCH_FILE);
  const n = obj && typeof obj === "object" ? Number(obj[digits]) : 0;
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
}

async function bumpEpoch(digits, logger) {
  const obj = (await tryReadJson(EPOCH_FILE)) ?? {};
  const next = (await loadEpoch(digits)) + 1;
  obj[digits] = next;
  try {
    await mkdir(dirname(EPOCH_FILE), { recursive: true });
    const tmp = `${EPOCH_FILE}.tmp.${process.pid}.${randomBytes(4).toString("hex")}`;
    await writeFile(tmp, JSON.stringify(obj, null, 2) + "\n", { encoding: "utf8", mode: 0o600 });
    await rename(tmp, EPOCH_FILE);
  } catch (err) {
    logger?.warn?.(`${PLUGIN_ID}: epoch write failed: ${err?.message ?? err}`);
  }
  return next;
}

// ---------------------------------------------------------------------------
// Meta webhook payload parsing
// ---------------------------------------------------------------------------
// Shape (relevant fragment):
//   {
//     "object": "whatsapp_business_account",
//     "entry": [
//       {
//         "id": "<waba-id>",
//         "changes": [
//           {
//             "field": "messages",
//             "value": {
//               "messaging_product": "whatsapp",
//               "metadata": { "phone_number_id": "...", "display_phone_number": "..." },
//               "contacts":   [ { "profile": { "name": "..." }, "wa_id": "..." } ],
//               "messages":   [ {
//                  "from":       "<OWNER_PHONE>",
//                  "id":         "wamid.HBgL...",
//                  "timestamp":  "1717851234",
//                  "type":       "text",
//                  "text":       { "body": "hello" },
//                  "context":    { "from": "...", "id": "wamid..." } // for replies
//               } ],
//               "statuses":   [ ... ]   // delivery acks; we ignore
//             }
//           }
//         ]
//       }
//     ]
//   }
// ---------------------------------------------------------------------------

/** @typedef {{ from: string, id: string, type: string, text: string|null, replyToId: string|null, raw: object }} InboundMessage */
/** @typedef {{ wabaId: string|null, phoneNumberId: string|null, messages: InboundMessage[] }} ParsedEvent */

/**
 * Walk the Meta payload and pull out a flat list of well-formed text
 * messages. Returns { wabaId, phoneNumberId, messages: [] }. Non-text types
 * and delivery `statuses` are filtered out.
 */
function parseMetaPayload(body) {
  /** @type {ParsedEvent} */
  const out = { wabaId: null, phoneNumberId: null, messages: [] };
  if (!body || typeof body !== "object") return out;
  const entries = Array.isArray(body.entry) ? body.entry : [];
  for (const entry of entries) {
    if (out.wabaId == null && typeof entry?.id === "string") out.wabaId = entry.id;
    const changes = Array.isArray(entry?.changes) ? entry.changes : [];
    for (const change of changes) {
      const value = change && change.value && typeof change.value === "object" ? change.value : null;
      if (!value) continue;
      if (out.phoneNumberId == null && value.metadata && typeof value.metadata.phone_number_id === "string") {
        out.phoneNumberId = value.metadata.phone_number_id;
      }
      const msgs = Array.isArray(value.messages) ? value.messages : [];
      for (const m of msgs) {
        if (!m || typeof m !== "object") continue;
        const from = typeof m.from === "string" ? m.from : "";
        const id = typeof m.id === "string" ? m.id : "";
        if (!from || !id) continue;
        const replyToId = m.context && typeof m.context.id === "string" ? m.context.id : null;
        if (m.type === "text") {
          const text = m.text && typeof m.text.body === "string" ? m.text.body : "";
          out.messages.push({ from, id, type: "text", text, mediaId: null, replyToId, raw: m });
        } else if (m.type === "audio" || m.type === "voice") {
          // voice note / audio -> capture media id for transcription
          const media = (m.type === "audio" ? m.audio : m.voice) || {};
          const mediaId = typeof media.id === "string" ? media.id : "";
          if (!mediaId) continue;
          out.messages.push({ from, id, type: "audio", text: null, mediaId, replyToId, raw: m });
        } else if (m.type === "image") {
          // image -> capture media id for vision description; caption rides along
          const media = m.image || {};
          const mediaId = typeof media.id === "string" ? media.id : "";
          if (!mediaId) continue;
          const caption = typeof media.caption === "string" ? media.caption : "";
          out.messages.push({ from, id, type: "image", text: caption, mediaId, replyToId, raw: m });
        } else if (m.type === "video" || m.type === "document" || m.type === "sticker") {
          // not yet supported -> surfaced so we can reply honestly instead of silence
          out.messages.push({ from, id, type: "unsupported", text: m.type, mediaId: null, replyToId, raw: m });
        }
      }
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// HMAC validation (X-Hub-Signature-256)
// ---------------------------------------------------------------------------

function verifySignature(rawBody, headerValue, appSecret) {
  if (typeof headerValue !== "string" || !headerValue.startsWith(SIG_PREFIX)) return false;
  const provided = headerValue.slice(SIG_PREFIX.length);
  if (provided.length === 0) return false;
  const computed = createHmac("sha256", appSecret).update(rawBody).digest("hex");
  // timingSafeEqual requires equal-length buffers; both are hex strings
  // of the same length (sha256 hex is always 64 chars). Still, defend.
  if (provided.length !== computed.length) return false;
  try {
    return timingSafeEqual(Buffer.from(provided, "utf8"), Buffer.from(computed, "utf8"));
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Outbound (Graph API)
// ---------------------------------------------------------------------------
// Per task brief: POST https://graph.facebook.com/v21.0/<PHONE_NUMBER_ID>/messages
//   Authorization: Bearer <accessToken>
//   { "messaging_product": "whatsapp",
//     "to":                 "<e164>",
//     "type":               "text",
//     "text":               { "body": "<reply>" },
//     "context":            { "message_id": "<inbound id>" }   // optional
//   }
//
// This function is wired but not yet called from a "reply" path because
// building a real outbound adapter (ChannelOutboundAdapter.sendText) is
// out of scope for this build. A follow-up build will call this from
// either a custom channel adapter (preferred) or a webhook-taskflow that
// watches a side file (fallback).
// ---------------------------------------------------------------------------

/**
 * Send a text reply to an E.164 phone via the Cloud API.
 * Returns the parsed JSON response body on success, throws on error.
 *
 * @param {{ phoneNumberId: string, accessToken: string }} secret
 * @param {{ to: string, body: string, replyToMessageId?: string|null }} args
 */
async function sendWhatsAppText(secret, args) {
  const url = `${META_GRAPH_BASE}/${GRAPH_API_VERSION}/${encodeURIComponent(secret.phoneNumberId)}/messages`;
  /** @type {Record<string, unknown>} */
  const payload = {
    messaging_product: "whatsapp",
    to: args.to,
    type: "text",
    text: { body: args.body },
  };
  if (args.replyToMessageId) {
    payload.context = { message_id: args.replyToMessageId };
  }
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${secret.accessToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Graph API ${res.status}: ${text.slice(0, 500)}`);
  }
  return res.json().catch(() => ({}));
}

// ---------------------------------------------------------------------------
// Voice replies (on-device TTS)
// ---------------------------------------------------------------------------
// Rule: voice note in -> voice note out. The reply text is synthesized
// on-box by ollie-tts (Kokoro-82M int8 ONNX, voice am_michael, speed 1.1,
// pronunciation lexicon) and sent as an OGG/Opus audio message. Best-effort:
// any failure falls back to a plain text reply.
// ---------------------------------------------------------------------------

const TTS_HOME = "/home/openclaw/tts";
const TTS_PY = `${TTS_HOME}/venv/bin/python`;
const TTS_SCRIPT = `${TTS_HOME}/tts_say.py`;
const TTS_TIMEOUT_MS = 120000;
const VOICE_MAX_CHARS = 900; // longer replies are essays -> text

/** Synthesize text to an OGG/Opus file. Resolves the path, or null. */
function synthesizeVoice(text, logger) {
  return new Promise((resolve) => {
    if (!existsSync(TTS_SCRIPT)) { resolve(null); return; }
    const out = join(tmpdir(), `ollie-voice-${Date.now()}-${randomBytes(3).toString("hex")}.ogg`);
    let child;
    try {
      child = spawn(TTS_PY, [TTS_SCRIPT, "--out", out],
        { stdio: ["pipe", "ignore", "pipe"], env: { ...process.env, OLLIE_TTS_HOME: TTS_HOME } });
    } catch (e) {
      logger?.warn?.(`${PLUGIN_ID}: tts spawn failed: ${e?.message ?? e}`);
      resolve(null);
      return;
    }
    let err = "";
    child.stderr.on("data", (d) => { err += d.toString(); });
    const timer = setTimeout(() => {
      try { child.kill("SIGKILL"); } catch { /* ignore */ }
      logger?.warn?.(`${PLUGIN_ID}: tts timed out (${TTS_TIMEOUT_MS}ms)`);
      resolve(null);
    }, TTS_TIMEOUT_MS);
    child.on("error", () => { clearTimeout(timer); resolve(null); });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code === 0 && existsSync(out)) { resolve(out); return; }
      logger?.warn?.(`${PLUGIN_ID}: tts exit=${code} ${err.slice(0, 200)}`);
      resolve(null);
    });
    child.stdin.end(text);
  });
}

/** Upload an OGG/Opus file and send it as an audio (voice) message. */
async function sendWhatsAppAudio(secret, { to, filePath }) {
  const buf = await readFile(filePath);
  const form = new FormData();
  form.append("messaging_product", "whatsapp");
  form.append("type", "audio/ogg");
  form.append("file", new Blob([buf], { type: "audio/ogg" }), "voice.ogg");
  const upRes = await fetch(
    `${META_GRAPH_BASE}/${GRAPH_API_VERSION}/${encodeURIComponent(secret.phoneNumberId)}/media`,
    { method: "POST", headers: { Authorization: `Bearer ${secret.accessToken}` }, body: form },
  );
  if (!upRes.ok) throw new Error(`media upload ${upRes.status}: ${(await upRes.text().catch(() => "")).slice(0, 300)}`);
  const { id: mediaId } = await upRes.json();
  if (!mediaId) throw new Error("media upload returned no id");
  const res = await fetch(
    `${META_GRAPH_BASE}/${GRAPH_API_VERSION}/${encodeURIComponent(secret.phoneNumberId)}/messages`,
    {
      method: "POST",
      headers: { Authorization: `Bearer ${secret.accessToken}`, "Content-Type": "application/json" },
      body: JSON.stringify({ messaging_product: "whatsapp", to, type: "audio", audio: { id: mediaId } }),
    },
  );
  if (!res.ok) throw new Error(`audio send ${res.status}: ${(await res.text().catch(() => "")).slice(0, 300)}`);
  return res.json().catch(() => ({}));
}

/** Voice-reply pipeline: synthesize + upload + send. True on success. */
async function trySendVoiceReply(secret, to, text, logger) {
  const ogg = await synthesizeVoice(text, logger);
  if (!ogg) return false;
  try {
    await sendWhatsAppAudio(secret, { to, filePath: ogg });
    logger?.info?.(`${PLUGIN_ID}: voice reply sent to ${to} (${text.length} chars)`);
    return true;
  } catch (e) {
    logger?.warn?.(`${PLUGIN_ID}: voice reply failed (text fallback): ${e?.message ?? e}`);
    return false;
  } finally {
    try { await unlink(ogg); } catch { /* ignore */ }
  }
}

/**
 * Mark an inbound message as read (blue ticks) and optionally show the typing
 * indicator to the sender. Both use the same /messages "status:read" call.
 * The typing indicator auto-dismisses when we send our reply, or after ~25s.
 * Best-effort: failures are logged, never thrown (cosmetic, must not block).
 */
async function markReadAndTyping(secret, messageId, { typing = true } = {}, logger) {
  if (!messageId) return;
  const url = `${META_GRAPH_BASE}/${GRAPH_API_VERSION}/${encodeURIComponent(secret.phoneNumberId)}/messages`;
  const post = async (payload) => {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Authorization": `Bearer ${secret.accessToken}`, "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return res.ok ? null : `${res.status}: ${(await res.text().catch(() => "")).slice(0, 160)}`;
  };
  const base = { messaging_product: "whatsapp", status: "read", message_id: messageId };
  try {
    if (typing) {
      const err = await post({ ...base, typing_indicator: { type: "text" } });
      if (!err) return;
      // Combined call failed — fall back to a plain read receipt so blue ticks
      // are guaranteed even if the typing-indicator field is ever rejected.
      logger?.warn?.(`${PLUGIN_ID}: typing call failed (${err}) -> read-only fallback`);
    }
    const err2 = await post(base);
    if (err2) logger?.warn?.(`${PLUGIN_ID}: mark-read ${err2}`);
  } catch (e) {
    logger?.warn?.(`${PLUGIN_ID}: mark-read/typing failed: ${e?.message ?? e}`);
  }
}

// ---------------------------------------------------------------------------
// Inert-mode placeholders for agent-injection
// ---------------------------------------------------------------------------
// In a follow-up build, the parsed inbound event will be handed to
// api.runtime.channel.inbound.run (runChannelInboundEvent) with a complete
// ChannelTurnAdapter. That requires implementing the 5-stage adapter
// (ingest/classify/preflight/resolveTurn/onFinalize) and building a
// ChannelEventDeliveryAdapter that calls sendWhatsAppText(...) above. The
// session key for inbound from `+<OWNER_PHONE>` against the agent
// "ollie" would look like `agent:ollie:whatsapp-cloud:dm:+<OWNER_PHONE>`
// (mirroring the Baileys `dmScope: per-channel-peer` setting already in
// openclaw.json).
//
// Until that is wired in, this plugin is "log and dedupe" only -- a real
// agent run is NOT triggered by inbound. The user must complete the
// follow-up before this channel is operational for live conversations.
// ---------------------------------------------------------------------------

function buildSessionKey(agentId, e164) {
  // Mirrors the per-channel-peer session layout used by the existing
  // Baileys whatsapp channel. The leading "+" is stripped from the e164
  // so the key is filesystem-friendly.
  const safe = String(e164 || "").replace(/^\+/, "");
  return `agent:${agentId || "main"}:${CHANNEL_ID}:dm:+${safe}`;
}

function buildAgentId(api) {
  // Default agent id is "main" per OpenClaw convention. We don't try to
  // resolve the routed agent from cfg here -- the channel machinery does
  // that downstream. The placeholder is for diagnostics.
  return "main";
}

// ---------------------------------------------------------------------------
// HTTP route handler
// ---------------------------------------------------------------------------
// The route is registered via api.registerHttpRoute (signature verified
// in webhooks/index.js:581 and dist/plugin-sdk/types-B4TJD_iZ.d.ts:7004).
// We accept BOTH the (req, res) Node IncomingMessage/ServerResponse shape
// (per the type) and any other req that exposes `req.method` and a
// `req.on("data")` stream.
// ---------------------------------------------------------------------------

function makeRouteHandler(cfg, logger, ctx) {
  return async function handle(req, res) {
    const method = String(req?.method || "GET").toUpperCase();
    if (method === "GET") return handleVerify(req, res, cfg, logger);
    if (method === "POST") return handleInbound(req, res, cfg, logger, ctx);
    // Meta never sends anything else. Reject quickly.
    sendJson(res, 405, { error: "method_not_allowed" });
    return true;
  };
}

async function handleVerify(req, res, cfg, logger) {
  // Parse the URL -- req.url looks like "/plugins/whatsapp-cloud/webhook?hub.mode=..."
  let url = "";
  try { url = new URL(req.url || "/", "http://localhost").toString(); } catch { /* ignore */ }
  let parsed;
  try { parsed = new URL(req.url || "/", "http://localhost"); } catch {
    sendJson(res, 400, { error: "bad_url" });
    return true;
  }
  const mode = parsed.searchParams.get("hub.mode");
  const token = parsed.searchParams.get("hub.verify_token");
  const challenge = parsed.searchParams.get("hub.challenge");

  // Read secret. If missing, return 200 with a static challenge so Meta
  // can be re-aimed later. NEVER echo back the secret.
  const secret = validateSecret(await tryReadJson(cfg.secretFile));
  if (!secret) {
    logger?.info?.(
      `${PLUGIN_ID}: GET handshake: no secret file at ${cfg.secretFile} -> returning 200 with empty body (inert).`,
    );
    res.statusCode = 200;
    res.setHeader("content-type", "text/plain; charset=utf-8");
    res.end("");
    return true;
  }

  if (mode !== "subscribe" || !token || !challenge) {
    logger?.warn?.(`${PLUGIN_ID}: GET handshake: missing params (mode=${mode}, token=${!!token}, challenge=${!!challenge}) -> 403`);
    sendJson(res, 403, { error: "missing_params" });
    return true;
  }

  if (token !== secret.verifyToken) {
    logger?.warn?.(`${PLUGIN_ID}: GET handshake: verify_token mismatch -> 403`);
    sendJson(res, 403, { error: "verify_token_mismatch" });
    return true;
  }

  logger?.info?.(`${PLUGIN_ID}: GET handshake OK -> 200 with challenge`);
  res.statusCode = 200;
  res.setHeader("content-type", "text/plain; charset=utf-8");
  res.end(String(challenge));
  return true;
}

async function handleInbound(req, res, cfg, logger, ctx) {
  // Read the body. We need the raw bytes (string) for HMAC, so we
  // collect and decode as utf8 ourselves rather than relying on
  // req.body (which the gateway may not have pre-parsed for plugin
  // routes).
  const rawBody = await readRawBody(req, MAX_BODY_BYTES).catch((err) => {
    logger?.warn?.(`${PLUGIN_ID}: POST body read failed: ${err?.message ?? err}`);
    return null;
  });
  if (rawBody == null) {
    sendJson(res, 400, { error: "body_read_failed" });
    return true;
  }

  // Secret. If missing or inert, ack quickly with a no-op.
  const secret = validateSecret(await tryReadJson(cfg.secretFile));
  if (!secret) {
    if (cfg.logEveryInbound) {
      logger?.info?.(`${PLUGIN_ID}: POST received (inert: no secret) len=${rawBody.length} -> 200 no-op`);
    }
    sendJson(res, 200, { ok: true, inert: true });
    return true;
  }
  if (!cfg.enabled) {
    if (cfg.logEveryInbound) {
      logger?.info?.(`${PLUGIN_ID}: POST received (config.enabled=false) len=${rawBody.length} -> 200 no-op`);
    }
    sendJson(res, 200, { ok: true, inert: "disabled" });
    return true;
  }

  // HMAC validation. Header is `X-Hub-Signature-256: sha256=...`.
  const sigHeader = pickHeader(req, "x-hub-signature-256");
  if (!verifySignature(Buffer.from(rawBody, "utf8"), sigHeader, secret.appSecret)) {
    logger?.warn?.(`${PLUGIN_ID}: POST signature mismatch -> 403 (header present: ${Boolean(sigHeader)})`);
    sendJson(res, 403, { error: "signature_mismatch" });
    return true;
  }

  // Parse JSON. If it's not JSON we still ack 200 to avoid Meta retries
  // on bad payloads; we just log and exit.
  let payload;
  try {
    payload = JSON.parse(rawBody);
  } catch (err) {
    logger?.warn?.(`${PLUGIN_ID}: POST body is not JSON: ${err?.message ?? err} -> 200 no-op`);
    sendJson(res, 200, { ok: true, ignored: "not_json" });
    return true;
  }

  // Filter for text messages.
  const parsed = parseMetaPayload(payload);
  if (parsed.messages.length === 0) {
    if (cfg.logEveryInbound) {
      logger?.info?.(
        `${PLUGIN_ID}: POST ${payload?.object || "?"} -> no text messages (statuses-only or other) -> 200`,
      );
    }
    sendJson(res, 200, { ok: true, accepted: 0 });
    return true;
  }

  // Dedupe.
  const state = await loadDedupe(cfg.dedupeFile);
  const fresh = [];
  for (const m of parsed.messages) {
    if (dedupeHas(state, m.id)) {
      logger?.info?.(`${PLUGIN_ID}: dedupe hit, skipping id=${m.id}`);
      continue;
    }
    dedupeAdd(state, m.id);
    fresh.push(m);
  }
  await saveDedupe(cfg.dedupeFile, state, logger);

  if (fresh.length === 0) {
    sendJson(res, 200, { ok: true, accepted: 0, deduped: parsed.messages.length });
    return true;
  }

  // Ack Meta IMMEDIATELY (agent turns are slow; Meta retries on non-2xx).
  sendJson(res, 200, { ok: true, accepted: fresh.length });

  // Hand each message to the per-sender queue (debounce + serialization);
  // turns run in the background, best-effort.
  setImmediate(() => {
    for (const m of fresh) {
      try {
        enqueueInbound({ message: m, phoneNumberId: parsed.phoneNumberId, wabaId: parsed.wabaId, secret, cfg, logger, ctx });
      } catch (err) {
        logger?.warn?.(`${PLUGIN_ID}: enqueueInbound failed for id=${m.id}: ${err?.message ?? err}`);
      }
    }
  });
  return true;
}

/**
 * Placeholder for the actual agent-injection. In a follow-up build this
 * will hand the parsed inbound event to api.runtime.channel.inbound.run
 * with a complete ChannelTurnAdapter, OR register a custom channel
 * adapter (preferred) and let the existing ollie-wa-approval before_agent_run
 * gate fire. For now it logs the parsed event so the operator can see the
 * route is alive and traffic is being parsed.
 */
// Soft per-turn budget: turns that run longer than this are converted into
// background jobs (deterministic enforcement of "never grind in chat turns").
const SOFT_TURN_MS = 75000;

/** Enqueue a background job via job-submit.sh. Resolves true on success. */
function submitBackgroundJob(taskText, digits, logger) {
  return new Promise((resolve) => {
    try {
      const child = spawn("/home/openclaw/bin/job-submit.sh",
        ["--channel", "whatsapp", "--to", digits, "--task", taskText],
        { stdio: ["ignore", "pipe", "pipe"] });
      let out = "";
      child.stdout.on("data", (d) => { out += d.toString(); });
      const timer = setTimeout(() => {
        try { child.kill("SIGKILL"); } catch { /* ignore */ }
        resolve(false);
      }, 15000);
      child.on("error", () => { clearTimeout(timer); resolve(false); });
      child.on("close", (code) => {
        clearTimeout(timer);
        logger?.info?.(`${PLUGIN_ID}: job-submit ${code === 0 ? "ok" : `failed (code ${code})`}: ${out.trim()}`);
        resolve(code === 0);
      });
    } catch {
      resolve(false);
    }
  });
}

/**
 * Show "typing…" to the sender immediately AND keep it alive for the whole
 * turn. Meta dismisses the indicator after 25s (or when we reply), so we
 * refresh it every TYPING_REFRESH_MS until stop() is called. The first send is
 * awaitable via `.ready` so callers can guarantee the animation is up before
 * any work begins.
 */
const TYPING_REFRESH_MS = 20000;
function startTypingKeeper(secret, messageId, logger) {
  let stopped = false;
  const ping = () => stopped ? Promise.resolve() : markReadAndTyping(secret, messageId, { typing: true }, logger);
  const ready = ping();
  const timer = setInterval(() => { ping(); }, TYPING_REFRESH_MS);
  if (typeof timer.unref === "function") timer.unref();
  return {
    ready,
    stop() { if (!stopped) { stopped = true; clearInterval(timer); } },
  };
}

// ---------------------------------------------------------------------------
// Per-sender turn queue (debounce + serialization)
// ---------------------------------------------------------------------------
// WhatsApp users often send a link, then "fact check this" a few seconds
// later. Without batching, each message starts its own agent turn — possibly
// CONCURRENTLY for the same sender, since each webhook POST is processed
// independently. That caused duplicate "On it" job acks, background jobs
// submitted without their context message (a job whose whole task was
// "Fact check this"), and racing writes to the same session. This queue:
//   1. debounces — waits TURN_DEBOUNCE_MS after the LAST message before
//      starting a turn, merging rapid-fire messages into ONE turn;
//   2. serializes — never two turns for the same sender at once; messages
//      arriving mid-turn are batched into the next turn.
// ---------------------------------------------------------------------------

const TURN_DEBOUNCE_MS = 4000;

/** @type {Map<string, { pending: object[], timer: ReturnType<typeof setTimeout>|null, running: boolean, typing: { ready: Promise<unknown>, stop: () => void }|null }>} */
const senderQueues = new Map();

function enqueueInbound(args) {
  const { message, secret, logger } = args;
  const from = message.from;
  // Allowlist gate — only permitted numbers get answered (closed by default).
  if (!isAllowed(from, secret.allowFrom)) {
    logger?.info?.(`${PLUGIN_ID}: inbound from ${from} not in allowlist -> ignored`);
    return;
  }
  const digits = String(from).replace(/[^\d]/g, "");
  let q = senderQueues.get(digits);
  if (!q) {
    q = { pending: [], timer: null, running: false, typing: null };
    senderQueues.set(digits, q);
  }
  q.pending.push(args);
  // Blue ticks + "typing…" immediately so the debounce never reads as lag.
  if (!q.typing) {
    q.typing = startTypingKeeper(secret, message.id, logger);
  } else {
    // Keeper already live for this batch — just blue-tick the new message.
    markReadAndTyping(secret, message.id, { typing: false }, logger);
  }
  if (q.timer) clearTimeout(q.timer);
  q.timer = setTimeout(() => {
    q.timer = null;
    void drainQueue(digits, logger);
  }, TURN_DEBOUNCE_MS);
}

async function drainQueue(digits, logger) {
  const q = senderQueues.get(digits);
  if (!q || q.running || q.pending.length === 0) return;
  q.running = true;
  const batch = q.pending.splice(0);
  const typing = q.typing;
  q.typing = null;
  try {
    // AWAIT the first typing ping so the animation is visibly up before any
    // work begins (a fast reply must not beat it).
    if (typing) await typing.ready;
    await runBatchTurn(batch, typing, logger);
  } catch (err) {
    logger?.warn?.(`${PLUGIN_ID}: turn failed for ${digits}: ${err?.message ?? err}`);
  } finally {
    typing?.stop();
    q.running = false;
    if (q.pending.length > 0) void drainQueue(digits, logger);
    else if (!q.timer && !q.typing) senderQueues.delete(digits);
  }
}

/**
 * Run ONE agent turn for a batch of messages from the same sender.
 * Voice notes are transcribed and images vision-described per message, then
 * all texts merge into a single prompt — so "link" + "fact check this" become
 * one job, not two context-less ones. The typing keeper is owned by
 * drainQueue; we stop() it right before sending the reply.
 */
async function runBatchTurn(batch, typing, logger) {
  const { secret, ctx } = batch[0];
  const from = batch[0].message.from;
  const digits = String(from).replace(/[^\d]/g, "");

  const parts = [];
  let hasVoice = false; // voice note in -> voice note out
  for (const { message } of batch) {
  let text = (message.text || "").trim();
  if (message.type === "audio") {
    hasVoice = true;
    text = await transcribeAudio(secret, message.mediaId, logger);
    if (!text) {
      try {
        await sendWhatsAppText(secret, { to: from, body: "I couldn't make out that voice note — mind typing it or sending it again?" });
      } catch { /* ignore */ }
      continue;
    }
    logger?.info?.(`${PLUGIN_ID}: transcribed voice from ${from}: ${JSON.stringify(truncateForLog(text))}`);
  } else if (message.type === "image") {
    const desc = await describeImage(secret, message.mediaId, logger);
    if (!desc) {
      try {
        await sendWhatsAppText(secret, { to: from, body: "I couldn't process that image — mind sending it again (or as a smaller photo)?" });
      } catch { /* ignore */ }
      continue;
    }
    const caption = text;
    text = `The user sent an IMAGE. Vision description (with verbatim OCR of all visible text):\n---\n${desc}\n---` +
      (caption ? `\nUser's caption with the image: ${caption}` : "\nNo caption was attached.") +
      "\nRespond to the user about this image (if it looks like a claim/news/screenshot, consider fact-checking it).";
    logger?.info?.(`${PLUGIN_ID}: described image from ${from} (${desc.length} chars)`);
  } else if (message.type === "unsupported") {
    try {
      await sendWhatsAppText(secret, { to: from, body: `I can handle text, voice notes and images for now — ${message.text || "that kind of"} messages aren't supported yet.` });
    } catch { /* ignore */ }
    continue;
  }
  if (!text) continue;

  // Slash commands are handled HERE, not by OpenClaw: both agent paths treat
  // inbound text as a plain prompt, so the built-in /new never fires on this
  // channel. /new (or /reset, /clear) bumps the sender's session epoch, which
  // rotates the session key + embedded session file -> guaranteed fresh chat.
  // Any later messages in the same batch land in the fresh session.
  if (/^\/(new|reset|clear)\s*$/i.test(text)) {
    const epoch = await bumpEpoch(digits, logger);
    logger?.info?.(`${PLUGIN_ID}: session reset from ${from} -> epoch ${epoch}`);
    try {
      await sendWhatsAppText(secret, { to: from, body: "Fresh slate — context wiped. What are we plotting now?" });
    } catch (e) {
      logger?.warn?.(`${PLUGIN_ID}: reset ack send failed: ${e?.message ?? e}`);
    }
    continue;
  }
  parts.push(text);
  }

  const text = parts.join("\n\n");
  if (!text.trim()) return;

  const epoch = await loadEpoch(digits);
  const sessionKey = `${CHANNEL_ID}:dm:${digits}` + (epoch > 0 ? `:e${epoch}` : "");
  const lastId = batch[batch.length - 1].message.id;
  logger?.info?.(`${PLUGIN_ID}: INBOUND from=${from} msgs=${batch.length} lastId=${lastId} session=${sessionKey} body=${JSON.stringify(truncateForLog(text))}`);

  // Sender envelope: lets the agent know who is talking (needed e.g. for
  // background-job delivery targets). AGENTS.md instructs the agent to treat
  // this as metadata and never echo it back.
  const turnText = `[whatsapp from:+${digits}] ${text}`;

  // NO ack — just reply. FAST path = in-process warm runtime; CLI spawn is the
  // fallback. Turns exceeding SOFT_TURN_MS are converted into background jobs:
  // the user gets a quick ack and the result is delivered by the jobs runner.
  const t0 = Date.now();
  const TIMEOUT = Symbol("soft-timeout");
  const race = (p) => {
    let timer;
    return Promise.race([
      p,
      new Promise((res) => { timer = setTimeout(() => res(TIMEOUT), SOFT_TURN_MS); }),
    ]).finally(() => clearTimeout(timer));
  };
  const jobify = async () => {
    const ok = await submitBackgroundJob(text, digits, logger);
    if (!ok) return false;
    try {
      await sendWhatsAppText(secret, { to: from, body: "On it — this needs some digging. I'll message you here when it's done." });
    } catch (e) {
      logger?.warn?.(`${PLUGIN_ID}: job ack send failed: ${e?.message ?? e}`);
    }
    logger?.info?.(`${PLUGIN_ID}: slow turn converted to background job after ${Date.now() - t0}ms`);
    return true;
  };

  // Permission tier: the first allowFrom entry (or explicit ownerFrom) is the
  // OWNER and gets the full "main" agent. Everyone else is a GUEST and is
  // routed CLI-only to the restricted "guest" agent (tool allow-list enforced
  // by the gateway config — no exec/write/desktop). Guests never touch the
  // embedded path because it cannot guarantee agent selection.
  const ownerDigits = String(secret.ownerFrom ?? secret.allowFrom?.[0] ?? "").replace(/[^\d]/g, "");
  const isOwner = digits === ownerDigits;

  let via = "embedded";
  let reply = null;
  if (isOwner) {
    reply = await race(runAgentEmbedded(ctx, turnText, digits, logger, epoch));
    if (reply === TIMEOUT) {
      if (await jobify()) return;
      reply = null;
    } else if (reply != null && !stripToolCallLeak(reply)) {
      // Embedded emitted ONLY a tool call as text (empty after stripping) — it
      // couldn't execute the tool. Fall through to the CLI path, which runs
      // tools reliably (e.g. reminders, fact-check, web search).
      logger?.info?.(`${PLUGIN_ID}: embedded leaked tool-call -> CLI fallback for tool execution`);
      reply = null;
    }
  }
  if (reply == null) {
    via = isOwner ? "cli" : "cli-guest";
    reply = await race(runAgentTurn(turnText, sessionKey, logger, isOwner ? AGENT_ID : "guest"));
    if (reply === TIMEOUT) {
      if (await jobify()) return;
      reply = null;
    }
  }
  logger?.info?.(`${PLUGIN_ID}: turn done via=${via} in ${Date.now() - t0}ms`);
  const cleaned = stripToolCallLeak(reply);
  if (reply && cleaned !== reply.trim()) {
    logger?.warn?.(`${PLUGIN_ID}: stripped leaked tool-call block from reply`);
  }
  const body = cleaned && cleaned.trim() ? cleaned.trim() : "Sorry — I couldn't produce a reply just now. Please try again.";
  // Stop refreshing before we send so typing never re-appears after the reply.
  typing?.stop();
  // Voice note in -> voice note out (short replies only; text on any failure).
  if (hasVoice && body.length <= VOICE_MAX_CHARS) {
    if (await trySendVoiceReply(secret, from, body, logger)) return;
  }
  try {
    await sendWhatsAppText(secret, { to: from, body });
    logger?.info?.(`${PLUGIN_ID}: replied to ${from} (${body.length} chars)`);
  } catch (e) {
    logger?.warn?.(`${PLUGIN_ID}: reply send failed for ${from}: ${e?.message ?? e}`);
  }
}

// ---------------------------------------------------------------------------
// Tiny HTTP helpers
// ---------------------------------------------------------------------------

function sendJson(res, status, obj) {
  try {
    res.statusCode = status;
    res.setHeader("content-type", "application/json; charset=utf-8");
    res.end(JSON.stringify(obj));
  } catch {
    // ignore -- connection may already be closed
  }
}

/** Read all bytes from req as utf8, capping at maxBytes. */
function readRawBody(req, maxBytes) {
  return new Promise((resolve, reject) => {
    if (!req || typeof req.on !== "function") {
      reject(new Error("not_a_stream"));
      return;
    }
    const chunks = [];
    let total = 0;
    let aborted = false;
    req.on("data", (chunk) => {
      if (aborted) return;
      const buf = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk || "");
      total += buf.length;
      if (total > maxBytes) {
        aborted = true;
        reject(new Error("body_too_large"));
        try { req.destroy?.(); } catch { /* ignore */ }
        return;
      }
      chunks.push(buf);
    });
    req.on("end", () => {
      if (aborted) return;
      resolve(Buffer.concat(chunks).toString("utf8"));
    });
    req.on("error", (err) => {
      if (aborted) return;
      reject(err);
    });
  });
}

/** Pull a header value regardless of case (Node lowercases all headers). */
function pickHeader(req, name) {
  if (!req || !req.headers) return null;
  const target = String(name).toLowerCase();
  for (const k of Object.keys(req.headers)) {
    if (k.toLowerCase() === target) {
      const v = req.headers[k];
      return Array.isArray(v) ? v[0] : v;
    }
  }
  return null;
}

function truncateForLog(s, max = 200) {
  if (typeof s !== "string") return "";
  return s.length <= max ? s : s.slice(0, max - 1) + "\u2026";
}

// ---------------------------------------------------------------------------
// Plugin entry
// ---------------------------------------------------------------------------

const ollie_whatsapp_cloud_default = definePluginEntry({
  id: PLUGIN_ID,
  name: "Ollie WhatsApp Cloud API Channel",
  description:
    "Meta WhatsApp Cloud API (Graph v21.0) channel for Ollie. Inbound webhook + Graph API send. INERT by default.",
  register(api) {
    const cfg = resolveConfig(api.config?.plugins?.entries?.[PLUGIN_ID]?.config);
    const logger = api.logger;
    // Warm runtime + config for the in-process fast agent path.
    const ctx = { runtime: api.runtime, config: api.config };
    logger?.info?.(
      `${PLUGIN_ID}: loaded (enabled=${cfg.enabled}, secretFile=${cfg.secretFile}, dedupeFile=${cfg.dedupeFile}, route=${ROUTE_PATH})`,
    );

    // Pre-create the dedupe dir so the first POST doesn't need a mkdir
    // under load. Best-effort; never throw.
    try {
      const dir = dirname(cfg.dedupeFile);
      if (!existsSync(dir)) {
        // existsSync is sync; that's fine on the boot path.
        // (We don't await mkdir here -- the save path will do it.)
      }
    } catch { /* ignore */ }

    // If completely inert (no secret, config off) still mount the route
    // so the gateway is consistent and the operator can verify the
    // route in the gateway's route table.
    api.registerHttpRoute({
      // auth "plugin" = the gateway passes the request through and the PLUGIN
      // enforces auth itself (we do: verify-token handshake on GET +
      // X-Hub-Signature-256 HMAC on POST). NOTE: only "plugin" | "gateway" are
      // valid; "none" is rejected by the loader and the route never registers.
      path: ROUTE_PATH,
      auth: "plugin",
      match: "exact",
      replaceExisting: true,
      handler: makeRouteHandler(cfg, logger, ctx),
    });
    logger?.info?.(`${PLUGIN_ID}: registered HTTP route ${ROUTE_PATH} (inert=${!cfg.enabled})`);

    // Touch secret on disk so we can warn (once) if it's missing.
    if (cfg.enabled) {
      // Best-effort: do NOT block the boot path on disk I/O. Schedule on
      // next tick.
      setImmediate(async () => {
        try {
          const secret = validateSecret(await tryReadJson(cfg.secretFile));
          if (!secret) {
            logger?.warn?.(
              `${PLUGIN_ID}: enabled=true but secret file missing or malformed at ${cfg.secretFile}. Inert until fixed.`,
            );
          } else {
            logger?.info?.(
              `${PLUGIN_ID}: secret loaded phoneNumberId=${secret.phoneNumberId} wabaId=${secret.wabaId || "?"} verifyToken.len=${secret.verifyToken.length}`,
            );
          }
        } catch (err) {
          logger?.warn?.(`${PLUGIN_ID}: secret probe failed: ${err?.message ?? err}`);
        }
      });
    }
  },
});

export default ollie_whatsapp_cloud_default;
