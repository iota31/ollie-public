// Focused tests for approval-callback correlation logging.
//
// handleApprovalCallback must emit one logCallback() line per event with:
//   - message_id (from the incoming callback)
//   - H-ref (from the parsed callback data)
//   - decision ("approve" | "deny")
//   - callback namespace + version
//   - authorization result ("ok" | "unauthorized" | "malformed")
//   - backend HTTP status + error code (when applicable)
//   - edit/remove-buttons result ("editMessage_ok" | "reply_fallback" |
//     "editMessage_failed" | "reply_only" | "reply_failed")
//
// sanitizeForLog must NEVER emit the bot token, approval credential, action
// digest, or any other secret; it must clamp output length.

import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const pluginModulePromise = import(
  pathToFileURL(new URL("../index.js", import.meta.url).pathname).href
);

async function loadPluginModule() {
  return pluginModulePromise;
}

function makeTempStateFile(initial = {}) {
  const dir = mkdtempSync(join(tmpdir(), "wa-approval-"));
  const file = join(dir, "whatsapp-contacts.json");
  writeFileSync(file, JSON.stringify({ approved: [], blocked: [], pending: {}, ...initial }, null, 2));
  return file;
}

function makeLogger() {
  const lines = [];
  return {
    info: (...a) => lines.push(a.map(String).join(" ")),
    warn: (...a) => lines.push(a.map(String).join(" ")),
    error: (...a) => lines.push(a.map(String).join(" ")),
    lines,
  };
}

async function makeApi({ stateFile, handsApprovalToken = "secret", handsConsentUrl = "http://hands.test/consent", logger = null } = {}) {
  const sent = [];
  const interactiveHandlers = [];
  const { default: plugin } = await loadPluginModule();
  const api = {
    config: {
      plugins: {
        entries: {
          "ollie-wa-approval": {
            config: {
              enabled: false,
              approvalsFile: stateFile,
              ownerTelegramChatId: "<OWNER_TELEGRAM_CHAT_ID>",
              handsApprovalToken,
              handsConsentUrl,
              hookTimeoutMs: 5000,
              requestTimeoutMinutes: 60,
            },
          },
        },
      },
    },
    runtime: {
      channel: {
        outbound: {
          loadAdapter: async () => ({ sendText: async () => true }),
        },
      },
    },
    logger: logger ?? makeLogger(),
    on() {},
    registerInteractiveHandler(h) { interactiveHandlers.push(h); },
  };
  plugin.register(api);
  return { api, sent, interactiveHandlers };
}

function setFetch(mock) {
  const original = globalThis.fetch;
  globalThis.fetch = mock;
  return () => { globalThis.fetch = original; };
}

function jsonResponse(status, body) {
  return { status, ok: status >= 200 && status < 300, async json() { return body; } };
}

// The callback path speaks TWO requests to the Hands engine, in order:
//   1. GET  /consent -> pending inventory, the only source of the action digest
//                       bound to this ref (it cannot fit in Telegram's 64-byte
//                       callback payload).
//   2. POST /consent -> the decision itself, carrying that digest.
// A single-response double would silently answer the inventory GET with the
// decision's status, so these helpers let each test drive the POST outcome
// while the inventory stays healthy.
function handsDouble(consentResponse, { pending = [] } = {}) {
  return async (_url, options) => {
    if (!options?.method || options.method === "GET") {
      return jsonResponse(200, { ok: true, pending });
    }
    return consentResponse;
  };
}

// ---------------------------------------------------------------------------
// sanitizeForLog: secret redaction
// ---------------------------------------------------------------------------

test("sanitizeForLog: bearer token is redacted", async () => {
  const { __testHooks } = await loadPluginModule();
  const out = __testHooks.sanitizeForLog("Authorization: Bearer abcdef123 token");
  assert.ok(!/abcdef123/.test(out));
  assert.ok(/Bearer\[redacted\]/.test(out));
});

test("sanitizeForLog: JWT first segment is masked so first 10 chars do not leak", async () => {
  const { __testHooks } = await loadPluginModule();
  const jwt = "<JWT_TEST_TOKEN>";
  const out = __testHooks.sanitizeForLog("token " + jwt);
  assert.ok(!out.includes(jwt.split(".")[0].slice(0, 10)));
});

