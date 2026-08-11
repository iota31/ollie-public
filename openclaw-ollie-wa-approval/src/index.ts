// =============================================================================
// @ollie/openclaw-wa-approval  —  TypeScript reference
//
// This file is a STRUCTURAL MIRROR of `index.js` (the loadable entry).
// It exists for documentation and to make the hook contracts searchable
// in editors. OpenClaw resolves the plugin entry from
// DEFAULT_PLUGIN_ENTRY_CANDIDATES (manifest-DaiqPlf0.js:833-838) and will
// pick `index.js` at the package root.
//
// If/when a build step is added, compile this file to `index.js` and the
// runtime behavior will be identical.
// =============================================================================

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { readFileSync, statSync } from "node:fs";

// ---- Hook event/context types (from dist/plugin-sdk/hook-types-B_5108I1.d.ts) ----

interface PluginHookAgentContext {
  runId?: string;
  jobId?: string;
  trace?: unknown;
  agentId?: string;
  sessionKey?: string;
  sessionId?: string;
  workspaceDir?: string;
  modelProviderId?: string;
  modelId?: string;
  /** Provider surface name, e.g. "whatsapp", "telegram". */
  messageProvider?: string;
  trigger?: string;
  channelId?: string;
  contextTokenBudget?: number;
  contextWindowSource?: "model" | "modelsConfig" | "agentContextTokens" | "default";
  contextWindowReferenceTokens?: number;
}

interface PluginHookMessageContext {
  channelId: string;
  accountId?: string;
  conversationId?: string;
  sessionKey?: string;
  runId?: string;
  messageId?: string;
  senderId?: string;
  trace?: unknown;
  traceId?: string;
  spanId?: string;
  parentSpanId?: string;
  callDepth?: number;
}

interface PluginHookBeforeAgentRunEvent {
  prompt: string;
  messages: unknown[];
  systemPrompt?: string;
  accountId?: string;
  channelId?: string;
  senderId?: string;
  senderIsOwner?: boolean;
}

type InputGateDecision =
  | { outcome: "pass" }
  | { outcome: "block"; reason: string; message?: string };

interface PluginHookMessageSendingEvent {
  to: string;
  content: string;
  replyToId?: string | number;
  threadId?: string | number;
  metadata?: Record<string, unknown>;
}

interface PluginHookMessageSendingResult {
  content?: string;
  cancel?: boolean;
  cancelReason?: string;
  metadata?: Record<string, unknown>;
}

interface PluginHookMessageReceivedEvent {
  from: string;
  content: string;
  timestamp?: number;
  threadId?: string | number;
  messageId?: string;
  senderId?: string;
  sessionKey?: string;
  runId?: string;
  trace?: unknown;
  traceId?: string;
  spanId?: string;
  parentSpanId?: string;
  metadata?: Record<string, unknown>;
}

// ---- Plugin config ----

interface OllieWaApprovalConfig {
  enabled: boolean;
  ownerTelegramChatId: string;
  approvalsFile: string;
  requestTimeoutMinutes: number;
  hookTimeoutMs: number;
  handsConsentUrl: string;
  handsApprovalToken: string;
}

// ---- State ----

interface PendingEntry {
  from: string;
  preview: string;
  kind: "inbound" | "outbound";
  requestedAt: string;
  expiresAt: string;
}

interface State {
  owner?: string;
  approved: string[];
  blocked: string[];
  pending: Record<string, PendingEntry>;
  _unreadable?: boolean;
}

// ---- WORK_DIGEST injection (structural mirror of index.js readWorkDigest) ----

const WORK_DIGEST_PATH = "/home/openclaw/.openclaw/workspace/WORK_DIGEST.md";
const WORK_DIGEST_MAX_CHARS = 4000;
let workDigestCache: { mtimeMs: number; text: string } = { mtimeMs: -1, text: "" };

/** Read WORK_DIGEST.md, mtime-cached, truncated; never throws ("" on problems). */
function readWorkDigestMtimeCached(): string {
  try {
    const st = statSync(WORK_DIGEST_PATH);
    if (st.mtimeMs === workDigestCache.mtimeMs) return workDigestCache.text;
    let text = readFileSync(WORK_DIGEST_PATH, "utf8");
    if (text.length > WORK_DIGEST_MAX_CHARS) text = text.slice(0, WORK_DIGEST_MAX_CHARS);
    workDigestCache = { mtimeMs: st.mtimeMs, text };
    return text;
  } catch {
    workDigestCache = { mtimeMs: -1, text: "" };
    return "";
  }
}

// ---- Plugin entry ----

