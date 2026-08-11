// Owner-command routing: channel authority, ref namespaces, and the
// digest-bound Hands callback.
//
// The rules under test come from the settled routing doctrine:
//   * Contact gating (W-) is Telegram-only in BOTH directions. WhatsApp must
//     never be the surface for deciding whether Ollie talks to a stranger,
//     because the trigger is the stranger's message, not the owner's, so there
//     is no 24h-window guarantee.
//   * Hands (H-) commands from owner WhatsApp ARE legitimate: the owner's own
//     message opened the window (the interactive case).
//   * Identity resolution fails closed — an unresolvable sender gets guest
//     treatment, never owner.
//   * Button-tap approvals must carry the action digest bound to the ref, read
//     from the engine's pending inventory. If that inventory is unreachable,
//     fail closed: approve nothing.

import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const modulePromise = import(pathToFileURL(new URL("../index.js", import.meta.url).pathname).href);

const OWNER_TELEGRAM = "<OWNER_TELEGRAM_CHAT_ID>";
const OWNER_WHATSAPP = "+15550000000";

function makeStateFile(state = {}) {
  const dir = mkdtempSync(join(tmpdir(), "wa-approval-routing-"));
  const file = join(dir, "whatsapp-contacts.json");
  writeFileSync(file, JSON.stringify({ approved: [], blocked: [], pending: {}, ...state }, null, 2));
  return file;
}

function readStateFile(file) {
  return JSON.parse(readFileSync(file, "utf8"));
}

/** A pending contact request that will not expire during the test. */
function contactPending(from = "+19998887777", kind = "inbound") {
  return {
    from,
    preview: "hello",
    kind,
    requestedAt: new Date().toISOString(),
    expiresAt: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
  };
}

