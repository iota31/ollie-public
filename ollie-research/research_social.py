"""SOCIAL (Instagram / X) adapter for Ollie's Curiosity Engine.

A *minimal* client to the ollie-hands MCP (FastMCP streamable-HTTP, :3200 on
the Tailscale box). It drives the stealth Camoufox browser through the engine's
``act`` tool — READS ONLY (goto / extract / links, all NOTIFY-tier, auto-run,
no consent prompt) — and turns recent posts on a tracked profile into the
engine's shared *candidate* records.

Hard rules baked in here:
  * stdlib only (urllib for HTTP + SSE; no third-party deps on the gateway box);
  * ``poll(sources)`` NEVER raises and NEVER blocks the rest of the engine —
    if the hands engine is inert / down / 401 / unreachable, or the social lane
    is otherwise unusable, it logs once (info) and returns ``[]``;
  * we NEVER issue a write op (click / fill / type_text / press / select / send
    / post). The only browser ops we ever send are goto / extract / links, plus
    the read-only ``session_info`` probe. There is a defensive allow-list gate
    so a future edit can't accidentally smuggle a write through.

Transport (verified live against ollie-hands, see server.py):
  1. POST initialize  -> response carries an ``mcp-session-id`` header; the body
     is an SSE stream (``data:`` lines are JSON-RPC frames).
  2. POST notifications/initialized  (with the mcp-session-id header).
  3. POST tools/call  (with the mcp-session-id header).

A FastMCP str-returning tool answers with
``result.content[0].text`` = a JSON *string*; we parse that to get the real
payload. ``session_info`` -> {hands_enabled, platform, session:{locked}, ...}.
``act`` -> {action, preview, policy, status, result:{...verb output...}}.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------- #
# Configuration — all module-overridable for tests (no env required to mock).  #
# --------------------------------------------------------------------------- #

# Tailscale IP of the box (NOT 127.0.0.1). Override in tests / per-deploy.
MCP_BASE_URL = os.environ.get("OLLIE_HANDS_MCP_URL", "http://<TAILSCALE_IP>:3200/mcp")

# MCP protocol version we advertise on initialize (server negotiates).
PROTOCOL_VERSION = "2025-06-18"

# Network timeouts (seconds) and limits.
HTTP_TIMEOUT = 30
MAX_POSTS_PER_SOURCE = 6      # cap permalinks we drill into per profile
LINKS_LIMIT = 60             # how many anchors to pull from a profile page
TEXT_CAP = 1500              # candidate.text hard cap (chars)

# The ONLY browser ops this adapter is ever allowed to send. Defense in depth:
# _browser_read() refuses anything outside this set, so a future edit cannot
# smuggle a write op (click/fill/type_text/press/select/send/post) through.
_ALLOWED_BROWSER_OPS = frozenset({"goto", "extract", "links"})

# Sources types this adapter owns.
_OWNED_TYPES = frozenset({"instagram", "x"})

_LOG_ONCE_SEEN: set[str] = set()


def _home() -> str:
    """Resolve the gateway home. OLLIE_HOME wins (testability), then HOME,
    then the deployed default. Used for the openclaw.json + log paths."""
    return os.environ.get("OLLIE_HOME") or os.environ.get("HOME") or "/home/openclaw"


def _openclaw_json_path() -> str:
    return os.path.join(_home(), ".openclaw", "openclaw.json")


def _log_path() -> str:
    return os.path.join(_home(), ".openclaw", "logs", "research-social.log")


# --------------------------------------------------------------------------- #
# Logging — guarded, append-only, never throws.                               #
# --------------------------------------------------------------------------- #

def _log(level: str, msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [{level}] research-social: {msg}\n"
    try:
        path = _log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        # Logging must never break the lane.
        pass


def _log_once(key: str, level: str, msg: str) -> None:
    if key in _LOG_ONCE_SEEN:
        return
    _LOG_ONCE_SEEN.add(key)
    _log(level, msg)


# --------------------------------------------------------------------------- #
# Token.                                                                       #
# --------------------------------------------------------------------------- #

def _read_token() -> str:
    """Read the bearer header value from openclaw.json. It is already stored as
    ``"Bearer <token>"`` at mcp.servers.hands.headers.Authorization, so we use
    it verbatim as the Authorization header value. Override this whole function
    in tests; transport mocks make it irrelevant anyway."""
    with open(_openclaw_json_path(), "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data["mcp"]["servers"]["hands"]["headers"]["Authorization"]


# --------------------------------------------------------------------------- #
# Errors used to signal "social lane is simply down -> return []".            #
# --------------------------------------------------------------------------- #

class HandsUnavailable(Exception):
    """The hands engine is inert / down / unauthorized / unreachable."""


# --------------------------------------------------------------------------- #
# Minimal FastMCP streamable-HTTP client (stdlib urllib + SSE data: parsing).  #
# --------------------------------------------------------------------------- #

class HandsClient:
    """One short-lived MCP session against ollie-hands.

    The single network seam is :meth:`_post` — tests monkeypatch *that* and get
    real SSE parsing / JSON-RPC handling above it for free.
    """

    def __init__(self, base_url: str | None = None, token: str | None = None):
        self.base_url = base_url or MCP_BASE_URL
        self._token = token
        self.session_id: str | None = None
        self._id = 0

    # -- network seam -------------------------------------------------------- #

    def _auth(self) -> str:
        if self._token is None:
            self._token = _read_token()
        return self._token

    def _post(self, body: bytes, headers: dict) -> tuple[int, dict, bytes]:
        """POST to the MCP endpoint. Returns (status, lower-cased headers, body
        bytes). Raises on transport error / HTTP error (callers translate that
        to 'lane down'). THIS is the mock point for tests."""
        req = urllib.request.Request(self.base_url, data=body, headers=headers,
                                     method="POST")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, hdrs, raw

    # -- JSON-RPC over SSE --------------------------------------------------- #

    def _headers(self) -> dict:
        h = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Authorization": self._auth(),
        }
        if self.session_id:
            h["mcp-session-id"] = self.session_id
        return h

    def _rpc(self, method: str, params: dict | None = None, *,
             notification: bool = False, capture_session: bool = False):
        """Send one JSON-RPC frame. For notifications returns None. Otherwise
        returns the matching ``result`` object (raises on JSON-RPC error)."""
        frame: dict = {"jsonrpc": "2.0", "method": method}
        if not notification:
            self._id += 1
            frame["id"] = self._id
        if params is not None:
            frame["params"] = params

        body = json.dumps(frame).encode("utf-8")
        try:
            status, hdrs, raw = self._post(body, self._headers())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise HandsUnavailable("401 unauthorized") from e
            raise HandsUnavailable(f"http {e.code}") from e
        except (urllib.error.URLError, ConnectionRefusedError, socket.timeout,
                TimeoutError, OSError) as e:
            raise HandsUnavailable(f"transport: {e}") from e

        if capture_session and hdrs.get("mcp-session-id"):
            self.session_id = hdrs["mcp-session-id"]

        if notification:
            return None

        frames = _parse_sse(raw)
        return _select_result(frames, self._id)

    # -- handshake + tool calls --------------------------------------------- #

    def connect(self) -> None:
        """initialize -> capture mcp-session-id -> notifications/initialized."""
        self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "ollie-research-social", "version": "0"},
            },
            capture_session=True,
        )
        self._rpc("notifications/initialized", notification=True)

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        """tools/call -> parse the FastMCP text content into the tool payload."""
        result = self._rpc("tools/call",
                            {"name": name, "arguments": arguments or {}})
        return _tool_payload(result)

    # -- typed helpers ------------------------------------------------------- #

    def session_info(self) -> dict:
        return self.call_tool("session_info", {})

    def _browser_read(self, op: str, **args) -> dict:
        """Send a READ-ONLY browser op via the ``act`` tool. Refuses anything
        outside the allow-list — writes can never leave this client."""
        if op not in _ALLOWED_BROWSER_OPS:
            raise ValueError(f"refusing non-read browser op: {op!r}")
        payload = self.call_tool("act", {"kind": "browser", "op": op, **args})
        # act envelope: {action, preview, policy, status, result}. A blocked /
        # denied / errored step has status != ok and no usable result.
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise HandsUnavailable(
                f"act {op} status={payload.get('status') if isinstance(payload, dict) else '?'}")
        return payload.get("result") or {}

    def goto(self, url: str) -> dict:
        return self._browser_read("goto", url=url)

    def extract(self, selector: str = "") -> dict:
        return self._browser_read("extract", selector=selector)

    def links(self, limit: int = LINKS_LIMIT) -> dict:
        return self._browser_read("links", limit=limit)


# --------------------------------------------------------------------------- #
# SSE + JSON-RPC framing helpers (module-level, easy to unit test).            #
# --------------------------------------------------------------------------- #

def _parse_sse(raw: bytes) -> list[dict]:
    """Pull JSON objects out of an SSE stream: every ``data:`` line is parsed as
    a JSON-RPC frame. Tolerates plain-JSON bodies (non-SSE) and junk lines."""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "replace")
    else:
        text = raw or ""
    frames: list[dict] = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        chunk = line[len("data:"):].strip()
        if not chunk:
            continue
        try:
            obj = json.loads(chunk)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            frames.append(obj)
    if not frames:
        # Some deployments answer initialize/tools-call as bare JSON, not SSE.
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                frames.append(obj)
        except (ValueError, TypeError):
            pass
    return frames


def _select_result(frames: list[dict], want_id: int) -> dict:
    """Return the ``result`` of the JSON-RPC frame matching want_id. Falls back
    to the last frame carrying a result. Raises on JSON-RPC error / nothing."""
    chosen = None
    for fr in frames:
        if fr.get("id") == want_id:
            chosen = fr
            break
    if chosen is None:
        for fr in reversed(frames):
            if "result" in fr or "error" in fr:
                chosen = fr
                break
    if chosen is None:
        raise HandsUnavailable("no JSON-RPC result frame in response")
    if "error" in chosen and chosen["error"] is not None:
        raise HandsUnavailable(f"json-rpc error: {chosen['error']}")
    result = chosen.get("result")
    if not isinstance(result, dict):
        raise HandsUnavailable("json-rpc result missing/not an object")
    return result


def _tool_payload(result: dict) -> dict:
    """Unwrap a FastMCP tools/call result into the tool's own dict payload.

    A str-returning FastMCP tool answers with result.content[0].text = a JSON
    string; some builds also echo result.structuredContent. Prefer structured,
    else parse the text content. Returns {} if nothing decodable."""
    if not isinstance(result, dict):
        return {}
    sc = result.get("structuredContent")
    if isinstance(sc, dict) and sc:
        # FastMCP wraps a bare value as {"result": <value>}; unwrap if present.
        if set(sc.keys()) == {"result"} and isinstance(sc["result"], dict):
            return sc["result"]
        return sc
    for part in result.get("content") or []:
        if isinstance(part, dict) and part.get("type") == "text":
            txt = part.get("text") or ""
            try:
                obj = json.loads(txt)
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict):
                return obj
    return {}


# --------------------------------------------------------------------------- #
# Candidate parsing.                                                           #
# --------------------------------------------------------------------------- #

_LOGIN_WALL = re.compile(
    r"\b(log\s*in|sign\s*up|create\s+(a\s+)?new\s+account|"
    r"enter\s+your\s+(phone|email|password)|"
    r"see\s+photos\s+and\s+videos\s+from|"
    r"something\s+went\s+wrong|page\s+couldn'?t\s+load|"
    r"try\s+again\s+later)\b", re.I)

# Permalink shapes per platform (host-agnostic; we match on path).
_IG_POST = re.compile(r"/(p|reel|reels|tv)/[^/?#]+", re.I)
_X_POST = re.compile(r"/[A-Za-z0-9_]{1,15}/status(?:es)?/\d+", re.I)

# Loose ISO-8601 timestamp finder (datetime= attrs / printed times bleed into
# innerText sometimes). Best-effort only.
_ISO_TS = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)\b")


def _normalize_url(url: str) -> str:
    """Deterministic url normalization for fingerprinting: trim, drop fragment
    and query, lower-case scheme+host, strip a single trailing slash."""
    u = (url or "").strip()
    u = u.split("#", 1)[0].split("?", 1)[0]
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)([^/]+)(/.*)?$", u)
    if m:
        scheme = m.group(1).lower()
        host = m.group(2).lower()
        path = m.group(3) or ""
        u = scheme + host + path
    if len(u) > 1 and u.endswith("/"):
        u = u[:-1]
    return u


def _fingerprint(url: str, title: str) -> str:
    basis = _normalize_url(url) + "|" + (title or "").strip()
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _looks_like_login_wall(text: str) -> bool:
    return bool(_LOGIN_WALL.search(text or ""))


def _post_re(source_type: str) -> re.Pattern:
    return _IG_POST if source_type == "instagram" else _X_POST


def _profile_url(source: dict) -> str:
    """Resolve the profile/timeline URL to visit from a source record.
    Accepts a full URL in ``target`` or a bare handle."""
    target = (source.get("target") or source.get("url") or "").strip()
    if not target:
        return ""
    if target.startswith("http://") or target.startswith("https://"):
        return target
    handle = target.lstrip("@")
    if source.get("type") == "instagram":
        return f"https://www.instagram.com/{handle}/"
    return f"https://x.com/{handle}"


def _collect_permalinks(links_payload: dict, source_type: str) -> list[dict]:
    """Pull post permalinks (+anchor text) out of a links() result, in order,
    deduped. Each item: {url, text}."""
    rx = _post_re(source_type)
    seen: set[str] = set()
    out: list[dict] = []
    for a in (links_payload or {}).get("links") or []:
        if not isinstance(a, dict):
            continue
        href = (a.get("href") or "").strip()
        if not href or not rx.search(href):
            continue
        norm = _normalize_url(href)
        if norm in seen:
            continue
        seen.add(norm)
        out.append({"url": href, "text": (a.get("text") or "").strip()})
    return out


def _first_line(text: str) -> str:
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if ln:
            return ln
    return ""


def _best_effort_ts(text: str) -> str | None:
    m = _ISO_TS.search(text or "")
    return m.group(1) if m else None


def _make_candidate(source: dict, *, url: str, title: str, text: str,
                    ts: str | None) -> dict:
    title = (title or "").strip()
    body = (text or "").strip()[:TEXT_CAP]
    tags = source.get("domain_tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    return {
        "source_id": source.get("id"),
        "source_type": source.get("type"),
        "url": url,
        "title": title,
        "text": body,
        "ts": ts,
        "domain_tags": [str(t) for t in tags],
        "fingerprint": _fingerprint(url, title),
    }


def _poll_source(client: HandsClient, source: dict) -> list[dict]:
    """Visit one profile, pull permalinks, drill into recent posts (read-only),
    parse to candidates. Defensive everywhere: any empty/odd result -> []."""
    profile = _profile_url(source)
    if not profile:
        return []

    client.goto(profile)
    profile_body = (client.extract("") or {}).get("text") or ""
    links_payload = client.links(LINKS_LIMIT)
    permalinks = _collect_permalinks(links_payload, source.get("type"))

    if not permalinks:
        # No post links surfaced — almost certainly a login wall / locked
        # session rendering. Degrade silently.
        if _looks_like_login_wall(profile_body) or not profile_body:
            _log_once(f"wall:{source.get('id')}", "info",
                      f"{source.get('type')} source {source.get('id')!r} "
                      f"yielded no post links (login wall / locked session)")
        return []

    candidates: list[dict] = []
    for item in permalinks[:MAX_POSTS_PER_SOURCE]:
        post_url = item["url"]
        try:
            client.goto(post_url)
            body = (client.extract("") or {}).get("text") or ""
        except HandsUnavailable:
            # Engine wobbled mid-drill — keep whatever we already gathered.
            break
        except Exception:
            continue

        if _looks_like_login_wall(body) or not body.strip():
            # Drilled into a wall — fall back to the anchor text, no body.
            title = item["text"] or _first_line(profile_body)
            if not title:
                continue
            candidates.append(_make_candidate(
                source, url=post_url, title=title, text="", ts=None))
            continue

        title = _first_line(body) or item["text"]
        candidates.append(_make_candidate(
            source, url=post_url, title=title, text=body,
            ts=_best_effort_ts(body)))

    return candidates


# --------------------------------------------------------------------------- #
# Public entrypoint.                                                           #
# --------------------------------------------------------------------------- #

def poll(sources) -> list[dict]:
    """Public contract. For each ENABLED instagram/x source, return candidate
    records. NEVER raises; returns [] (info-logged once) if the social lane is
    unusable — so it can't block the rest of the Curiosity Engine."""
    try:
        return _poll_impl(sources or [])
    except HandsUnavailable as e:
        _log_once(f"down:{e}", "info", f"social lane down: {e} -> []")
        return []
    except Exception as e:  # pragma: no cover - last-resort guard
        _log("error", f"unexpected: {type(e).__name__}: {e} -> []")
        return []


def _poll_impl(sources) -> list[dict]:
    targets = [s for s in sources
               if isinstance(s, dict)
               and s.get("type") in _OWNED_TYPES
               and s.get("enabled", True)]
    if not targets:
        return []

    client = HandsClient()
    client.connect()

    # Probe FIRST. If hands are disabled / engine inert -> whole lane down.
    info = client.session_info()
    if not info.get("hands_enabled"):
        _log_once("disabled", "info",
                  "ollie-hands reports hands_enabled=false -> social lane down")
        return []

    session = info.get("session") or {}
    if session.get("locked"):
        _log_once("locked", "info",
                  "windows session is locked — Camoufox reads may be degraded; "
                  "parsing defensively")

    candidates: list[dict] = []
    for source in targets:
        try:
            candidates.extend(_poll_source(client, source))
        except HandsUnavailable as e:
            # Engine went away mid-run — stop touching it, return what we have.
            _log_once(f"mid:{e}", "info",
                      f"hands became unavailable mid-poll: {e}")
            break
        except Exception as e:
            _log("error", f"source {source.get('id')!r} failed: "
                          f"{type(e).__name__}: {e}")
            continue
    return candidates