export default definePluginEntry({
  id: "ollie-wa-approval",
  name: "Ollie WhatsApp First-Contact Approval",
  description:
    "HARD pre-LLM gate. Owner approves new WhatsApp contacts on Telegram before Ollie processes inbound or sends outbound.",
  register(api) {
    // Read the plugin config out of api.config.plugins.entries[id].config.
    // (api.config is the live OpenClaw config object — see hooks.md for
    // event.context.pluginConfig semantics, which is the same value.)
    const cfg = resolveConfig(
      (api.config as { plugins?: { entries?: Record<string, { config?: unknown }> } })
        ?.plugins?.entries?.["ollie-wa-approval"]?.config,
    );

    api.logger?.info?.(
      `ollie-wa-approval: loaded (enabled=${cfg.enabled}, ownerTelegramChatId=${cfg.ownerTelegramChatId}, approvalsFile=${cfg.approvalsFile})`,
    );

    // ----- before_prompt_build (WORK_DIGEST ground-truth injection) -----
    // Mirrors index.js: registered UNCONDITIONALLY (independent of cfg.enabled,
    // which only gates the WhatsApp approval hooks). Reads workspace/
    // WORK_DIGEST.md (mtime-cached) and returns { appendSystemContext } so the
    // ground-truth work digest lands in EVERY session's system prompt and
    // survives /reset. Digest content changes need no plugin restart.
    api.on(
      "before_prompt_build",
      async (): Promise<{ appendSystemContext: string } | undefined> => {
        const digest = readWorkDigestMtimeCached(); // see index.js readWorkDigest()
        return digest ? { appendSystemContext: digest } : undefined;
      },
      { priority: 50, timeoutMs: 1000 },
    );

    if (!cfg.enabled) {
      // Inert: register no-op hooks so the manifest stays accurate.
      api.on("before_agent_run", async () => undefined, { priority: 50, timeoutMs: 1000 });
      api.on("message_sending", async () => undefined, { priority: 50, timeoutMs: 1000 });
      api.on("message_received", async () => undefined, { priority: 50, timeoutMs: 1000 });
      return;
    }

    // ----- before_agent_run -----
    api.on(
      "before_agent_run",
      async (
        event: PluginHookBeforeAgentRunEvent,
        ctx: PluginHookAgentContext,
      ): Promise<InputGateDecision | void> => {
        try {
          const provider = ctx?.messageProvider ?? "";
          if (provider !== "whatsapp") return undefined;
          const phone = senderIdToPhone(event?.senderId ?? ctx?.senderId);
          if (!phone) {
            return { outcome: "block", reason: "no-sender", message: "Unable to identify sender." };
          }
          if (phone === "+<OWNER_PHONE>") return undefined; // owner short-circuit
          const result = await evaluateInbound(api, cfg, phone, previewFor(event?.prompt ?? ""));
          if (result.allowed) return undefined;
          if (result.reason === "blocked") {
            return { outcome: "block", reason: "blocked", message: "" };
          }
          return {
            outcome: "block",
            reason: "pending-approval",
            message: `Awaiting owner approval (ref ${result.ref ?? "?"})${result.dedup ? " (already pending)" : ""}.`,
          };
        } catch (err) {
          api.logger?.warn?.(`ollie-wa-approval: before_agent_run hook error: ${(err as Error).message ?? err}`);
          return { outcome: "block", reason: "hook-error" };
        }
      },
      { priority: 50, timeoutMs: cfg.hookTimeoutMs },
    );

    // ----- message_sending -----
    api.on(
      "message_sending",
      async (
        event: PluginHookMessageSendingEvent,
        ctx: PluginHookMessageContext,
      ): Promise<PluginHookMessageSendingResult | void> => {
        try {
          const channel = ctx?.channelId ?? "";
          if (channel !== "whatsapp") return undefined;
          const phone = senderIdToPhone(event?.to);
          if (!phone) return undefined;
          if (phone === "+<OWNER_PHONE>") return undefined;
          const result = await evaluateOutbound(api, cfg, phone, previewFor(event?.content ?? ""));
          if (result.allowed) return undefined;
          return {
            cancel: true,
            cancelReason: result.reason === "blocked" ? "blocked" : `pending-approval:${result.ref ?? "?"}`,
          };
        } catch (err) {
          api.logger?.warn?.(`ollie-wa-approval: message_sending hook error: ${(err as Error).message ?? err}`);
          return { cancel: true, cancelReason: "hook-error" };
        }
      },
      { priority: 50, timeoutMs: cfg.hookTimeoutMs },
    );

    // ----- message_received -----
    api.on(
      "message_received",
      async (
        event: PluginHookMessageReceivedEvent,
        ctx: PluginHookMessageContext,
      ): Promise<void> => {
        try {
          const channel = ctx?.channelId ?? "";
          if (channel !== "telegram") return;
          const senderId = event?.senderId ?? "";
          if (senderId !== cfg.ownerTelegramChatId) return;
          const cmd = parseOwnerCommand(event?.content ?? "");
          if (!cmd) return;
          const result = await applyOwnerReply(api, cfg, cmd.ref, cmd.decision);
          if (result.ok) {
            const verb = result.decision === "approve" ? "\u2705 Approved" : "\u26d4 Denied";
            await sendOwnerTelegram(
              api,
              cfg,
              `${verb} ${result.phone} (ref ${cmd.ref}). ${
                result.decision === "approve"
                  ? "Next message from this number will reach Ollie."
                  : "Future messages from this number will be blocked."
              }`,
            );
          } else {
            await sendOwnerTelegram(
              api,
              cfg,
              `\u2757 No pending approval request with ref "${cmd.ref}". Reply with the ref shown in the original request.`,
            );
          }
        } catch (err) {
          api.logger?.warn?.(`ollie-wa-approval: message_received hook error: ${(err as Error).message ?? err}`);
        }
      },
      { priority: 50, timeoutMs: cfg.hookTimeoutMs },
    );
  },
});

