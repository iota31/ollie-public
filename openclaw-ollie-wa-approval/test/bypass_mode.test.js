import test from "node:test";
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const modulePromise = import(pathToFileURL(new URL("../index.js", import.meta.url).pathname).href);

function api() {
  return {
    config: { plugins: { entries: { "ollie-wa-approval": { config: {
      ownerTelegramChatId: "<OWNER_TELEGRAM_CHAT_ID>",
      handsApprovalToken: "secret",
      handsConsentUrl: "http://hands.test/consent",
      hookTimeoutMs: 5000,
    } } } } },
    logger: { info() {}, warn() {} },
  };
}

function response(status, body) {
  return { status, async json() { return body; } };
}

test("parses only exact Hands mode commands", async () => {
  const { __testHooks } = await modulePromise;
  assert.deepEqual(__testHooks.parseHandsModeCommand("hands mode"), { operation: "status" });
  assert.deepEqual(__testHooks.parseHandsModeCommand("hands normal"), { operation: "set", mode: "normal" });
  assert.deepEqual(__testHooks.parseHandsModeCommand("hands bypass"), { operation: "set", mode: "bypass" });
  assert.equal(__testHooks.parseHandsModeCommand("Hands bypass"), null);
  assert.equal(__testHooks.parseHandsModeCommand("please hands bypass"), null);
});

test("bypass callback is distinct and owner-authorized", async () => {
  const { __testHooks } = await modulePromise;
  assert.deepEqual(
    __testHooks.parseApprovalCallback({ channel: "telegram", data: "ollie_approval:v1:b:H-mode1" }),
    { handled: true, ref: "H-mode1", approve: true, enableBypass: true, stage: "bypass" },
  );
  assert.equal(__testHooks.isAuthorizedOwnerCallback(
    { ownerTelegramChatId: "<OWNER_TELEGRAM_CHAT_ID>" },
    { senderId: "<OWNER_TELEGRAM_CHAT_ID>", chatId: "<OWNER_TELEGRAM_CHAT_ID>", auth: { isAuthorizedSender: true } },
  ), true);
});

test("bypass callback sends one atomic consent request", async () => {
  const { __testHooks } = await modulePromise;
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    // GET = pending inventory (digest source); POST = the atomic consent decision.
    if (!options?.method || options.method === "GET") {
      return response(200, { ok: true, pending: [{ ref: "H-mode2", script_hash: "abc123digest" }] });
    }
    return response(200, { ok: true, mode: "bypass" });
  };
  try {
    await __testHooks.handleApprovalCallback(api(), {
      channel: "telegram",
      senderId: "<OWNER_TELEGRAM_CHAT_ID>",
      chatId: "<OWNER_TELEGRAM_CHAT_ID>",
      data: "ollie_approval:v1:b:H-mode2",
      auth: { isAuthorizedSender: true },
      respond: { editMessage: async () => true },
    });
  } finally {
    globalThis.fetch = original;
  }
  assert.equal(calls.length, 2);
  assert.equal(calls[0].url, "http://hands.test/consent");
  assert.equal(calls[0].options.method, "GET");
  assert.equal(calls[1].url, "http://hands.test/consent");
  assert.equal(calls[1].options.method, "POST");
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    ref: "H-mode2", approve: true, enable_bypass: true, script_hash: "abc123digest",
  });
});

test("mode status and set use approval credential on narrow route", async () => {
  const { __testHooks } = await modulePromise;
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    return response(200, { ok: true, mode: calls.length === 1 ? "normal" : "bypass" });
  };
  try {
    await __testHooks.handleHandsModeCommand(api(), { operation: "status" });
    await __testHooks.handleHandsModeCommand(api(), { operation: "set", mode: "bypass" });
  } finally {
    globalThis.fetch = original;
  }
  assert.equal(calls[0].url, "http://hands.test/mode");
  assert.equal(calls[0].options.headers.Authorization, "Bearer secret");
  assert.equal(calls[1].options.method, "POST");
  assert.deepEqual(JSON.parse(calls[1].options.body), { mode: "bypass" });
});