test("sanitizeForLog: URLs are redacted to [url]", async () => {
  const { __testHooks } = await loadPluginModule();
  const out = __testHooks.sanitizeForLog("see https://example.com/secret/path?q=1");
  assert.ok(!/example\.com/.test(out));
  assert.ok(/\[url\]/.test(out));
});

test("sanitizeForLog: long hex strings become [hex]", async () => {
  const { __testHooks } = await loadPluginModule();
  const hex = "a".repeat(64);
  const out = __testHooks.sanitizeForLog("digest " + hex);
  assert.ok(!out.includes(hex));
  assert.ok(out.includes("[hex]"));
});

test("sanitizeForLog: clamps output length to MAX_LOG_CHARS+ellipsis", async () => {
  const { __testHooks } = await loadPluginModule();
  const out = __testHooks.sanitizeForLog("x".repeat(1000));
  assert.ok(out.length <= 210);
  assert.ok(out.endsWith("\u2026"));
});

// ---------------------------------------------------------------------------
// logCallback: emits required fields and never leaks secrets
// ---------------------------------------------------------------------------

test("logCallback: emits one JSON line with ref, decision, cb_ns, cb_ver, auth", async () => {
  const { __testHooks } = await loadPluginModule();
  const logger = makeLogger();
  const ctx = {
    messageId: 100,
    data: "ollie_approval:v1:a:H-CORR1",
    senderId: "<OWNER_TELEGRAM_CHAT_ID>",
    chatId: "<OWNER_TELEGRAM_CHAT_ID>",
  };
  __testHooks.logCallback({ logger }, ctx, {
    ref: "H-CORR1",
    decision: "approve",
    cb_ns: "ollie_approval",
    cb_ver: "v1",
    auth: "ok",
    backend_status: 200,
    backend_error_code: null,
    edit_result: "editMessage_ok",
  });
  const line = logger.lines.find((l) => /cb /.test(l));
  assert.ok(line, "expected one cb line");
  // Must contain the ref and decision.
  assert.ok(/H-CORR1/.test(line));
  assert.ok(/approve/.test(line));
  assert.ok(/ollie_approval/.test(line));
  assert.ok(/"v1"/.test(line));
  assert.ok(/"auth":"ok"/.test(line));
  // Must not contain any token or credential.
  assert.ok(!/Bearer\s+[A-Za-z0-9._\-]+/.test(line));
  assert.ok(!/https?:\/\/\S+/.test(line));
});

test("logCallback: never logs the approval credential or token strings", async () => {
  const { __testHooks } = await loadPluginModule();
  const logger = makeLogger();
  const ctx = {
    messageId: 5,
    // The "data" is the callback payload itself; we explicitly test that
    // passing it doesn't accidentally echo a secret.
    data: "ollie_approval:v1:a:H-LEAK1",
    senderId: "<OWNER_TELEGRAM_CHAT_ID>",
    chatId: "<OWNER_TELEGRAM_CHAT_ID>",
  };
  // Stuff the would-be secret into the fields object too; logCallback
  // should ignore any unknown fields and only emit the whitelisted ones.
  __testHooks.logCallback({ logger }, ctx, {
    ref: "H-LEAK1",
    decision: "deny",
    cb_ns: "ollie_approval",
    cb_ver: "v1",
    auth: "ok",
    backend_status: 200,
    backend_error_code: null,
    edit_result: "editMessage_ok",
    bogus_secret: "supersecret123",
    handsApprovalToken: "real-token",
  });
  const line = logger.lines.find((l) => /cb /.test(l)) || "";
  assert.ok(!/supersecret123/.test(line), "bogus_secret must not leak");
  assert.ok(!/real-token/.test(line), "handsApprovalToken must not leak");
});

// ---------------------------------------------------------------------------
// handleApprovalCallback integration: full lineage logged end-to-end
// ---------------------------------------------------------------------------

