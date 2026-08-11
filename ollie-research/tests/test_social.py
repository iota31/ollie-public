"""Offline tests for research_social (the SOCIAL adapter).

The entire MCP transport is mocked at the single network seam,
``HandsClient._post`` — so SSE parsing, JSON-RPC framing, the FastMCP
text-content unwrap, the act-envelope handling and candidate parsing are all
exercised for real against synthetic bytes. No network, no ollie-hands, stdlib
unittest only.
"""

import json
import os
import socket
import sys
import tempfile
import unittest
import urllib.error

# Make `import research_social` work regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import research_social as rs  # noqa: E402


# --------------------------------------------------------------------------- #
# Synthetic MCP transport.                                                     #
# --------------------------------------------------------------------------- #

def _sse(frame: dict) -> bytes:
    """Encode a JSON-RPC frame as an SSE 'message' event, like FastMCP does."""
    return ("event: message\ndata: " + json.dumps(frame) + "\n\n").encode("utf-8")


class ScriptedHands:
    """A fake ollie-hands MCP endpoint. Records every tools/call it receives."""

    def __init__(self):
        self.session = {"engine": "ollie-hands", "version": "0.0",
                        "hands_enabled": True, "platform": "win32",
                        "session": {"locked": False}}
        self.links_payload = {"url": "x", "links": []}
        self.act_status = "ok"
        self.text_by_url = {}      # url -> extract innerText
        self.default_text = ""
        self.calls = []            # [{"name":..., "args":...}]
        self.raw_bodies = []       # every JSON-RPC frame posted (write-op audit)
        self._last_url = ""
        self._session_id_issued = False

    # -- the seam patched onto HandsClient._post ---------------------------- #
    def post(self, body, headers):
        frame = json.loads(body.decode("utf-8"))
        self.raw_bodies.append(frame)
        method = frame.get("method")
        fid = frame.get("id")

        if method == "initialize":
            self._session_id_issued = True
            return 200, {"mcp-session-id": "sess-test"}, _sse({
                "jsonrpc": "2.0", "id": fid,
                "result": {"protocolVersion": rs.PROTOCOL_VERSION,
                           "serverInfo": {"name": "ollie-hands"},
                           "capabilities": {}},
            })

        if method == "notifications/initialized":
            return 202, {}, b""

        if method == "tools/call":
            name = frame["params"]["name"]
            args = frame["params"].get("arguments", {})
            self.calls.append({"name": name, "args": args})
            payload = self._dispatch(name, args)
            return 200, {}, _sse({
                "jsonrpc": "2.0", "id": fid,
                "result": {"content": [{"type": "text",
                                        "text": json.dumps(payload)}],
                           "isError": False},
            })

        raise AssertionError(f"unexpected method {method!r}")

    def _dispatch(self, name, args):
        if name == "session_info":
            return self.session
        if name == "act":
            op = args.get("op")
            if op == "goto":
                self._last_url = args.get("url", "")
                result = {"url": self._last_url, "title": "t"}
            elif op == "extract":
                txt = self.text_by_url.get(self._last_url, self.default_text)
                result = {"url": self._last_url,
                          "selector": args.get("selector", "") or "body",
                          "text": txt}
            elif op == "links":
                result = self.links_payload
            else:
                # Should never happen — the client guards. Make it loud.
                raise AssertionError(f"non-read browser op reached transport: {op!r}")
            return {"action": "browser", "preview": f"browser {op}",
                    "policy": {"tier": "T2", "consent": "notify", "reason": "r"},
                    "status": self.act_status, "result": result}
        raise AssertionError(f"unexpected tool {name!r}")


_WRITE_OPS = {"click", "fill", "type_text", "press", "select", "send", "post"}


