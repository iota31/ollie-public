#!/usr/bin/env python3
"""audit-verify — check the tamper-evident audit chain (plan Track D2).

Reproducible OFF the box: point it at a (read-only) copy of the audit dir synced
elsewhere, and a host compromise that rewrote/deleted history is detectable from
a machine the attacker doesn't control.

    python3 scripts/audit-verify.py <audit_dir>

Exit 0 = chain intact; 1 = a break (the exact file/line/id is reported); 2 = bad
usage. Designed with zero third-party deps so it runs anywhere Python does.
"""

from __future__ import annotations

import sys
from pathlib import Path

# allow running from anywhere without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ollie_hands.audit import verify_chain  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: audit-verify.py <audit_dir>")
        return 2
    res = verify_chain(sys.argv[1])
    files = ", ".join(res["files"]) or "(none)"
    print(f"files:   {files}")
    print(f"chained: {res['chained']} records   legacy(pre-chain): {res['legacy']}")
    if res["ok"]:
        print("RESULT:  OK — chain intact, nothing altered/reordered/deleted")
        return 0
    b = res["break"]
    print(f"RESULT:  BREAK in {b['file']} line {b['line']} "
          f"(id={b['id']}): {b['reason']}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
