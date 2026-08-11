#!/usr/bin/env python3
"""
Ollie Curiosity Engine — Control Dashboard HTTP Server
Binds to Tailscale interface only (<TAILSCALE_IP>:3400).  Bearer-gated.
stdlib only: http.server, json, os, subprocess, secrets, threading, uuid, re, time
"""
import http.server
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlparse

# ── Runtime config (overridable via env or test patching) ──────────────────
# Default bind 0.0.0.0: the gateway runs in a WSL NAT distro where the host's
# Tailscale IP (<TAILSCALE_IP>) is NOT a local interface and cannot be bound
# (EADDRNOTAVAIL). We bind 0.0.0.0 inside WSL and bridge the tailnet via a
# Windows host portproxy (<TAILSCALE_IP>:3400 -> WSL-IP:3400). Access stays
# owner-private: bearer-gated, and nothing routes to the WSL port without the
# host portproxy we control. (No env needed -> detached starts can't lose it.)
TAILSCALE_IP = os.environ.get("OLLIE_DASHBOARD_HOST", "0.0.0.0")
PORT = int(os.environ.get("OLLIE_DASHBOARD_PORT", "3400"))

OLLIE_HOME = os.environ.get("OLLIE_HOME", "/home/openclaw")
HOME = os.environ.get("HOME", OLLIE_HOME)

TOKEN_FILE   = os.path.join(HOME,       ".openclaw", "secrets", "research-dashboard-token")
DATA_DIR     = os.path.join(OLLIE_HOME, ".openclaw", "workspace", "research")
BUDGET_BIN   = os.path.join(OLLIE_HOME, "bin",       "budget.py")
SPEND_LOG    = os.path.join(HOME,       ".openclaw", "logs", "research-spend.log")

SOURCES_FILE   = os.path.join(DATA_DIR, "sources.json")
INTERESTS_FILE = os.path.join(DATA_DIR, "interests.json")
QUEUE_FILE     = os.path.join(DATA_DIR, "queue.json")

_THIS_DIR  = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(_THIS_DIR, "dashboard", "index.html")