class SocialTestBase(unittest.TestCase):
    def setUp(self):
        # Redirect home so logs land in a throwaway dir, and stub the token so
        # no openclaw.json read is attempted.
        self._tmp = tempfile.mkdtemp()
        self._old_home = os.environ.get("OLLIE_HOME")
        os.environ["OLLIE_HOME"] = self._tmp
        self._old_token = rs._read_token
        rs._read_token = lambda: "Bearer test-token"
        self._old_post = rs.HandsClient._post
        rs._LOG_ONCE_SEEN.clear()

    def tearDown(self):
        rs.HandsClient._post = self._old_post
        rs._read_token = self._old_token
        if self._old_home is None:
            os.environ.pop("OLLIE_HOME", None)
        else:
            os.environ["OLLIE_HOME"] = self._old_home

    def install(self, scripted: ScriptedHands):
        # Wrap in a plain function so the descriptor protocol re-binds `self` to
        # the HandsClient instance (a pre-bound method would shift the args).
        rs.HandsClient._post = lambda _client, body, headers: scripted.post(body, headers)

    def install_raising(self, exc):
        def _raise(_self, _body, _headers):
            raise exc
        rs.HandsClient._post = _raise


# --------------------------------------------------------------------------- #
# Lane-down paths.                                                             #
# --------------------------------------------------------------------------- #

class TestLaneDown(SocialTestBase):
    def _src(self):
        return [{"id": "ig1", "type": "instagram", "target": "natgeo",
                 "domain_tags": ["nature"], "enabled": True}]

    def test_hands_disabled_returns_empty_and_no_act(self):
        sh = ScriptedHands()
        sh.session["hands_enabled"] = False
        self.install(sh)
        self.assertEqual(rs.poll(self._src()), [])
        names = [c["name"] for c in sh.calls]
        self.assertEqual(names, ["session_info"])  # probed, then bailed
        self.assertNotIn("act", names)

    def test_http_401_returns_empty(self):
        self.install_raising(urllib.error.HTTPError(
            rs.MCP_BASE_URL, 401, "Unauthorized", {}, None))
        self.assertEqual(rs.poll(self._src()), [])

    def test_connection_refused_returns_empty(self):
        self.install_raising(ConnectionRefusedError("refused"))
        self.assertEqual(rs.poll(self._src()), [])

    def test_timeout_returns_empty(self):
        self.install_raising(socket.timeout("timed out"))
        self.assertEqual(rs.poll(self._src()), [])

    def test_url_error_returns_empty(self):
        self.install_raising(urllib.error.URLError("no route"))
        self.assertEqual(rs.poll(self._src()), [])


# --------------------------------------------------------------------------- #
# Happy path + parsing.                                                        #
# --------------------------------------------------------------------------- #

class TestExtraction(SocialTestBase):
    def test_well_formed_candidates(self):
        sh = ScriptedHands()
        sh.links_payload = {"url": "https://www.instagram.com/natgeo/", "links": [
            {"text": "nav", "href": "https://www.instagram.com/accounts/login/"},
            {"text": "Post One", "href": "https://www.instagram.com/p/AAA111/"},
            {"text": "Post Two", "href": "https://www.instagram.com/reel/BBB222/"},
        ]}
        sh.default_text = "Profile body of natgeo with many posts."
        sh.text_by_url = {
            "https://www.instagram.com/p/AAA111/":
                "First caption line here\nmore body 2024-03-01T10:00:00Z text",
            "https://www.instagram.com/reel/BBB222/":
                "Second caption line\nrest of the reel body",
        }
        self.install(sh)
        src = {"id": "ig1", "type": "instagram", "target": "natgeo",
               "domain_tags": ["nature", "science"], "enabled": True}
        out = rs.poll([src])

        self.assertEqual(len(out), 2)
        c0 = out[0]
        self.assertEqual(c0["source_id"], "ig1")
        self.assertEqual(c0["source_type"], "instagram")
        self.assertEqual(c0["url"], "https://www.instagram.com/p/AAA111/")
        self.assertEqual(c0["title"], "First caption line here")
        self.assertTrue(c0["text"].startswith("First caption line here"))
        self.assertEqual(c0["ts"], "2024-03-01T10:00:00Z")
        self.assertEqual(c0["domain_tags"], ["nature", "science"])
        self.assertEqual(len(c0["fingerprint"]), 64)
        # second post has no ISO timestamp
        self.assertIsNone(out[1]["ts"])

    def test_text_capped_at_1500(self):
        sh = ScriptedHands()
        sh.links_payload = {"links": [
            {"text": "p", "href": "https://x.com/jack/status/123"}]}
        sh.text_by_url = {"https://x.com/jack/status/123": "h\n" + ("z" * 5000)}
        self.install(sh)
        out = rs.poll([{"id": "x1", "type": "x", "target": "jack",
                        "domain_tags": ["tech"], "enabled": True}])
        self.assertEqual(len(out), 1)
        self.assertLessEqual(len(out[0]["text"]), rs.TEXT_CAP)

    def test_x_status_permalinks_detected(self):
        sh = ScriptedHands()
        sh.links_payload = {"links": [
            {"text": "home", "href": "https://x.com/home"},
            {"text": "tweet", "href": "https://x.com/sama/status/99887766"},
        ]}
        sh.text_by_url = {"https://x.com/sama/status/99887766": "A tweet body"}
        self.install(sh)
        out = rs.poll([{"id": "x2", "type": "x", "target": "https://x.com/sama",
                        "domain_tags": [], "enabled": True}])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["url"], "https://x.com/sama/status/99887766")

    def test_max_posts_cap(self):
        sh = ScriptedHands()
        many = [{"text": f"p{i}", "href": f"https://www.instagram.com/p/X{i}/"}
                for i in range(20)]
        sh.links_payload = {"links": many}
        sh.default_text = "body"
        self.install(sh)
        out = rs.poll([{"id": "ig", "type": "instagram", "target": "z",
                        "domain_tags": [], "enabled": True}])
        self.assertEqual(len(out), rs.MAX_POSTS_PER_SOURCE)