async function makeApi({ stateFile, enabled = false } = {}) {
  const { default: plugin } = await modulePromise;
  const sent = [];
  const hooks = new Map();
  const api = {
    config: {
      plugins: {
        entries: {
          "ollie-wa-approval": {
            config: {
              enabled,
              approvalsFile: stateFile,
              ownerTelegramChatId: OWNER_TELEGRAM,
              ownerWhatsAppNumber: OWNER_WHATSAPP,
              handsApprovalToken: "secret",
              handsConsentUrl: "http://hands.test/consent",
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
          loadAdapter: async (channel) => ({
            sendText: async ({ to, text }) => { sent.push({ channel, to, text }); return true; },
          }),
        },
      },
    },
    logger: { info() {}, warn() {}, error() {} },
    on(name, fn) { hooks.set(name, fn); },
    registerInteractiveHandler() {},
  };
  plugin.register(api);
  return { api, sent, hooks };
}

function setFetch(mock) {
  const original = globalThis.fetch;
  globalThis.fetch = mock;
  return () => { globalThis.fetch = original; };
}

function jsonResponse(status, body) {
  return { status, ok: status >= 200 && status < 300, async json() { return body; } };
}

/** Hands double: healthy inventory on GET, caller-chosen response on POST. */
function handsDouble(calls, { pending = [], consent = jsonResponse(200, { ok: true }) } = {}) {
  return async (url, options) => {
    calls.push({ url, method: options?.method ?? "GET", body: options?.body });
    if (!options?.method || options.method === "GET") {
      return jsonResponse(200, { ok: true, pending });
    }
    return consent;
  };
}

// ---------------------------------------------------------------------------
// parseOwnerCommand: shared grammar + the optional typed digest argument
// ---------------------------------------------------------------------------

test("parseOwnerCommand: accepts the optional trailing action digest", async () => {
  const { __testHooks } = await modulePromise;
  const digest = "a1b2c3d4e5f60718";
  assert.deepEqual(__testHooks.parseOwnerCommand(`approve H-aB9_x1 ${digest}`), {
    decision: "approve", ref: "H-aB9_x1", digest,
  });
  assert.deepEqual(__testHooks.parseOwnerCommand(`deny H-aB9_x1 ${digest}`), {
    decision: "deny", ref: "H-aB9_x1", digest,
  });
});

test("parseOwnerCommand: digest is optional and refs keep their exact bytes", async () => {
  const { __testHooks } = await modulePromise;
  assert.deepEqual(__testHooks.parseOwnerCommand("approve H-aB9_x1"), {
    decision: "approve", ref: "H-aB9_x1", digest: "",
  });
  assert.deepEqual(__testHooks.parseOwnerCommand("deny W-Zz0-11"), {
    decision: "deny", ref: "W-Zz0-11", digest: "",
  });
});

test("parseOwnerCommand: a bare decision is legal and carries no ref", async () => {
  const { __testHooks } = await modulePromise;
  assert.deepEqual(__testHooks.parseOwnerCommand("approve"), {
    decision: "approve", ref: null, digest: "",
  });
});

test("parseOwnerCommand: rejects non-commands and lowercase ref prefixes", async () => {
  const { __testHooks } = await modulePromise;
  assert.equal(__testHooks.parseOwnerCommand("please approve H-aB9_x1"), null);
  assert.equal(__testHooks.parseOwnerCommand("approve h-aB9_x1"), null);
  assert.equal(__testHooks.parseOwnerCommand("approve X-aB9_x1"), null);
  assert.equal(__testHooks.parseOwnerCommand("something else"), null);
  assert.equal(__testHooks.parseOwnerCommand(undefined), null);
});

// ---------------------------------------------------------------------------
// DOCTRINE: W- contact approvals are refused off Telegram
// ---------------------------------------------------------------------------

test("W- ref from WhatsApp is refused and never touches contact state", async () => {
  const stateFile = makeStateFile({ pending: { "W-abc123": contactPending() } });
  const { api } = await makeApi({ stateFile });
  const { __testHooks } = await modulePromise;

  const calls = [];
  const restore = setFetch(handsDouble(calls));
  let reply;
  try {
    reply = await __testHooks.routeOwnerApproval(
      api, { decision: "approve", ref: "W-abc123", digest: "" }, { allowContact: false },
    );
  } finally { restore(); }

  assert.match(reply, /only be decided on Telegram/i);
  const after = readStateFile(stateFile);
  assert.ok(after.pending["W-abc123"], "the pending contact request must survive untouched");
  assert.deepEqual(after.approved, [], "the stranger must not have been approved");
  assert.deepEqual(after.blocked, [], "the stranger must not have been blocked either");
  assert.equal(calls.length, 0, "a refused contact decision must not call any backend");
});

test("bare approve from WhatsApp cannot auto-select a pending W- contact", async () => {
  // Exactly one pending approval exists and it is a contact request. On
  // Telegram a bare "approve" would resolve to it; on WhatsApp it must be
  // invisible, so the command resolves to nothing at all.
  const stateFile = makeStateFile({ pending: { "W-solo01": contactPending("+19990001111") } });
  const { api } = await makeApi({ stateFile });
  const { __testHooks } = await modulePromise;

  const calls = [];
  const restore = setFetch(handsDouble(calls, { pending: [] }));
  let reply;
  try {
    reply = await __testHooks.routeOwnerApproval(
      api, { decision: "approve", ref: null, digest: "" }, { allowContact: false },
    );
  } finally { restore(); }

  assert.match(reply, /no pending approval/i);
  const after = readStateFile(stateFile);
  assert.ok(after.pending["W-solo01"], "contact request must still be pending");
  assert.deepEqual(after.approved, []);
});

test("WhatsApp ambiguity inventory never lists a stranger's contact request", async () => {
  // Two Hands approvals force the ambiguity prompt. The contact request must
  // not appear in it — listing it would both offer an illegal choice and leak
  // the stranger's phone number onto WhatsApp.
  const stateFile = makeStateFile({ pending: { "W-leak01": contactPending("+19995554444") } });
  const { api } = await makeApi({ stateFile });
  const { __testHooks } = await modulePromise;

  const restore = setFetch(handsDouble([], {
    pending: [
      { ref: "H-one", preview: "click a button" },
      { ref: "H-two", preview: "run a script" },
    ],
  }));
  let reply;
  try {
    reply = await __testHooks.routeOwnerApproval(
      api, { decision: "approve", ref: null, digest: "" }, { allowContact: false },
    );
  } finally { restore(); }

  assert.match(reply, /H-one/);
  assert.match(reply, /H-two/);
  assert.ok(!reply.includes("W-leak01"), "contact ref must not be offered on WhatsApp");
  assert.ok(!reply.includes("19995554444"), "stranger's number must not leak onto WhatsApp");
});

test("W- ref from Telegram is accepted and applied", async () => {
  // The mirror image of the refusal above: Telegram is the full-authority
  // surface, so the same command succeeds there.
  const stateFile = makeStateFile({ pending: { "W-abc123": contactPending("+19998887777") } });
  const { api } = await makeApi({ stateFile });
  const { __testHooks } = await modulePromise;

  const reply = await __testHooks.routeOwnerApproval(
    api, { decision: "approve", ref: "W-abc123", digest: "" }, { allowContact: true },
  );

  assert.match(reply, /Approved/);
  const after = readStateFile(stateFile);
  assert.equal(after.pending["W-abc123"], undefined, "pending entry must be consumed");
  assert.deepEqual(after.approved, ["+19998887777"]);
});

test("H- ref from WhatsApp is accepted and reaches the Hands backend", async () => {
  const stateFile = makeStateFile();
  const { api } = await makeApi({ stateFile });
  const { __testHooks } = await modulePromise;

  const calls = [];
  const restore = setFetch(handsDouble(calls));
  let reply;
  try {
    reply = await __testHooks.routeOwnerApproval(
      api, { decision: "approve", ref: "H-hands1", digest: "" }, { allowContact: false },
    );
  } finally { restore(); }

  assert.match(reply, /Approved ref H-hands1/);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, "POST");
  assert.deepEqual(JSON.parse(calls[0].body), { ref: "H-hands1", approve: true });
});

test("a typed digest is forwarded to /consent on the H- text path", async () => {
  const stateFile = makeStateFile();
  const { api } = await makeApi({ stateFile });
  const { __testHooks } = await modulePromise;

  const calls = [];
  const restore = setFetch(handsDouble(calls));
  try {
    await __testHooks.routeOwnerApproval(
      api, { decision: "approve", ref: "H-typed1", digest: "feedfacefeedface" }, { allowContact: true },
    );
  } finally { restore(); }

  assert.deepEqual(JSON.parse(calls[0].body), {
    ref: "H-typed1", approve: true, script_hash: "feedfacefeedface",
  });
});

// ---------------------------------------------------------------------------
// before_agent_run: channel authority end to end
// ---------------------------------------------------------------------------

test("before_agent_run: owner WhatsApp W- command is blocked, refused, and answered on WhatsApp", async () => {
  const stateFile = makeStateFile({ pending: { "W-wa0001": contactPending("+19998887777") } });
  const { sent, hooks } = await makeApi({ stateFile });
  const beforeAgentRun = hooks.get("before_agent_run");
  assert.ok(beforeAgentRun, "before_agent_run must be registered");

  const calls = [];
  const restore = setFetch(handsDouble(calls));
  let result;
  try {
    result = await beforeAgentRun(
      { senderId: OWNER_WHATSAPP, prompt: "approve W-wa0001" },
      { messageProvider: "whatsapp" },
    );
  } finally { restore(); }

  assert.deepEqual(result, { outcome: "block", reason: "owner-approval-handled" });
  assert.equal(calls.length, 0, "no backend call for a refused contact decision");
  const reply = sent.find((m) => /only be decided on Telegram/i.test(m.text));
  assert.ok(reply, "owner must be told where to answer");
  assert.equal(reply.channel, "whatsapp", "the refusal answers on the channel it arrived from");
  assert.ok(readStateFile(stateFile).pending["W-wa0001"], "contact must remain pending");
});

test("before_agent_run: owner Telegram W- command is blocked and applied", async () => {
  const stateFile = makeStateFile({ pending: { "W-tg0001": contactPending("+19998887777") } });
  const { sent, hooks } = await makeApi({ stateFile });
  const beforeAgentRun = hooks.get("before_agent_run");

  const result = await beforeAgentRun(
    {
      senderId: OWNER_TELEGRAM,
      channelId: OWNER_TELEGRAM,
      senderIsOwner: true,
      prompt: "approve W-tg0001",
    },
    { messageProvider: "telegram" },
  );

  assert.deepEqual(result, { outcome: "block", reason: "owner-approval-handled" });
  assert.equal(readStateFile(stateFile).pending["W-tg0001"], undefined);
  assert.deepEqual(readStateFile(stateFile).approved, ["+19998887777"]);
  assert.ok(sent.some((m) => m.channel === "telegram" && /Approved/.test(m.text)));
});

test("before_agent_run: a non-owner WhatsApp sender gets no approval authority", async () => {
  const stateFile = makeStateFile({ pending: { "W-guest1": contactPending("+19998887777") } });
  const { sent, hooks } = await makeApi({ stateFile });
  const beforeAgentRun = hooks.get("before_agent_run");

  const calls = [];
  const restore = setFetch(handsDouble(calls));
  let result;
  try {
    result = await beforeAgentRun(
      { senderId: "+19998887777", prompt: "approve H-guest1" },
      { messageProvider: "whatsapp" },
    );
  } finally { restore(); }

  // Identity fails closed: guest treatment, so the command is not intercepted
  // as an owner command and no backend is touched.
  assert.equal(result, undefined);
  assert.equal(calls.length, 0, "a guest must never reach the Hands backend");
  assert.equal(sent.length, 0, "a guest must not be answered by the approval router");
});

test("before_agent_run: an unresolvable WhatsApp sender is not the owner", async () => {
  const stateFile = makeStateFile();
  const { sent, hooks } = await makeApi({ stateFile });
  const beforeAgentRun = hooks.get("before_agent_run");

  const calls = [];
  const restore = setFetch(handsDouble(calls));
  let result;
  try {
    result = await beforeAgentRun(
      { senderId: "", prompt: "approve H-nobody" },
      { messageProvider: "whatsapp" },
    );
  } finally { restore(); }

  assert.equal(result, undefined);
  assert.equal(calls.length, 0);
  assert.equal(sent.length, 0);
});

test("before_agent_run: an approval-shaped message from a non-owner Telegram sender fails closed", async () => {
  const stateFile = makeStateFile();
  const { sent, hooks } = await makeApi({ stateFile });
  const beforeAgentRun = hooks.get("before_agent_run");

  const calls = [];
  const restore = setFetch(handsDouble(calls));
  let result;
  try {
    result = await beforeAgentRun(
      { senderId: "999", channelId: "999", senderIsOwner: false, prompt: "approve H-abc123" },
      { messageProvider: "telegram" },
    );
  } finally { restore(); }

  assert.deepEqual(result, { outcome: "block", reason: "owner-auth-failed" });
  assert.equal(calls.length, 0, "an unauthorized command must never reach the backend");
  assert.ok(sent.some((m) => /Not authorized/i.test(m.text)));
});

test("before_agent_run: senderIsOwner alone cannot authorize a Telegram command", async () => {
  const stateFile = makeStateFile();
  const { hooks } = await makeApi({ stateFile });
  const beforeAgentRun = hooks.get("before_agent_run");

  const calls = [];
  const restore = setFetch(handsDouble(calls));
  let result;
  try {
    // Correct sender, but the message did not arrive in the owner's private chat.
    result = await beforeAgentRun(
      {
        senderId: OWNER_TELEGRAM,
        channelId: "-100999",
        senderIsOwner: true,
        prompt: "approve H-abc123",
      },
      { messageProvider: "telegram" },
    );
  } finally { restore(); }

  assert.deepEqual(result, { outcome: "block", reason: "owner-auth-failed" });
  assert.equal(calls.length, 0);
});

// ---------------------------------------------------------------------------
// Digest binding on the inline-button callback path
// ---------------------------------------------------------------------------

test("callback attaches the digest bound to THIS ref, not another pending row", async () => {
  const stateFile = makeStateFile();
  const { api } = await makeApi({ stateFile });
  const { __testHooks } = await modulePromise;

  const calls = [];
  const restore = setFetch(handsDouble(calls, {
    pending: [
      { ref: "H-other", script_hash: "0000000000000000" },
      { ref: "H-mine", script_hash: "abcabcabcabcabca" },
      { ref: "H-later", script_hash: "1111111111111111" },
    ],
  }));
  try {
    await __testHooks.handleApprovalCallback(api, {
      channel: "telegram",
      senderId: OWNER_TELEGRAM,
      chatId: OWNER_TELEGRAM,
      messageId: 42,
      data: "ollie_approval:v1:a:H-mine",
      auth: { isAuthorizedSender: true },
      respond: { editMessage: async () => true },
    });
  } finally { restore(); }

  const post = calls.find((c) => c.method === "POST");
  assert.ok(post, "the decision must be POSTed");
  assert.deepEqual(JSON.parse(post.body), {
    ref: "H-mine", approve: true, script_hash: "abcabcabcabcabca",
  });
});

test("callback fails closed when the pending inventory is unreachable", async () => {
  const stateFile = makeStateFile();
  const { api } = await makeApi({ stateFile });
  const { __testHooks } = await modulePromise;

  const calls = [];
  const replies = [];
  const restore = setFetch(async (url, options) => {
    calls.push({ url, method: options?.method ?? "GET" });
    if (!options?.method || options.method === "GET") {
      return jsonResponse(503, { ok: false, error: "inventory down" });
    }
    return jsonResponse(200, { ok: true });
  });
  let result;
  try {
    result = await __testHooks.handleApprovalCallback(api, {
      channel: "telegram",
      senderId: OWNER_TELEGRAM,
      chatId: OWNER_TELEGRAM,
      messageId: 43,
      data: "ollie_approval:v1:a:H-closed",
      auth: { isAuthorizedSender: true },
      respond: { reply: async (arg) => { replies.push(arg.text); }, editMessage: async () => true },
    });
  } finally { restore(); }

  assert.equal(result.status, "error");
  assert.ok(!calls.some((c) => c.method === "POST"),
    "no consent decision may be sent without the bound digest");
  assert.ok(replies.some((t) => /nothing was approved/i.test(t)),
    "the owner must be told nothing happened");
});

test("callback fails closed when the inventory request rejects outright", async () => {
  const stateFile = makeStateFile();
  const { api } = await makeApi({ stateFile });
  const { __testHooks } = await modulePromise;

  const calls = [];
  const restore = setFetch(async (_url, options) => {
    calls.push({ method: options?.method ?? "GET" });
    throw new Error("connection refused");
  });
  let result;
  try {
    result = await __testHooks.handleApprovalCallback(api, {
      channel: "telegram",
      senderId: OWNER_TELEGRAM,
      chatId: OWNER_TELEGRAM,
      messageId: 44,
      data: "ollie_approval:v1:d:H-closed2",
      auth: { isAuthorizedSender: true },
      respond: { reply: async () => true, editMessage: async () => true },
    });
  } finally { restore(); }

  assert.equal(result.status, "error");
  assert.equal(calls.length, 1, "only the inventory GET is attempted");
  assert.equal(calls[0].method, "GET");
});

test("callback emits exactly one cb correlation line on the fail-closed path", async () => {
  const stateFile = makeStateFile();
  const lines = [];
  const { api } = await makeApi({ stateFile });
  api.logger = { info: (m) => lines.push(String(m)), warn: (m) => lines.push(String(m)), error: (m) => lines.push(String(m)) };
  const { __testHooks } = await modulePromise;

  const restore = setFetch(async (_url, options) => {
    if (!options?.method || options.method === "GET") {
      return jsonResponse(503, { ok: false, error: "inventory down" });
    }
    return jsonResponse(200, { ok: true });
  });
  try {
    await __testHooks.handleApprovalCallback(api, {
      channel: "telegram",
      senderId: OWNER_TELEGRAM,
      chatId: OWNER_TELEGRAM,
      messageId: 45,
      data: "ollie_approval:v1:a:H-closed3",
      auth: { isAuthorizedSender: true },
      respond: { reply: async () => true },
    });
  } finally { restore(); }

  const cbLines = lines.filter((l) => /: cb \{/.test(l));
  assert.equal(cbLines.length, 1, "one correlation line per callback, on every path");
  assert.match(cbLines[0], /inventory_unavailable/);
  assert.match(cbLines[0], /"auth":"ok"/);
});

// ---------------------------------------------------------------------------
// Owner response transport
// ---------------------------------------------------------------------------

test("sendOwnerResponse falls back to Telegram when the WhatsApp send fails", async () => {
  const { __testHooks } = await modulePromise;
  const sent = [];
  const api = {
    config: { plugins: { entries: { "ollie-wa-approval": { config: {
      ownerTelegramChatId: OWNER_TELEGRAM,
      ownerWhatsAppNumber: OWNER_WHATSAPP,
    } } } } },
    runtime: { channel: { outbound: { loadAdapter: async (channel) => ({
      sendText: async ({ to, text }) => {
        if (channel === "whatsapp") throw new Error("24h window closed");
        sent.push({ channel, to, text });
        return true;
      },
    }) } } },
    logger: { info() {}, warn() {} },
  };

  const ok = await __testHooks.sendOwnerResponse(api, "whatsapp", "decision needed");
  assert.equal(ok, true);
  assert.deepEqual(sent, [{ channel: "telegram", to: OWNER_TELEGRAM, text: "decision needed" }]);
});

test("sendOwnerResponse falls back to Telegram for an unrecognised channel", async () => {
  const { __testHooks } = await modulePromise;
  const sent = [];
  const api = {
    config: { plugins: { entries: { "ollie-wa-approval": { config: {
      ownerTelegramChatId: OWNER_TELEGRAM,
      ownerWhatsAppNumber: OWNER_WHATSAPP,
    } } } } },
    runtime: { channel: { outbound: { loadAdapter: async (channel) => ({
      sendText: async ({ to, text }) => { sent.push({ channel, to, text }); return true; },
    }) } } },
    logger: { info() {}, warn() {} },
  };

  const ok = await __testHooks.sendOwnerResponse(api, "discord", "decision needed");
  assert.equal(ok, true);
  assert.deepEqual(sent, [{ channel: "telegram", to: OWNER_TELEGRAM, text: "decision needed" }]);
});

// ---------------------------------------------------------------------------
// Ref namespace
// ---------------------------------------------------------------------------

test("makeRef namespaces contact and hands refs", async () => {
  const { __testHooks } = await modulePromise;
  assert.match(__testHooks.makeRef("W"), /^W-[A-Za-z0-9_-]{6}$/);
  assert.match(__testHooks.makeRef("H"), /^H-[A-Za-z0-9_-]{6}$/);
  assert.match(__testHooks.makeRef(), /^W-/, "contact is the default namespace");
});

test("contact gating mints W- refs and prompts on Telegram only", async () => {
  const stateFile = makeStateFile();
  const { api, sent } = await makeApi({ stateFile, enabled: true });
  const { __testHooks } = await modulePromise;

  const result = await __testHooks.evaluateInbound(api, "+19993334444", "hi there");

  assert.equal(result.allowed, false);
  assert.match(result.ref, /^W-/);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].channel, "telegram", "contact prompts are Telegram-only, both directions");
  assert.equal(sent[0].to, OWNER_TELEGRAM);
  assert.ok(readStateFile(stateFile).pending[result.ref]);
});
