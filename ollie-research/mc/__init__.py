"""
Ollie Mission Control — server-side package.

Holds the route REGISTRY plus generic stdlib helpers (io / auth / cache).
Handler modules (`reads`, `writes`) import this package and self-register
their endpoints; the HTTP server (`research_dashboard.py`) looks routes up
here instead of maintaining hand-written per-method dicts.

A "route" is a (METHOD, pattern) pair mapped to a handler function with the
signature `fn(handler, **path_params) -> None`, where `handler` is the live
`BaseHTTPRequestHandler` instance (so the fn can use its response helpers,
read the body, etc.). Patterns use `{name}` placeholders, e.g.
`/api/sources/{id}`; the captured value is validated against the registered
guard regex (if any) and passed as a keyword argument.
"""
import re
import threading

# ── Route registry ─────────────────────────────────────────────────────────
# Each entry: {"method", "pattern", "regex", "params", "guards", "fn"}
_ROUTES = []
_ROUTES_LOCK = threading.Lock()

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _compile(pattern: str):
    """Turn `/api/sources/{id}` into a regex + ordered param-name list."""
    params = []

    def _sub(m):
        params.append(m.group(1))
        # Greedy `.+` (not `[^/]+`) so a path like `/api/sources/../etc/passwd`
        # still MATCHES the route and is then rejected by the param guard with
        # HTTP 400 — preserving the legacy startswith()-then-validate behavior
        # (the old code 400'd traversal attempts; a 404 would be a regression).
        return r"(?P<%s>.+)" % m.group(1)

    regex_src = _PLACEHOLDER_RE.sub(_sub, re.escape(pattern).replace(r"\{", "{").replace(r"\}", "}"))
    return re.compile("^" + regex_src + "$"), params


def route(method: str, pattern: str, guards: dict | None = None):
    """Decorator: register `fn` for (method, pattern).

    `guards` maps a path-param name to a compiled regex (or pattern string);
    a captured value that fails its guard yields HTTP 400 before `fn` runs.
    """
    regex, params = _compile(pattern)
    compiled_guards = {}
    for name, g in (guards or {}).items():
        compiled_guards[name] = g if hasattr(g, "match") else re.compile(g)

    def _register(fn):
        with _ROUTES_LOCK:
            _ROUTES.append({
                "method": method.upper(),
                "pattern": pattern,
                "regex": regex,
                "params": params,
                "guards": compiled_guards,
                "fn": fn,
            })
        return fn

    return _register


# Sentinel return values from dispatch()
DISPATCH_NOT_FOUND = "not_found"
DISPATCH_BAD_PARAM = "bad_param"


def dispatch(handler, method: str, path: str) -> str | None:
    """Look up `path` for `method` and invoke the matching handler.

    Returns None on success (handler wrote the response), or a sentinel
    string (`DISPATCH_NOT_FOUND` / `DISPATCH_BAD_PARAM`) so the caller can
    emit the right error. On a bad path-param the matched route's guard
    failed; the dispatcher reports it rather than the handler.
    """
    method = method.upper()
    matched_path = False
    for r in _ROUTES:
        if r["method"] != method:
            continue
        m = r["regex"].match(path)
        if not m:
            continue
        matched_path = True
        params = m.groupdict()
        for name, value in params.items():
            guard = r["guards"].get(name)
            if guard is not None and not guard.match(value):
                return DISPATCH_BAD_PARAM
        r["fn"](handler, **params)
        return None
    # No (method, path) matched.
    return DISPATCH_NOT_FOUND


def load_handlers():
    """Import the handler modules so their @route decorators register.

    Kept lazy + idempotent: importing a module twice is a no-op in CPython,
    and the @route side effects only run on first import.

    Auto-discovery (the DROP-IN PANEL CONTRACT): besides the core `reads` /
    `writes` modules, ANY `mc/reads_*.py` or `mc/controls*.py` file is imported
    automatically via glob. A new panel agent can drop `mc/reads_jobs.py` in
    place and its @route endpoints register with NO edit to this file.
    """
    import glob
    import importlib
    import os

    from . import reads   # noqa: F401
    from . import writes  # noqa: F401

    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    discovered = set()
    for pattern in ("reads_*.py", "controls*.py"):
        for path in sorted(glob.glob(os.path.join(pkg_dir, pattern))):
            mod = os.path.splitext(os.path.basename(path))[0]
            discovered.add(mod)
    for mod in sorted(discovered):
        # Import-once: re-importing is a no-op, but guard import errors so one
        # broken panel module can't sink the whole dashboard at boot.
        try:
            importlib.import_module(f".{mod}", __name__)
        except Exception:  # pragma: no cover - defensive; logged, not fatal
            import logging
            logging.exception("Mission Control: failed to load handler module %r", mod)