# --------------------------------------------------------------------------- #
# Degrade-to-empty paths.                                                      #
# --------------------------------------------------------------------------- #

class TestDegrade(SocialTestBase):
    def test_no_post_links_returns_empty(self):
        sh = ScriptedHands()
        sh.links_payload = {"links": [
            {"text": "Log in", "href": "https://www.instagram.com/accounts/login/"}]}
        sh.default_text = "Log in to see photos and videos from friends."
        self.install(sh)
        out = rs.poll([{"id": "ig", "type": "instagram", "target": "z",
                        "domain_tags": [], "enabled": True}])
        self.assertEqual(out, [])

    def test_empty_extract_returns_empty(self):
        sh = ScriptedHands()
        sh.links_payload = {"links": []}
        sh.default_text = ""
        self.install(sh)
        out = rs.poll([{"id": "ig", "type": "instagram", "target": "z",
                        "domain_tags": [], "enabled": True}])
        self.assertEqual(out, [])

    def test_act_status_error_treated_as_down(self):
        sh = ScriptedHands()
        sh.act_status = "denied"   # e.g. consent path went wrong
        sh.links_payload = {"links": [
            {"text": "p", "href": "https://www.instagram.com/p/AAA/"}]}
        self.install(sh)
        out = rs.poll([{"id": "ig", "type": "instagram", "target": "z",
                        "domain_tags": [], "enabled": True}])
        self.assertEqual(out, [])

    def test_locked_session_still_parses_defensively(self):
        sh = ScriptedHands()
        sh.session["session"]["locked"] = True
        sh.links_payload = {"links": [
            {"text": "p", "href": "https://www.instagram.com/p/AAA/"}]}
        sh.text_by_url = {"https://www.instagram.com/p/AAA/": "Caption body"}
        self.install(sh)
        out = rs.poll([{"id": "ig", "type": "instagram", "target": "z",
                        "domain_tags": [], "enabled": True}])
        self.assertEqual(len(out), 1)  # locked != fatal; we still try


# --------------------------------------------------------------------------- #
# Safety: read-only, source filtering, fingerprinting.                         #
# --------------------------------------------------------------------------- #

