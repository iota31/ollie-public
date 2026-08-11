"""Apply the narrow Camoufox/Playwright Firefox page-error compatibility fix.

Camoufox can emit a Firefox PageError without ``location``. Playwright 1.60.0
unconditionally dereferences that optional object in two bundled dispatchers,
which kills the Node driver process. Keep this patch explicit and fail closed:
an upstream bundle change must be reviewed instead of silently going unpatched.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


REPLACEMENTS = {
    "pageError.location.url": "pageError.location?.url || \"\"",
    "pageError.location.lineNumber": "pageError.location?.lineNumber || 0",
    "pageError.location.columnNumber": "pageError.location?.columnNumber || 0",
}


def main() -> None:
    spec = importlib.util.find_spec("playwright")
    if spec is None or not spec.submodule_search_locations:
        raise SystemExit("playwright package not found")
    bundle = Path(next(iter(spec.submodule_search_locations))) / (
        "driver/package/lib/coreBundle.js"
    )
    source = bundle.read_text(encoding="utf-8")
    changed = source
    for unsafe, safe in REPLACEMENTS.items():
        unsafe_count = changed.count(unsafe)
        safe_count = changed.count(safe)
        if unsafe_count == 0 and safe_count >= 2:
            continue  # idempotent reinstall/deploy
        if unsafe_count != 2:
            raise SystemExit(
                f"unexpected Playwright bundle shape for {unsafe!r}: "
                f"unsafe={unsafe_count}, patched={safe_count}"
            )
        changed = changed.replace(unsafe, safe)
    if changed != source:
        bundle.write_text(changed, encoding="utf-8", newline="\n")
        print(f"patched Playwright Firefox page-error handling: {bundle}")
    else:
        print(f"Playwright Firefox page-error handling already patched: {bundle}")


if __name__ == "__main__":
    main()
