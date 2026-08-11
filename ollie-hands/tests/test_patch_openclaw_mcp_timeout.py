"""Focused tests for the Hands-only OpenClaw MCP callTool timeout patch.

The fixture reproduces the exact invocation shape found in the deployed bundle
``agent-bundle-mcp-runtime-n24dxm4C.js`` (tab-indented, minified), so these tests
double as a shape-drift tripwire before the patch is applied to the live bundle.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest


SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
PATCHER_PATH = SCRIPTS / "patch-openclaw-mcp-timeout.py"


def _load_patcher():
    spec = importlib.util.spec_from_file_location("patch_openclaw_mcp_timeout", PATCHER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


patcher = _load_patcher()


# Exact block from the live bundle (see git blame / box diff). Surrounding lines
# stand in for the rest of the minified file.
LIVE_BLOCK = (
    "\t\tasync callTool(serverName, toolName, input) {\n"
    "\t\t\tfailIfDisposed();\n"
    "\t\t\tawait getCatalog();\n"
    "\t\t\tconst session = sessions.get(serverName);\n"
    '\t\t\tif (!session) throw new Error(`bundle-mcp server "${serverName}" is not connected`);\n'
    "\t\t\treturn await session.client.callTool({\n"
    "\t\t\t\tname: toolName,\n"
    "\t\t\t\targuments: isMcpConfigRecord(input) ? input : {}\n"
    "\t\t\t});\n"
    "\t\t},\n"
)


def _bundle(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    f = tmp_path / "agent-bundle-mcp-runtime-n24dxm4C.js"
    f.write_text(body, encoding="utf-8", newline="")
    return f


def test_patch_targets_only_the_hands_server(tmp_path):
    bundle = _bundle(tmp_path, LIVE_BLOCK)
    assert patcher.patch_file(bundle) is True
    patched = bundle.read_text(encoding="utf-8")

    # The extended timeout is applied, and gated on the Hands server name so no
    # other MCP server's request timeout changes.
    assert "timeout: 240_000" in patched
    assert 'serverName === "hands" ? { timeout: 240_000 } : void 0' in patched
    # The tool name / arguments payload is untouched.
    assert "arguments: isMcpConfigRecord(input) ? input : {}" in patched


def test_timeout_exceeds_confirm_window():
    # The 240_000ms request timeout must clear the 180s owner-confirm window.
    config = (SCRIPTS.parent / "ollie_hands" / "config.py").read_text(encoding="utf-8")
    import re

    confirm_timeout = int(re.search(r"confirm_timeout:\s*int\s*=\s*(\d+)", config).group(1))
    assert "240_000" in patcher.PATCHED
    assert 240 > confirm_timeout


def test_patch_is_idempotent(tmp_path):
    bundle = _bundle(tmp_path, LIVE_BLOCK)
    assert patcher.patch_file(bundle) is True
    first = bundle.read_text(encoding="utf-8")
    # Second run is a no-op and does not double-apply.
    assert patcher.patch_file(bundle) is False
    assert bundle.read_text(encoding="utf-8") == first
    assert first.count("timeout: 240_000") == 1


def test_patch_fails_closed_on_unknown_shape(tmp_path):
    drifted = LIVE_BLOCK.replace("session.client.callTool", "session.client.invokeTool")
    bundle = _bundle(tmp_path, drifted)
    with pytest.raises(SystemExit):
        patcher.patch_file(bundle)
    # Fail closed: nothing was written.
    assert bundle.read_text(encoding="utf-8") == drifted


def test_patched_bundle_leaves_non_hands_calls_on_default(tmp_path):
    bundle = _bundle(tmp_path, LIVE_BLOCK)
    patcher.patch_file(bundle)
    patched = bundle.read_text(encoding="utf-8")
    # Exactly one options object is injected, guarded by the Hands discriminator.
    assert patched.count("{ timeout: 240_000 }") == 1
    assert patched.count('serverName === "hands"') == 1
