#!/usr/bin/env python3
"""Ollie voice: text -> WhatsApp-ready OGG/Opus voice note.

Engine chain (2026-06-11): MiMo v2.5 TTS (cloud, free month, natural voice)
-> Kokoro-82M on-device fallback (am_michael, speed 1.1) so Ollie keeps his
voice even offline. A pronunciation lexicon (lexicon.json, same dir)
respells names for the Kokoro path; MiMo handles names natively.
MiMo API shape: POST /v1/chat/completions, model mimo-v2.5-tts, the text to
speak goes in an ASSISTANT-role message, audio comes back base64 in
choices[0].message.audio.data.

Usage:
    tts_say.py --out /path/reply.ogg [--text "..."] [--speed 1.1]
    echo "text" | tts_say.py --out /path/reply.ogg

Reads text from --text or stdin. Prints the output path on success, exits
non-zero on failure. Designed to be spawned by the WhatsApp plugin and the
jobs runner; serialization/queueing is the caller's job.

Env (set by caller or defaults below):
    OLLIE_TTS_HOME   dir with venv, model + voices files (default ~/tts)
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HOME = os.path.expanduser(os.environ.get("OLLIE_TTS_HOME", "~/tts"))
MODEL = os.path.join(HOME, "kokoro-v1.0.int8.onnx")
VOICES = os.path.join(HOME, "voices-v1.0.bin")
FFMPEG = (
    os.environ.get("OLLIE_FFMPEG")
    or shutil.which("ffmpeg")
    or os.path.expanduser("~/bin/ffmpeg")
)


def setup_espeak():
    """Point phonemizer at the pip-bundled espeak-ng lib + data (the bundled
    lib has a CI build path baked in, so the data path MUST be set)."""
    try:
        import espeakng_loader
        os.environ.setdefault("PHONEMIZER_ESPEAK_LIBRARY", espeakng_loader.get_library_path())
        os.environ.setdefault("ESPEAK_DATA_PATH", espeakng_loader.get_data_path())
    except Exception:
        pass
LEXICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lexicon.json")
VOICE = "am_onyx"  # owner pick 2026-06-26 (Kokoro is now primary)
DEFAULT_SPEED = 1.1
MAX_CHARS = 900  # longer than this is an essay, not a voice note

MIMO_KEY_FILE = "/home/openclaw/.openclaw/secrets/mimo-lab.key"
MIMO_BASE = "https://token-plan-sgp.xiaomimimo.com/v1"
MIMO_VOICE = os.environ.get("OLLIE_MIMO_VOICE", "Dean")


def _tts_target():
    """(endpoint, model, key) for the cloud TTS leg.

    Prefers the central router's "tts" route (one place to tune the model);
    falls back to the hardcoded MiMo constants if the router or its key is
    unavailable, so TTS never silently degrades to Kokoro just because the
    router isn't deployed. Returns None only when no key can be found at all.
    """
    rd = os.environ.get("OLLIE_ROUTER_DIR", "/home/openclaw/.openclaw/ollie-router")
    if rd not in sys.path:
        sys.path.insert(0, rd)
    try:
        import ollie_router
        ms = ollie_router.resolve("tts")
        if ms and ms[0].api_key:
            return (ms[0].endpoint, ms[0].model, ms[0].api_key)
    except Exception:  # noqa: BLE001 — router missing/broken -> hardcoded fallback
        pass
    try:
        return (MIMO_BASE, "mimo-v2.5-tts", open(MIMO_KEY_FILE).read().strip())
    except OSError:
        return None


def mimo_tts(text):
    """Cloud TTS (router "tts" route) -> mp3 bytes, or None on any failure."""
    import base64
    import urllib.request
    tgt = _tts_target()
    if not tgt:
        return None
    base, model, key = tgt
    body = json.dumps({
        "model": model,
        "modalities": ["text", "audio"],
        "audio": {"voice": MIMO_VOICE, "format": "mp3"},
        "messages": [{"role": "assistant", "content": text}],
    }).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions", data=body, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "User-Agent": "curl/8.5.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            d = json.loads(r.read().decode())
        data = (d.get("choices") or [{}])[0].get("message", {}).get("audio", {}).get("data")
        return base64.b64decode(data) if data else None
    except Exception:  # noqa: BLE001
        return None


def load_lexicon():
    try:
        d = json.load(open(LEXICON))
        return {k: v for k, v in d.items() if not k.startswith("_") and isinstance(v, str)}
    except Exception:
        return {}


def apply_lexicon(text, lex):
    for k, v in lex.items():
        text = re.sub(rf"\b{re.escape(k)}\b", v, text)
        text = re.sub(rf"\b{re.escape(k)}\b", v, text, flags=re.IGNORECASE)
    return text


def clean_for_speech(text):
    """Strip chat artifacts that sound wrong when read aloud."""
    text = re.sub(r"https?://\S+", "the link in chat", text)
    text = re.sub(r"[*_`#>]+", "", text)          # markdown
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]", "", text)  # emoji
    text = re.sub(r"\s+", " ", text).strip()
    return text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--speed", type=float, default=DEFAULT_SPEED)
    args = ap.parse_args()

    text = args.text if args.text is not None else sys.stdin.read()
    text = clean_for_speech(text)
    if not text:
        print("empty text", file=sys.stderr)
        return 2
    if len(text) > MAX_CHARS:
        print(f"text too long for a voice note ({len(text)} > {MAX_CHARS})", file=sys.stderr)
        return 3

    # Primary: on-device Kokoro (local-first; voice = VOICE, owner pick am_onyx).
    # MiMo cloud is the fallback when Kokoro is unavailable.
    src_path, suffix = None, None
    try:
        ktext = apply_lexicon(text, load_lexicon())  # lexicon respellings for names
        setup_espeak()
        from kokoro_onnx import Kokoro
        import soundfile as sf
        k = Kokoro(MODEL, VOICES)
        audio, sr = k.create(ktext, voice=VOICE, speed=args.speed)
        suffix = ".wav"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            src_path = tmp.name
        sf.write(src_path, audio, sr)
    except Exception as e:  # noqa: BLE001 — kokoro failed -> cloud fallback
        print(f"kokoro tts unavailable ({e}) -> mimo fallback", file=sys.stderr)
        mp3 = mimo_tts(text)
        if mp3:
            suffix = ".mp3"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(mp3)
                src_path = tmp.name
    if not src_path:
        print("tts failed on both kokoro and mimo", file=sys.stderr)
        return 4

    try:
        # WhatsApp voice notes (rendered with the mic bubble) must be OGG/Opus.
        r = subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error", "-i", src_path,
             "-c:a", "libopus", "-b:a", "32k", "-ar", "48000", "-ac", "1",
             "-application", "voip", args.out],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            print(f"ffmpeg failed: {r.stderr[:300]}", file=sys.stderr)
            return 4
    finally:
        try:
            os.unlink(src_path)
        except OSError:
            pass
    print(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
