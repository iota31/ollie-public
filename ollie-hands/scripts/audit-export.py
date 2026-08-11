r"""audit-export — emit the audit JSONL chain as base64(tar.gz) on stdout.

Half of the hourly off-box sync (plan Track D2). Runs on the WINDOWS HOST (it can
read the ACL-locked audit dir; the isolated WSL gateway cannot). The output is
base64 — pipe-safe — so it crosses host -> WSL over `wsl` stdin WITHOUT a
filesystem bridge, preserving the gateway's deliberate isolation from C:. The WSL
side (`ollie-hands-audit-sync.sh`) age-encrypts and pushes it off-box.

Only the integrity-critical *.jsonl chain is exported (not screenshots).

    python audit-export.py            # uses C:\ProgramData\ollie-hands\audit
    set OLLIE_AUDIT_DIR=... & python audit-export.py
"""

from __future__ import annotations

import base64
import glob
import io
import os
import sys
import tarfile

AUDIT_DIR = os.environ.get("OLLIE_AUDIT_DIR", r"C:\ProgramData\ollie-hands\audit")


def main() -> int:
    files = sorted(glob.glob(os.path.join(AUDIT_DIR, "audit-*.jsonl")))
    if not files:
        sys.stderr.write(f"no audit-*.jsonl in {AUDIT_DIR}\n")
        return 1
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for f in files:
            tf.add(f, arcname=os.path.basename(f))
    # write raw base64 to stdout (no newline games); the receiver tolerates ws
    sys.stdout.write(base64.b64encode(buf.getvalue()).decode("ascii"))
    sys.stdout.flush()
    sys.stderr.write(f"exported {len(files)} audit files\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