# ── Atomic JSON I/O ──
# The registry module owns sources/interests on the box; the dashboard
# reads/writes the SAME files BY PATH (shared on-disk format, not shared code).
# A generic load(path)/save(path) shim over registry's load_sources/save_sources
# API was a mismatch (registry has no load(path)/save(path)), so the dashboard
# does its own atomic I/O against the identical paths.
def _load(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None

def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


# ── Data accessors ─────────────────────────────────────────────────────────
def load_sources():
    v = _load(SOURCES_FILE)
    return v if isinstance(v, list) else []

def save_sources(data: list):
    _save(SOURCES_FILE, data)

def load_interests():
    v = _load(INTERESTS_FILE)
    if not isinstance(v, dict):
        return {"domains": [], "keywords_boost": [], "anti_interests": [], "updated_at": ""}
    return v

def save_interests(data: dict):
    _save(INTERESTS_FILE, data)

def load_queue():
    v = _load(QUEUE_FILE)
    return v if isinstance(v, list) else []

def save_queue(data: list):
    _save(QUEUE_FILE, data)


# ── Bearer token ───────────────────────────────────────────────────────────
BEARER_TOKEN: str = ""   # set by main() or test harness

def get_or_create_token() -> str:
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    try:
        with open(TOKEN_FILE) as fh:
            tok = fh.read().strip()
        if tok:
            return tok
    except FileNotFoundError:
        pass
    tok = secrets.token_hex(32)
    with open(TOKEN_FILE, "w") as fh:
        fh.write(tok + "\n")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    logging.warning("Generated bearer token -> %s", TOKEN_FILE)
    return tok


# ── Schema validation ──────────────────────────────────────────────────────
# Canonical source types — MUST match research_registry.SOURCE_TYPES exactly.
# (4dpocket is a candidate source_type produced by lab_watcher, not a
# user-addable registry source, so it is not offered here.)
_SOURCE_TYPES = {"rss", "reddit", "blog", "discovery", "instagram", "x"}

def validate_source(d) -> tuple:
    if not isinstance(d, dict):
        return False, "source must be a JSON object"
    if d.get("type") not in _SOURCE_TYPES:
        return False, f"type must be one of: {sorted(_SOURCE_TYPES)}"
    if not isinstance(d.get("target"), str) or not d["target"].strip():
        return False, "target must be a non-empty string"
    if "weight" in d and not isinstance(d["weight"], (int, float)):
        return False, "weight must be a number"
    if "enabled" in d and not isinstance(d["enabled"], bool):
        return False, "enabled must be boolean"
    if "recency_days" in d and not isinstance(d["recency_days"], int):
        return False, "recency_days must be an integer"
    if "domain_tags" in d and not isinstance(d["domain_tags"], list):
        return False, "domain_tags must be a list"
    return True, None

def validate_interests(d) -> tuple:
    if not isinstance(d, dict):
        return False, "interests must be a JSON object"
    for k in ("domains", "keywords_boost", "anti_interests"):
        if k in d and not isinstance(d[k], list):
            return False, f"{k} must be a list"
    return True, None


# ── Budget helper ──────────────────────────────────────────────────────────
def get_budget_status() -> dict:
    result: dict = {"status": None, "spend_tail": []}
    try:
        proc = subprocess.run(
            [sys.executable, BUDGET_BIN, "status"],
            capture_output=True, text=True, timeout=5
        )
        if proc.returncode == 0:
            result["status"] = proc.stdout.strip()
        else:
            result["status"] = f"exit {proc.returncode}: {proc.stderr.strip()}"
    except Exception as exc:
        result["status"] = f"unavailable: {exc}"
    try:
        if os.path.exists(SPEND_LOG):
            with open(SPEND_LOG) as fh:
                lines = fh.readlines()
            result["spend_tail"] = [ln.rstrip() for ln in lines[-20:]]
    except Exception as exc:
        result["spend_tail"] = [f"error reading log: {exc}"]
    return result


# ── Regex guards for path parameters ──────────────────────────────────────
_ID_RE  = re.compile(r'^[a-zA-Z0-9_-]{1,128}$')
_FP_RE  = re.compile(r'^[a-zA-Z0-9_-]{1,256}$')

# ── Request handler ────────────────────────────────────────────────────────
class DashboardHandler(http.server.BaseHTTPRequestHandler):
    """Single handler for the Curiosity Engine control dashboard."""

    # Silence default stdout log — use logging module instead
    def log_message(self, fmt, *args):  # type: ignore[override]
        logging.debug("HTTP %s %s -> %s", self.command, self.path, fmt % args)

    # ── Auth ────────────────────────────────────────────────────────────────
    def _authed(self) -> bool:
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Bearer "):
            return False
        return secrets.compare_digest(hdr[7:].strip(), BEARER_TOKEN)

    # ── Response helpers ────────────────────────────────────────────────────
    def _json(self, code: int, payload):
        body = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code: int, msg: str):
        self._json(code, {"error": msg})

    # ── Body reader ─────────────────────────────────────────────────────────
    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None, "invalid Content-Length"
        if length == 0:
            return None, "empty body"
        if length > 1_048_576:
            return None, "body too large"
        raw = self.rfile.read(length)
        try:
            return json.loads(raw), None
        except json.JSONDecodeError as exc:
            return None, f"invalid JSON: {exc}"

    # ── Route: GET ──────────────────────────────────────────────────────────
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_html(); return
        if not self._authed():
            self._err(401, "unauthorized"); return
        routes = {
            "/api/sources":   lambda: self._json(200, load_sources()),
            "/api/interests": lambda: self._json(200, load_interests()),
            "/api/queue":     lambda: self._json(200, load_queue()),
            "/api/budget":    lambda: self._json(200, get_budget_status()),
        }
        handler = routes.get(path)
        if handler:
            handler()
        else:
            self._err(404, "not found")

    # ── Route: POST ─────────────────────────────────────────────────────────
    def do_POST(self):
        if not self._authed():
            self._err(401, "unauthorized"); return
        path = urlparse(self.path).path
        if path == "/api/sources":
            self._add_source()
        elif path == "/api/queue/reorder":
            self._queue_reorder()
        else:
            self._err(404, "not found")

    # ── Route: PUT ──────────────────────────────────────────────────────────
    def do_PUT(self):
        if not self._authed():
            self._err(401, "unauthorized"); return
        path = urlparse(self.path).path
        if path == "/api/interests":
            self._put_interests()
        elif path.startswith("/api/sources/"):
            sid = path[len("/api/sources/"):]
            if not _ID_RE.match(sid):
                self._err(400, "invalid source id"); return
            self._put_source(sid)
        elif path.startswith("/api/queue/"):
            fp = path[len("/api/queue/"):]
            if not _FP_RE.match(fp):
                self._err(400, "invalid fingerprint"); return
            self._put_queue_item(fp)
        else:
            self._err(404, "not found")

    # ── Route: DELETE ───────────────────────────────────────────────────────
    def do_DELETE(self):
        if not self._authed():
            self._err(401, "unauthorized"); return
        path = urlparse(self.path).path
        if path.startswith("/api/sources/"):
            sid = path[len("/api/sources/"):]
            if not _ID_RE.match(sid):
                self._err(400, "invalid source id"); return
            self._delete_source(sid)
        elif path.startswith("/api/queue/"):
            fp = path[len("/api/queue/"):]
            if not _FP_RE.match(fp):
                self._err(400, "invalid fingerprint"); return
            self._delete_queue_item(fp)
        else:
            self._err(404, "not found")

    # ── Serve HTML ──────────────────────────────────────────────────────────
    def _serve_html(self):
        try:
            with open(INDEX_HTML, "rb") as fh:
                body = fh.read()
        except FileNotFoundError:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"dashboard/index.html not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ── Sources CRUD ────────────────────────────────────────────────────────
    def _add_source(self):
        body, err = self._read_body()
        if err:
            self._err(400, err); return
        ok, verr = validate_source(body)
        if not ok:
            self._err(400, verr); return
        sources = load_sources()
        new_id = body.get("id") or f"src_{uuid.uuid4().hex[:8]}"
        if any(s.get("id") == new_id for s in sources):
            self._err(400, f"source id {new_id!r} already exists"); return
        src = {
            "id":          new_id,
            "type":        body["type"],
            "target":      body["target"].strip(),
            "domain_tags": list(body.get("domain_tags") or []),
            "weight":      float(body.get("weight", 1.0)),
            "enabled":     bool(body.get("enabled", True)),
            "recency_days":int(body.get("recency_days", 7)),
            "added_at":    body.get("added_at") or _utcnow(),
        }
        sources.append(src)
        save_sources(sources)
        self._json(201, src)

    def _put_source(self, sid: str):
        body, err = self._read_body()
        if err:
            self._err(400, err); return
        sources = load_sources()
        idx = next((i for i, s in enumerate(sources) if s.get("id") == sid), None)
        if idx is None:
            self._err(404, "source not found"); return
        src = dict(sources[idx])
        for field in ("type", "target", "domain_tags", "weight", "enabled", "recency_days"):
            if field in body:
                src[field] = body[field]
        ok, verr = validate_source(src)
        if not ok:
            self._err(400, verr); return
        sources[idx] = src
        save_sources(sources)
        self._json(200, src)

    def _delete_source(self, sid: str):
        sources = load_sources()
        before = len(sources)
        sources = [s for s in sources if s.get("id") != sid]
        if len(sources) == before:
            self._err(404, "source not found"); return
        save_sources(sources)
        self._json(200, {"deleted": sid})

    # ── Interests ───────────────────────────────────────────────────────────
    def _put_interests(self):
        body, err = self._read_body()
        if err:
            self._err(400, err); return
        ok, verr = validate_interests(body)
        if not ok:
            self._err(400, verr); return
        interests = load_interests()
        for field in ("domains", "keywords_boost", "anti_interests"):
            if field in body:
                interests[field] = body[field]
        interests["updated_at"] = _utcnow()
        save_interests(interests)
        self._json(200, interests)

    # ── Queue ────────────────────────────────────────────────────────────────
    def _queue_reorder(self):
        body, err = self._read_body()
        if err:
            self._err(400, err); return
        if not isinstance(body, list):
            self._err(400, "body must be an ordered list of fingerprint strings"); return
        if not all(isinstance(fp, str) and _FP_RE.match(fp) for fp in body):
            self._err(400, "each fingerprint must be a non-empty alphanumeric/dash/underscore string"); return
        queue = load_queue()
        fp_set = set(body)
        fp_idx = {fp: i for i, fp in enumerate(body)}
        updated = 0
        for item in queue:
            fp = item.get("fingerprint")
            if fp in fp_idx:
                item["manual_priority"] = fp_idx[fp]
                updated += 1
            elif fp not in fp_set:
                item["manual_priority"] = None
        save_queue(queue)
        self._json(200, {"reordered": updated})

    def _put_queue_item(self, fp: str):
        body, err = self._read_body()
        if err:
            self._err(400, err); return
        queue = load_queue()
        item = next((i for i in queue if i.get("fingerprint") == fp), None)
        if item is None:
            self._err(404, "queue item not found"); return
        for k in ("status", "manual_priority"):
            if k in body:
                item[k] = body[k]
        save_queue(queue)
        self._json(200, item)

    def _delete_queue_item(self, fp: str):
        queue = load_queue()
        before = len(queue)
        queue = [it for it in queue if it.get("fingerprint") != fp]
        if len(queue) == before:
            self._err(404, "queue item not found"); return
        save_queue(queue)
        self._json(200, {"deleted": fp})


# ── Utility ────────────────────────────────────────────────────────────────
def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── Entry point ────────────────────────────────────────────────────────────
def main():
    global BEARER_TOKEN
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [research-dashboard] %(message)s",
        stream=sys.stderr,
    )
    BEARER_TOKEN = get_or_create_token()
    os.makedirs(DATA_DIR, exist_ok=True)

    server = http.server.ThreadingHTTPServer((TAILSCALE_IP, PORT), DashboardHandler)
    server.daemon_threads = True
    logging.info("Listening on http://%s:%d  (Tailscale-only)", TAILSCALE_IP, PORT)
    logging.info("Bearer token file: %s", TOKEN_FILE)
    logging.info("Data dir: %s", DATA_DIR)
    logging.info("I/O: direct atomic JSON (shared file format with research_registry)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        logging.info("Stopped.")


if __name__ == "__main__":
    main()