// =============================================================================
// Helpers (same logic as index.js — kept here for reference and editor support)
// =============================================================================

const DEFAULT_CONFIG: OllieWaApprovalConfig = {
  enabled: false,
  ownerTelegramChatId: "<OWNER_TELEGRAM_CHAT_ID>",
  approvalsFile: "~/.openclaw/workspace/whatsapp-contacts.json",
  requestTimeoutMinutes: 60,
  hookTimeoutMs: 5000,
};

function resolveConfig(raw: unknown): OllieWaApprovalConfig {
  const c = raw && typeof raw === "object" ? (raw as Partial<OllieWaApprovalConfig>) : {};
  return {
    enabled: c.enabled === true,
    ownerTelegramChatId:
      typeof c.ownerTelegramChatId === "string" && c.ownerTelegramChatId.trim()
        ? c.ownerTelegramChatId.trim()
        : DEFAULT_CONFIG.ownerTelegramChatId,
    approvalsFile: expandHome(
      typeof c.approvalsFile === "string" && c.approvalsFile.trim()
        ? c.approvalsFile.trim()
        : DEFAULT_CONFIG.approvalsFile,
    ),
    requestTimeoutMinutes:
      typeof c.requestTimeoutMinutes === "number" && Number.isFinite(c.requestTimeoutMinutes)
        ? c.requestTimeoutMinutes
        : DEFAULT_CONFIG.requestTimeoutMinutes,
    hookTimeoutMs:
      typeof c.hookTimeoutMs === "number" && Number.isFinite(c.hookTimeoutMs)
        ? c.hookTimeoutMs
        : DEFAULT_CONFIG.hookTimeoutMs,
  };
}

function expandHome(p: string): string {
  if (p === "~") return process.env.HOME ?? "/";
  if (p.startsWith("~/") || p.startsWith("~\\")) {
    return (process.env.HOME ?? "/") + p.slice(1);
  }
  return p;
}

function normalizePhone(raw: unknown): string {
  if (typeof raw !== "string") return "";
  let s = raw.trim();
  if (!s) return "";
  s = s.replace(/[\s\-()]/g, "");
  if (!s.startsWith("+") && /^\d+$/.test(s)) s = "+" + s;
  return s;
}

function senderIdToPhone(senderId: unknown): string {
  return normalizePhone(senderId);
}

function previewFor(text: unknown, max = 120): string {
  if (typeof text !== "string") return "";
  const flat = text.replace(/\s+/g, " ").trim();
  if (flat.length <= max) return flat;
  return flat.slice(0, max - 1) + "\u2026";
}

interface EvaluateResult {
  allowed: boolean;
  reason: "approved" | "blocked" | "pending";
  ref?: string;
  dedup?: boolean;
}

interface ApplyResult {
  ok: boolean;
  reason?: "no-such-ref" | "write-failed";
  phone?: string;
  decision?: "approve" | "deny";
}

// (The state store + lock + telegram send + evaluateX/applyOwnerReply helpers
//  have identical implementations to index.js. See index.js for the body.)

declare function evaluateInbound(
  api: unknown,
  cfg: OllieWaApprovalConfig,
  phone: string,
  preview: string,
): Promise<EvaluateResult>;
declare function evaluateOutbound(
  api: unknown,
  cfg: OllieWaApprovalConfig,
  phone: string,
  preview: string,
): Promise<EvaluateResult>;
declare function applyOwnerReply(
  api: unknown,
  cfg: OllieWaApprovalConfig,
  ref: string,
  decision: "approve" | "deny",
): Promise<ApplyResult>;
declare function sendOwnerTelegram(api: unknown, cfg: OllieWaApprovalConfig, text: string): Promise<boolean>;

function parseOwnerCommand(text: string): { decision: "approve" | "deny"; ref: string; digest: string } | null {
  const t = (text ?? "").trim();
  const approveRe = /^(?:approve|yes|y|allow|ok)\s+([A-Za-z0-9_-]{3,64})(?:\s+([a-fA-F0-9]{16,64}))?\s*$/i;
  const denyRe = /^(?:deny|no|n|block|reject)\s+([A-Za-z0-9_-]{3,64})(?:\s+([a-fA-F0-9]{16,64}))?\s*$/i;
  let m = t.match(approveRe);
  if (m) return { decision: "approve", ref: m[1], digest: m[2] ?? "" };
  m = t.match(denyRe);
  if (m) return { decision: "deny", ref: m[1], digest: m[2] ?? "" };
  return null;
}