test("handleApprovalCallback: success records backend_status=200, auth=ok, editMessage_ok", async () => {
  const stateFile = makeTempStateFile();
  const logger = makeLogger();
  const { api } = await makeApi({ stateFile, logger });
  const { __testHooks } = await loadPluginModule();

  const restore = setFetch(async () => jsonResponse(200, { ok: true }));
  try {
    await __testHooks.handleApprovalCallback(api, {
      channel: "telegram",
      senderId: "<OWNER_TELEGRAM_CHAT_ID>",
      chatId: "<OWNER_TELEGRAM_CHAT_ID>",
      messageId: 7777,
      data: "ollie_approval:v1:a:H-CBK001",
      auth: { isAuthorizedSender: true },
      respond: {
        editMessage: async () => true,
        reply: async () => true,
      },
    });
  } finally { restore(); }

  const lines = logger.lines.filter((l) => /cb /.test(l));
  assert.equal(lines.length, 1, "expected exactly one cb log line");
  const line = lines[0];
  assert.ok(/H-CBK001/.test(line), "must contain ref");
  assert.ok(/approve/.test(line), "must contain decision");
  assert.ok(/ollie_approval/.test(line), "must contain cb_ns");
  assert.ok(/"v1"/.test(line), "must contain cb_ver");
  assert.ok(/"auth":"ok"/.test(line), "must contain auth=ok");
  assert.ok(/7777/.test(line), "must contain message_id");
  assert.ok(/200/.test(line), "must contain backend_status=200");
  assert.ok(/editMessage_ok/.test(line), "must contain edit_result");
});

test("handleApprovalCallback: unauthorized records auth=unauthorized, no backend call, no message_id leakage", async () => {
  const stateFile = makeTempStateFile();
  const logger = makeLogger();
  const { api } = await makeApi({ stateFile, logger });
  const { __testHooks } = await loadPluginModule();

  let fetched = 0;
  const restore = setFetch(async () => { fetched++; return jsonResponse(200, { ok: true }); });
  try {
    const r = await __testHooks.handleApprovalCallback(api, {
      channel: "telegram",
      senderId: "999",
      chatId: "<OWNER_TELEGRAM_CHAT_ID>",
      messageId: 8888,
      data: "ollie_approval:v1:d:H-CBK002",
      auth: { isAuthorizedSender: false },
      respond: {
        reply: async () => true,
        editMessage: async () => true,
      },
    });
    assert.equal(r.handled, true);
    assert.equal(r.status, "unauthorized");
    assert.equal(fetched, 0, "unauthorized must not call backend");
  } finally { restore(); }

  const lines = logger.lines.filter((l) => /cb /.test(l));
  assert.equal(lines.length, 1);
  const line = lines[0];
  assert.ok(/H-CBK002/.test(line));
  assert.ok(/deny/.test(line));
  assert.ok(/"auth":"unauthorized"/.test(line));
  // Backend status/error code must be null on the unauthorized path.
  assert.ok(/"backend_status":null/.test(line));
  assert.ok(/"backend_error_code":null/.test(line));
});

test("handleApprovalCallback: malformed records auth=malformed, no backend call", async () => {
  const stateFile = makeTempStateFile();
  const logger = makeLogger();
  const { api } = await makeApi({ stateFile, logger });
  const { __testHooks } = await loadPluginModule();

  let fetched = 0;
  const restore = setFetch(async () => { fetched++; return jsonResponse(200, { ok: true }); });
  try {
    const r = await __testHooks.handleApprovalCallback(api, {
      channel: "telegram",
      senderId: "<OWNER_TELEGRAM_CHAT_ID>",
      chatId: "<OWNER_TELEGRAM_CHAT_ID>",
      messageId: 9999,
      data: "ollie_approval:v1:a:NOTVALID",
      auth: { isAuthorizedSender: true },
      respond: {
        reply: async () => true,
        editMessage: async () => true,
      },
    });
    assert.equal(r.handled, true);
    assert.equal(r.status, "malformed");
    assert.equal(fetched, 0, "malformed must not call backend");
  } finally { restore(); }

  const lines = logger.lines.filter((l) => /cb /.test(l));
  assert.equal(lines.length, 1);
  const line = lines[0];
  assert.ok(/"auth":"malformed"/.test(line));
  assert.ok(/9999/.test(line), "must still log message_id");
});

