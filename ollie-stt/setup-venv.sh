#!/usr/bin/env bash
# setup-venv.sh — create/refresh the ollie-stt shared uv venv.
#
# RUN BY THE INTEGRATOR AT DEPLOY, NOT NOW. This installs the local
# STT runner's one heavy dep (faster-whisper, which pulls ctranslate2 +
# tokenizers) and pre-downloads the "small" int8 model into the
# shared HF cache. The box has NO docker/podman and a stdlib-only
# system python (see Plans/curious-foraging-magpie.md RECON), so a uv
# venv is the ONLY install route. Idempotent: safe to re-run.
#
# ┌─ WATCHDOG ALLOWLIST — DO THIS FIRST ──────────────────────────────────┐
# │ The lab-bypass watchdog (ollie_watchdog.py) alarms on new venvs /    │
# │ large ~/.cache trees outside the lab (built to catch the supertonic   │
# │ leak). BEFORE running this script the integrator MUST allowlist:     │
# │   - /home/openclaw/ollie-stt/.venv                                     │
# │   - faster-whisper's HF model cache (~/.cache/huggingface)            │
# │ Both are added to ollie_watchdog.py CACHE_ALLOWLIST / venv baseline   │
# │ in this commit, so the install should NOT self-alarm on our own      │
# │ legit footprint.                                                     │
# └─────────────────────────────────────────────────────────────────────────┘

set -euo pipefail

OLLIE_HOME="${OLLIE_HOME:-/home/openclaw}"
STT_DIR="${STT_DIR:-${OLLIE_HOME}/ollie-stt}"
VENV="${STT_DIR}/.venv"
UV="${UV:-${OLLIE_HOME}/.local/bin/uv}"
PY_VERSION="3.12"
REQ="${STT_DIR}/requirements.txt"

echo "== ollie-stt venv setup =="
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

# --- pre-download the small model (~464MB) --------------------------------
# This makes the first inbound voice note decode immediately rather than
# racing a download against the WhatsApp 24h inbound window. The HF_HOME
# default ($HOME/.cache/huggingface) is the path allowlisted in the
# watchdog so this download does NOT self-alarm.
echo "pre-downloading faster-whisper small model..."
"${VPY}" - <<'PYEOF' || echo "  (pre-download non-fatal — model will lazy-load on first call)"
from faster_whisper import WhisperModel
m = WhisperModel("small", device="cpu", compute_type="int8")
print(f"  loaded: {m.model.is_multilingual} multilingual, {m.model.n_text_state} dim")
PYEOF

# --- versions report -------------------------------------------------------
echo "== installed versions =="
"${VPY}" - <<'PYEOF'
import importlib.metadata as md
for pkg in ("faster_whisper", "ctranslate2", "tokenizers"):
    try:
        print(f"  {pkg}=={md.version(pkg)}")
    except Exception as e:
        print(f"  {pkg}: NOT INSTALLED ({e})")
PYEOF

echo "== done. venv ready at ${VENV} =="
echo "   run the runner with: ${VPY} ${STT_DIR}/stt_transcribe.py --in /path/voice.ogg"
