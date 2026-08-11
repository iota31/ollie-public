// Verify enabled=false still registers the callback handler and text approval path remains active.

import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const pluginModulePromise = import(pathToFileURL(new URL("../index.js", import.meta.url).pathname).href);

function makeTempStateFile() {
  const dir = mkdtempSync(join(tmpdir(), "wa-approval-"));
  const file = join(dir, "whatsapp-contacts.json");
  writeFileSync(file, JSON.stringify({ approved: [], blocked: [], pending: {} }, null, 2));
  return file;
}

async function loadPluginModule() {
  return pluginModulePromise;
}

async function makeApi({ enabled = false } = {}) {
  const interactiveHandlers = [];
  const { default: plugin } = await loadPluginModule();
  const api = {
    config: {
      plugins: {
        entries: {
          "ollie-wa-approval": {
            config: {
              enabled,
              approvalsFile: makeTempStateFile(),
              ownerTelegramChatId: "<OWNER_TELEGRAM_CHAT_ID>",
              ownerWhatsAppNumber: "+15550000000",
              handsApprovalToken: "",
              handsConsentUrl: "http://hands.test/consent",
              hookTimeoutMs: 5000,
              requestTimeoutMinutes: 60,
            },
          },
        },
      },
    },
    runtime: { channel: { outbound: { loadAdapter: async () => ({ sendText: async () => true }) } } },
    logger: { info() {}, warn() {} },
    on() {},
    registerInteractiveHandler(h) { interactiveHandlers.push(h); },
  };
  plugin.register(api);
  return { api, interactiveHandlers };
}

test("enabled=false still registers ollie_approval interactive handler", async () => {
  const { interactiveHandlers } = await makeApi({ enabled: false });
  const ours = interactiveHandlers.find(h => h && h.namespace === "ollie_approval" && h.channel === "telegram");
  assert.ok(ours, "expected ollie_approval handler to be registered even when enabled=false");
});

test("enabled=false still intercepts owner Telegram approval commands pre-LLM (text path)", async () => {
  const { interactiveHandlers } = await makeApi({ enabled: false });
  // We only assert registration here; the command path is covered by router.test.js.
  // This file ensures the structural callback registration is independent of enabled.
  assert.ok(interactiveHandlers.length >= 1);
});

