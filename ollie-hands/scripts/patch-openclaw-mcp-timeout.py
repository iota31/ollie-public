"""Extend the OpenClaw MCP request timeout for the Hands server only.

The OpenClaw agent bundle's ``callTool`` invocation omits request options, so
every MCP tool call inherits the MCP SDK's 60-second default request timeout. A
Hands tool call can legitimately block for the full 180-second owner-confirmation
window, so the SDK aborts the request before the owner can approve it.

This patch rewrites the bundled invocation to pass ``{ timeout: 240_000 }`` *only*
when ``serverName === "hands"``. Every other MCP server (factcheck, brave-search,
openroute, ...) keeps the SDK default; nothing else changes.

Keep it explicit and fail closed: an upstream bundle change must be reviewed
instead of silently going unpatched. Run against the installed OpenClaw dist:

    python patch-openclaw-mcp-timeout.py \
        /home/openclaw/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist
"""

from __future__ import annotations

import sys
from pathlib import Path


# The dist bundle is minified with tab indentation. Match the exact invocation
# (whitespace included) so a shape change fails loudly rather than mis-patching.
UNPATCHED = (
    "return await session.client.callTool({\n"
    "\t\t\t\tname: toolName,\n"
    "\t\t\t\targuments: isMcpConfigRecord(input) ? input : {}\n"
    "\t\t\t});"
)

# Only the Hands server gets the extended request timeout; everything else keeps
# the SDK default. 240_000ms > the 180s owner-confirmation window with headroom.
PATCHED = (
    "return await session.client.callTool({\n"
    "\t\t\t\tname: toolName,\n"
    "\t\t\t\targuments: isMcpConfigRecord(input) ? input : {}\n"
    '\t\t\t}, void 0, serverName === "hands" ? { timeout: 240_000 } : void 0);'
)

BUNDLE_GLOB = "agent-bundle-mcp-runtime-*.js"


def patch_file(bundle: Path) -> bool:
    """Patch one bundle in place. Return True if it wrote a change.

    Fail closed: raise if the bundle is neither already-patched nor an exact
    single-occurrence match for the known unpatched shape.
    """
    source = bundle.read_text(encoding="utf-8")
    unpatched_count = source.count(UNPATCHED)
    patched_count = source.count(PATCHED)

    if unpatched_count == 0 and patched_count == 1:
        print(f"already patched: {bundle}")
        return False
    if unpatched_count != 1:
        raise SystemExit(
            f"unexpected OpenClaw MCP bundle shape in {bundle}: "
            f"unpatched={unpatched_count}, patched={patched_count}"
        )

    bundle.write_text(source.replace(UNPATCHED, PATCHED, 1), encoding="utf-8", newline="")
    print(f"patched Hands MCP callTool timeout: {bundle}")
    return True


def main(argv: list[str]) -> None:
    if len(argv) != 2:
        raise SystemExit(
            "usage: patch-openclaw-mcp-timeout.py <openclaw-dist-dir-or-bundle.js>"
        )
    target = Path(argv[1])
    if target.is_file():
        bundles = [target]
    else:
        bundles = sorted(target.glob(BUNDLE_GLOB))
    if not bundles:
        raise SystemExit(f"no {BUNDLE_GLOB} found under {target}")

    changed = 0
    for bundle in bundles:
        changed += int(patch_file(bundle))
    print(f"done: {changed} of {len(bundles)} bundle(s) changed")


if __name__ == "__main__":
    main(sys.argv)