class TestSafety(SocialTestBase):
    def test_only_read_ops_ever_sent(self):
        sh = ScriptedHands()
        sh.links_payload = {"links": [
            {"text": "p", "href": "https://www.instagram.com/p/AAA/"},
            {"text": "q", "href": "https://www.instagram.com/p/BBB/"}]}
        sh.default_text = "Some caption"
        self.install(sh)
        rs.poll([{"id": "ig", "type": "instagram", "target": "z",
                  "domain_tags": [], "enabled": True}])

        # Only session_info + act tools were called.
        tool_names = {c["name"] for c in sh.calls}
        self.assertTrue(tool_names <= {"session_info", "act"}, tool_names)
        # Every act op is a read op.
        ops = {c["args"].get("op") for c in sh.calls if c["name"] == "act"}
        self.assertTrue(ops <= {"goto", "extract", "links"}, ops)
        # No write op, and only the browser kind, in any posted frame.
        for frame in sh.raw_bodies:
            args = (frame.get("params") or {}).get("arguments") or {}
            self.assertNotIn(args.get("op"), _WRITE_OPS)
            if "kind" in args:
                self.assertEqual(args["kind"], "browser")

    def test_non_igx_sources_skipped_without_connecting(self):
        # If poll touched the transport for non-ig/x sources it would explode.
        self.install_raising(AssertionError("must not connect for non-social"))
        out = rs.poll([
            {"id": "r1", "type": "rss", "target": "http://x/feed", "enabled": True},
            {"id": "rd", "type": "reddit", "target": "python", "enabled": True},
        ])
        self.assertEqual(out, [])

    def test_disabled_sources_skipped(self):
        self.install_raising(AssertionError("must not connect when all disabled"))
        out = rs.poll([{"id": "ig", "type": "instagram", "target": "z",
                        "enabled": False}])
        self.assertEqual(out, [])

    def test_empty_sources_returns_empty(self):
        self.install_raising(AssertionError("must not connect for empty"))
        self.assertEqual(rs.poll([]), [])
        self.assertEqual(rs.poll(None), [])

    def test_fingerprint_deterministic_and_normalized(self):
        a = rs._fingerprint("https://X.com/A/Status/1/?utm=1#frag", "Title")
        b = rs._fingerprint("https://x.com/A/Status/1", "Title")
        self.assertEqual(a, b)  # case/host/query/fragment/trailing-slash normalized
        self.assertNotEqual(
            a, rs._fingerprint("https://x.com/A/Status/1", "Other"))
        self.assertEqual(len(a), 64)


# --------------------------------------------------------------------------- #
# Framing helpers (unit-level).                                                #
# --------------------------------------------------------------------------- #

class TestFraming(SocialTestBase):
    def test_parse_sse_multiple_and_junk(self):
        raw = (b": comment\n"
               b"event: message\n"
               b'data: {"jsonrpc":"2.0","id":1,"result":{"ok":1}}\n\n'
               b"data: not-json\n"
               b'data: {"jsonrpc":"2.0","id":2,"result":{"ok":2}}\n\n')
        frames = rs._parse_sse(raw)
        self.assertEqual(len(frames), 2)
        self.assertEqual(rs._select_result(frames, 2), {"ok": 2})

    def test_parse_sse_bare_json_fallback(self):
        raw = b'{"jsonrpc":"2.0","id":5,"result":{"hands_enabled":true}}'
        frames = rs._parse_sse(raw)
        self.assertEqual(rs._select_result(frames, 5),
                         {"hands_enabled": True})

    def test_select_result_raises_on_error(self):
        frames = [{"jsonrpc": "2.0", "id": 1,
                   "error": {"code": -32000, "message": "boom"}}]
        with self.assertRaises(rs.HandsUnavailable):
            rs._select_result(frames, 1)

    def test_tool_payload_text_content(self):
        result = {"content": [{"type": "text",
                               "text": json.dumps({"hands_enabled": True})}],
                  "isError": False}
        self.assertEqual(rs._tool_payload(result), {"hands_enabled": True})

    def test_tool_payload_structured_unwrap(self):
        result = {"structuredContent": {"result": {"a": 1}}, "content": []}
        self.assertEqual(rs._tool_payload(result), {"a": 1})

    def test_tool_payload_empty_on_garbage(self):
        self.assertEqual(rs._tool_payload({"content": [{"type": "text",
                                                        "text": "nope"}]}), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