// Exercise the ACTUAL registered handler wrapper (not __testHooks.handleApprovalCallback directly)
// with a verified OpenClaw 2026.5.28-shaped ctx, including respond method exceptions.
test("registered handler wrapper logs callback entry and backend errors", async () => {
  // fresh state + logger capture
  const stateFile = makeTempStateFile();
  const logs = [];
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
              ownerWhatsAppNumber: "+15550000000",
              handsApprovalToken: "secret",
              handsConsentUrl: "http://hands.test/consent",
              hookTimeoutMs: 5000,
              requestTimeoutMinutes: 60,
            },
          },
        },
      },
    },
    runtime: { channel: { outbound: { loadAdapter: async () => ({ sendText: async () => true }) } } },
    logger: { info(...a){ logs.push(["info",...a]); }, warn(...a){ logs.push(["warn",...a]); } },
    on() {},
    registerInteractiveHandler(h) { interactiveHandlers.push(h); },
  };
  plugin.register(api);
  const ours = interactiveHandlers.find(h => h && h.namespace === "ollie_approval" && h.channel === "telegram");
  assert.ok(ours, "expected registered ollie_approval handler");

  function jsonResponse(status, body) {
    return { status, ok: status >= 200 && status < 300, async json() { return body; } };
  }
  function setFetch(mock) {
    const original = globalThis.fetch;
    globalThis.fetch = mock;
    return () => { globalThis.fetch = original; };
  }

  // Terminal success: clearButtons + editMessage must be called; respond exceptions must be observed via logger
  //
  // The callback path issues GET /consent (pending inventory -> bound action
  // digest) before POST /consent (the decision). `mode` drives the POST only;
  // the inventory GET always succeeds, so each case below exercises the branch
  // it is named for rather than the inventory-unavailable fail-closed branch.
  let mode = "ok";
  const restore = setFetch(async (_url, options) => {
    if (!options?.method || options.method === "GET") {
      return jsonResponse(200, { ok: true, pending: [] });
    }
    if (mode === "ok") return jsonResponse(200, { ok: true });
    if (mode === "transient") return jsonResponse(503, { ok: false, error: "down" });
    return jsonResponse(404, { ok: false, error_code: "unknown_or_expired" });
  });
  try {
    const effects = [];
    // OpenClaw 2026.5.28-shaped ctx: senderId + callback.{data,chatId,messageId} + auth + respond
    const ctxOk = {
      senderId: "<OWNER_TELEGRAM_CHAT_ID>",
      callback: {
        data: "ollie_approval:v1:a:H-REG01",
        chatId: "<OWNER_TELEGRAM_CHAT_ID>",
        messageId: 777,
      },
      auth: { isAuthorizedSender: true },
      respond: {
        clearButtons: async () => {
          effects.push("clear");
          // simulate a transient UI failure that should be logged, not rethrown
          throw new Error("clearButtons boom");
        },
        editMessage: async (arg) => {
          if (typeof arg !== "object" || arg === null || typeof arg.text !== "string") {
            throw new Error("editMessage must receive object with text");
          }
          effects.push({ edit: arg.text, buttons: arg && arg.buttons });
          // no throw here
        },
      },
    };
    const res1 = await ours.handler(ctxOk);
    assert.deepEqual(res1, { handled: true });
    assert.ok(logs.some(([level, message]) => level === "info" && /callback received/i.test(String(message))));
    // Terminal uses atomic editMessage({text, buttons:[]}); no prior clearButtons
    assert.ok(!effects.includes("clear"), "terminal must NOT call clearButtons (atomic edit)");
    const edit1 = effects.find(e => e && e.edit && /Approved H-REG01/.test(e.edit));
    assert.ok(edit1, "terminal must edit with Approved");
    assert.deepEqual(edit1 && edit1.buttons, [], "terminal edit must carry buttons:[]");

    // If the authoritative terminal edit fails, the active-looking buttons cannot fail silently:
    // log the edit error and send a separate terminal status message as a reliable fallback.
    effects.length = 0;
    logs.length = 0;
    const resFallback = await ours.handler({
      ...ctxOk,
      callback: { ...ctxOk.callback, data: "ollie_approval:v1:a:H-REG01B" },
      respond: {
        editMessage: async () => { throw new Error("editMessage boom"); },
        reply: async (arg) => effects.push({ reply: arg.text, buttons: arg.buttons }),
      },
    });
    assert.deepEqual(resFallback, { handled: true });
    assert.ok(logs.some(([level, message]) => level === "warn" && /editMessage error.*editMessage boom/i.test(String(message))));
    const fallback = effects.find(e => e && e.reply && /Approved H-REG01B/.test(e.reply));
    assert.ok(fallback, "terminal edit failure must produce a fallback status message");
    assert.deepEqual(fallback.buttons, [], "fallback status must not introduce buttons");

    // Transient: keep buttons (no clear), reply error; respond exceptions still logged
    mode = "transient";
    effects.length = 0;
    logs.length = 0;
    const ctxTransient = {
      senderId: "<OWNER_TELEGRAM_CHAT_ID>",
      callback: {
        data: "ollie_approval:v1:d:H-REG02",
        chat_id: "<OWNER_TELEGRAM_CHAT_ID>", // alternate key to exercise pickStr
      },
      auth: { isAuthorizedSender: true },
      respond: {
        clearButtons: async () => { effects.push("clear"); },
        reply: async (arg) => {
          if (typeof arg !== "object" || arg === null || typeof arg.text !== "string") {
            throw new Error("reply must receive object with text");
          }
          effects.push({ reply: arg.text });
          throw new Error("reply boom");
        },
      },
    };
    const res2 = await ours.handler(ctxTransient);
    assert.deepEqual(res2, { handled: true });
    assert.ok(!effects.includes("clear"), "transient must NOT clear buttons");
    assert.ok(effects.some(e => e && e.reply && /failed/i.test(e.reply)), "transient must reply error");
    const sawReplyWarn = logs.some(([lvl,msg]) => lvl === "warn" && /reply error/i.test(String(msg)));
    assert.ok(sawReplyWarn, "expected warn log for reply exception");

    // Authoritative terminal (404/unknown_or_expired): atomic edit with buttons:[], no prior clear
    mode = "expired";
    effects.length = 0;
    const ctxExpired = {
      senderId: "<OWNER_TELEGRAM_CHAT_ID>",
      callback: { payload: "ollie_approval:v1:a:H-REG03", chatId: "<OWNER_TELEGRAM_CHAT_ID>" }, // payload alias
      auth: { isAuthorizedSender: true },
      respond: {
        clearButtons: async () => { effects.push("clear"); },
        editMessage: async (arg) => { effects.push({ edit: arg && arg.text, buttons: arg && arg.buttons }); },
      },
    };
    const res3 = await ours.handler(ctxExpired);
    assert.deepEqual(res3, { handled: true });
    assert.ok(!effects.includes("clear"), "authoritative terminal must not call clearButtons");
    const ee = effects.find(e => e && e.edit && /Unknown or expired/.test(String(e.edit)));
    assert.ok(ee, "authoritative terminal must edit");
    assert.deepEqual(ee && ee.buttons, []);
    // A rejected Hands fetch is bounded/consumed and visible through the UI.
    globalThis.fetch = async (_url, options) => {
      assert.ok(options.signal, "Hands request must carry an abort signal");
      throw new Error("backend unavailable");
    };
    effects.length = 0;
    logs.length = 0;
    const res4 = await ours.handler({
      senderId: "<OWNER_TELEGRAM_CHAT_ID>",
      callback: { data: "ollie_approval:v1:a:H-REG04", chatId: "<OWNER_TELEGRAM_CHAT_ID>" },
      auth: { isAuthorizedSender: true },
      respond: {
        reply: async (arg) => effects.push({ reply: arg.text }),
      },
    });
    assert.deepEqual(res4, { handled: true });
    assert.ok(effects.some(e => e.reply && /failed/i.test(e.reply)));
  } finally {
    restore();
  }
});