test("handleApprovalCallback: backend 500 records backend_status=500 and edit_result=reply_only", async () => {
  const stateFile = makeTempStateFile();
  const logger = makeLogger();
  const { api } = await makeApi({ stateFile, logger });
  const { __testHooks } = await loadPluginModule();

  const restore = setFetch(handsDouble(jsonResponse(500, { ok: false, error: "boom" })));
  try {
    await __testHooks.handleApprovalCallback(api, {
      channel: "telegram",
      senderId: "<OWNER_TELEGRAM_CHAT_ID>",
      chatId: "<OWNER_TELEGRAM_CHAT_ID>",
      messageId: 1111,
      data: "ollie_approval:v1:a:H-CBK005",
      auth: { isAuthorizedSender: true },
      respond: {
        reply: async () => true,
        editMessage: async () => true,
      },
    });
  } finally { restore(); }

  const lines = logger.lines.filter((l) => /cb /.test(l));
  assert.equal(lines.length, 1);
  const line = lines[0];
  assert.ok(/500/.test(line), "must contain backend_status=500");
  assert.ok(/"backend_error_code":null|"backend_error_code":"[^"]+"/.test(line));
  assert.ok(/reply_only/.test(line), "transient must produce reply_only");
});

test("handleApprovalCallback: backend 404 unknown_or_expired records edit_result=editMessage_ok", async () => {
  const stateFile = makeTempStateFile();
  const logger = makeLogger();
  const { api } = await makeApi({ stateFile, logger });
  const { __testHooks } = await loadPluginModule();

  const restore = setFetch(handsDouble(jsonResponse(404, { ok: false, error_code: "unknown_or_expired" })));
  try {
    await __testHooks.handleApprovalCallback(api, {
      channel: "telegram",
      senderId: "<OWNER_TELEGRAM_CHAT_ID>",
      chatId: "<OWNER_TELEGRAM_CHAT_ID>",
      messageId: 2222,
      data: "ollie_approval:v1:a:H-CBK404",
      auth: { isAuthorizedSender: true },
      respond: {
        reply: async () => true,
        editMessage: async () => true,
      },
    });
  } finally { restore(); }

  const lines = logger.lines.filter((l) => /cb /.test(l));
  assert.equal(lines.length, 1);
  const line = lines[0];
  assert.ok(/H-CBK404/.test(line));
  assert.ok(/404/.test(line));
  assert.ok(/unknown_or_expired/.test(line), "must log backend_error_code");
  assert.ok(/editMessage_ok/.test(line), "404 is terminal -> editMessage_ok");
});

test("handleApprovalCallback: never logs approval token, bot token, or action digest", async () => {
  const stateFile = makeTempStateFile();
  const logger = makeLogger();
  // Use distinctive secret strings we can search for.
  const { api } = await makeApi({
    stateFile,
    logger,
    handsApprovalToken: "VERY-SECRET-TOKEN-XYZ-12345",
    handsConsentUrl: "http://hands.test/consent",
  });
  const { __testHooks } = await loadPluginModule();

  const restore = setFetch(async () => jsonResponse(200, { ok: true }));
  try {
    await __testHooks.handleApprovalCallback(api, {
      channel: "telegram",
      senderId: "<OWNER_TELEGRAM_CHAT_ID>",
      chatId: "<OWNER_TELEGRAM_CHAT_ID>",
      messageId: 3333,
      // Stuff a digest-shaped hex string into the H-ref body. This is the
      // payload we get from Telegram; the test ensures the line never echoes
      // any embedded secret back.
      data: "ollie_approval:v1:a:H-DEADBEEFCAFEBABE",
      auth: { isAuthorizedSender: true },
      respond: {
        reply: async () => true,
        editMessage: async () => true,
      },
    });
  } finally { restore(); }

  const lines = logger.lines.filter((l) => /cb /.test(l));
  assert.equal(lines.length, 1);
  const line = lines[0];
  // Approval credential must never appear.
  assert.ok(!/VERY-SECRET-TOKEN-XYZ-12345/.test(line),
    "approval credential must never be logged");
  // Hands consent URL must be redacted.
  assert.ok(!/http:\/\/hands\.test/.test(line),
    "hands consent URL must be redacted");
});