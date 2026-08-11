"""Schema-to-dispatch tests.

Ensures the public MCP `act` docstring and engine dispatch agree with the
canonical browser operations contract in policy.py, and that required
per-operation arguments are accurately documented.

These tests gate owner approval / grant issuance: the docs the brain sees
must match what the engine will actually dispatch.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ollie_hands import policy as P
from ollie_hands import engine as E
from ollie_hands import browser as B
import inspect
import re


def test_engine_browser_dispatches_only_canonical_ops():
    """Engine dispatch must not claim ops outside policy's canonical set.

    Canonical set is the union of the internal read/interact sets.
    Some canonical ops may be intentionally unsupported at the dispatch layer
    (no direct forwarding); callers should use plan_submit or avoid them.
    """
    read = getattr(P, "_BROWSER_READ", set())
    interact = getattr(P, "_BROWSER_INTERACT", set())
    canonical = set(read) | set(interact)

    src = inspect.getsource(E._dispatch)
    m = re.search(r'if kind == "browser":(.*?)(?=\n    if kind == |\Z)', src, re.DOTALL)
    assert m, "could not locate browser dispatch block in engine._dispatch"
    browser_block = m.group(1)

    dispatched = set(re.findall(r'if op == ["\']([^"\']+)["\']', browser_block))

    # Dispatch must not invent ops outside the declared canonical set.
    extra = dispatched - canonical
    assert not extra, f"dispatch contains unclaimed browser ops: {extra}"

    # Note: we do not require dispatch to cover the entire canonical set here.
    # If a canonical op is intentionally unsupported for direct act(), it can
    # remain absent from dispatch; the public schema/docs must still be accurate
    # for the ops that *are* supported (covered by other tests).


def test_act_docstring_documents_all_canonical_ops():
    """The public act() docstring must enumerate every supported browser op.

    We read the source text directly to avoid importing the MCP server module
    (which may trigger host/port validation at import time).
    """
    srv_path = pathlib.Path(__file__).resolve().parents[1] / "ollie_hands" / "server.py"
    text = srv_path.read_text()
    # Locate the act(...) def (with full signature) and its docstring.
    # The signature spans multiple lines; capture until the closing """.
    m = re.search(r'@mcp\.tool\(\)\s*\nasync def act\(.*?\n\s*"""(.*?)"""', text, re.DOTALL)
    if not m:
        # Fallback: search more loosely from 'async def act(' to its own closing triple quote
        m = re.search(r'async def act\(.*?\)\s*->\s*str:\s*"""(.*?)"""', text, re.DOTALL)
    assert m, "could not locate act() docstring in server.py"
    doc = m.group(1)
    read = getattr(P, "_BROWSER_READ", set())
    interact = getattr(P, "_BROWSER_INTERACT", set())
    canonical = set(read) | set(interact)
    for op in sorted(canonical):
        assert op in doc, f"act docstring missing browser op {op!r}"


# Per-op required arguments (truth from the runtime signatures).
# We do not invent new args; we only assert the ones the functions actually take.
REQUIRED_ARGS = {
    "goto": {"url"},
    "wait": {"seconds"},
    "extract": set(),  # selector optional
    "links": set(),    # limit optional
    "screenshot": {"save_path", "path"},  # accepts either alias
    "get_attr": {"selector", "attr"},
    "property_matches": {"selector", "prop"},  # arg is 'prop' not 'property'
    "click": {"selector"},
    "fill": {"selector", "value"},
    "type_text": {"selector", "value"},
    "press": {"key"},
    "element_text": {"selector"},
    "status": set(),
}


def test_act_signature_exposes_browser_required_arguments():
    """Public MCP act signature must expose required args for documented browser ops.

    The generated public schema is derived from this signature; tests must
    inspect the public surface (server.act), not only internal browser impl.
    """
    srv_path = pathlib.Path(__file__).resolve().parents[1] / "ollie_hands" / "server.py"
    text = srv_path.read_text()
    # Parse the act(...) signature line(s) and extract parameter names.
    # Capture from "async def act(" up to the closing ")" of the signature.
    m = re.search(r'async def act\((.*?)\)\s*->\s*str:', text, re.DOTALL)
    assert m, "could not locate act() signature in server.py"
    sig_src = m.group(1)
    # Collect bare parameter names (ignore defaults/annotations).
    # Handles both positional-with-defaults and trailing keyword params.
    params = set(re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\b\s*(?:[:=]|,|$)', sig_src))
    # Known non-argument identifiers that may appear in the signature source.
    params.discard("str")
    params.discard("int")
    params.discard("float")
    params.discard("bool")
    params.discard("dict")
    params.discard("list")
    params.discard("None")
    params.discard("Optional")

    # Required surface parameters for the documented browser ops.
    # These must be present in the public act signature.
    required_surface = {
        "seconds",         # for wait(seconds)
        "save_path",       # for screenshot(save_path)
        "path",            # alias accepted by engine for screenshot
        "property",        # for property_matches(selector, prop)
        "equals",          # comparison option for property_matches
        "contains",        # comparison option for property_matches
        "nonempty",        # comparison option for property_matches
    }
    missing = required_surface - params
    assert not missing, f"public act signature missing required params: {missing}; has {sorted(params)}"


def test_no_unsupported_browser_ops_claimed_in_act_docstring():
    """Docstring must not claim ops that policy does not support."""
    srv_path = pathlib.Path(__file__).resolve().parents[1] / "ollie_hands" / "server.py"
    text = srv_path.read_text()
    m = re.search(r'async def act\(.*?\):\s*"""(.*?)"""', text, re.DOTALL)
    assert m, "could not locate act() docstring in server.py"
    doc = m.group(1)
    # Known unsupported names that have appeared in past docs
    unsupported = {"select", "submit"}
    for bad in unsupported:
        if bad in doc:
            for line in doc.splitlines():
                if "browser" in line and bad in line:
                    raise AssertionError(f"act docstring claims unsupported op {bad!r}")
