#!/usr/bin/env bash
# setup-venv.sh — create/refresh the ollie-research shared uv venv.
#
# RUN BY THE INTEGRATOR AT DEPLOY, NOT NOW. This installs the Curiosity
# Engine's two heavy deps (crawl4ai + fastembed) plus a headless chromium for
# crawl4ai, into one dedicated uv venv. The box has NO docker/podman and a
# stdlib-only system python (see Plans/curious-foraging-magpie.md RECON), so a
# uv venv is the ONLY install route. Idempotent: safe to re-run.
#
# ┌─ WATCHDOG ALLOWLIST — DO THIS FIRST ──────────────────────────────────┐
# │ The lab-bypass watchdog (ollie_watchdog.py) alarms on new venvs / large │
# │ ~/.cache trees outside the lab (built to catch the supertonic leak).    │
# │ BEFORE running this script the integrator MUST allowlist:               │
# │   - /home/openclaw/ollie-research/.venv                                 │
# │   - the playwright browser cache (~/.cache/ms-playwright)               │
# │   - crawl4ai's cache (~/.cache/crawl4ai, ~/.crawl4ai)                    │
# │ Add them to ollie_watchdog.py CACHE_ALLOWLIST / venv baseline, or the   │
# │ install self-alarms on our own legit footprint.                         │
# └─────────────────────────────────────────────────────────────────────────┘

set -euo pipefail

OLLIE_HOME="${OLLIE_HOME:-/home/openclaw}"
RESEARCH_DIR="${RESEARCH_DIR:-${OLLIE_HOME}/ollie-research}"
VENV="${RESEARCH_DIR}/.venv"
UV="${UV:-${OLLIE_HOME}/.local/bin/uv}"
PY_VERSION="3.12"
REQ="${RESEARCH_DIR}/requirements.txt"

echo "== ollie-research venv setup =="
echo "  uv:       ${UV}"
echo "  venv:     ${VENV}"
echo "  python:   ${PY_VERSION}"
echo "  reqs:     ${REQ}"

# --- preflight -------------------------------------------------------------
if [ ! -x "${UV}" ]; then
  echo "FATAL: uv not found/executable at ${UV}" >&2
  exit 1
fi
if [ ! -f "${REQ}" ]; then
  echo "FATAL: requirements.txt not found at ${REQ}" >&2
  exit 1
fi

# --- create venv (idempotent) ---------------------------------------------
if [ -x "${VENV}/bin/python" ]; then
  echo "venv already exists, reusing: ${VENV}"
else
  echo "creating venv with python ${PY_VERSION}..."
  "${UV}" venv --python "${PY_VERSION}" "${VENV}"
fi

VPY="${VENV}/bin/python"

# --- install deps (idempotent; uv resolves/skips already-satisfied) --------
echo "installing deps from requirements.txt..."
"${UV}" pip install --python "${VPY}" -r "${REQ}"

# --- playwright chromium + system deps (idempotent) ------------------------
# crawl4ai drives chromium via playwright. `playwright install chromium` is a
# no-op if the browser is already present. Headless Linux chromium in WSL.
echo "installing playwright chromium..."
"${VPY}" -m playwright install chromium

# System libs chromium needs (libnspr4, libnss3, …). These genuinely matter —
# without them chrome exits 127 "error while loading shared libraries". They
# require root: `playwright install-deps` shells out to `sudo apt-get`, which
# HANGS forever waiting for a password under non-interactive SSH (openclaw has
# no passwordless sudo). So: only attempt it when we are root or have
# passwordless sudo; otherwise print the exact root command and move on (never
# hang). On this box, run the deps step as root via WSL:
#   wsl -d OpenClawGateway -u root -- \
#     bash -lc 'DEBIAN_FRONTEND=noninteractive \
#       /home/openclaw/ollie-research/.venv/bin/python -m playwright install-deps chromium'
echo "installing playwright system deps..."
if [ "$(id -u)" = "0" ] || sudo -n true 2>/dev/null; then
  DEBIAN_FRONTEND=noninteractive "${VPY}" -m playwright install-deps chromium || \
    echo "  (install-deps failed — install libnspr4/libnss3/… as root, see comment above)"
else
  echo "  (not root + no passwordless sudo — SKIPPING to avoid a sudo-password hang."
  echo "   Chromium WILL fail to launch until the libs are installed as root; run:"
  echo "   wsl -d OpenClawGateway -u root -- bash -lc 'DEBIAN_FRONTEND=noninteractive ${VPY} -m playwright install-deps chromium')"
fi

# --- crawl4ai post-install setup (idempotent if present) -------------------
# Newer crawl4ai ships a `crawl4ai-setup` entrypoint that finalizes the
# browser/runtime. Run it if available; harmless to re-run.
if [ -x "${VENV}/bin/crawl4ai-setup" ]; then
  echo "running crawl4ai-setup..."
  "${VENV}/bin/crawl4ai-setup" || echo "  (crawl4ai-setup non-fatal failure)"
fi

# --- versions report -------------------------------------------------------
echo "== installed versions =="
"${VPY}" - <<'PYEOF'
import importlib.metadata as md
for pkg in ("crawl4ai", "fastembed"):
    try:
        print(f"  {pkg}=={md.version(pkg)}")
    except Exception as e:
        print(f"  {pkg}: NOT INSTALLED ({e})")
PYEOF
"${VPY}" -m playwright --version 2>/dev/null | sed 's/^/  /' || true

echo "== done. venv ready at ${VENV} =="
echo "   run the client with: ${VPY} ${RESEARCH_DIR}/research_crawl.py <URL>"
