#!/usr/bin/env python3
"""Ollie STT: voice note -> text.

Local faster-whisper-small (int8, CPU) replacement for the cloud Groq
whisper-large-v3-turbo path. Drops the cloud dependency (Groq 429 was
breaking inbound voice on both WhatsApp + Telegram). Lab A/B
(2026-06-20): faster-whisper-small = 0% WER on the test clip, 464MB,
RTFx ~6 on the box's CPU. Model is loaded lazily on first call so
spawn latency is the import + first-segment decode, not the full
464MB load on every start.

Usage:
    stt_transcribe.py --in /path/voice.ogg [--out /path/transcript.txt]
    stt_transcribe.py --in voice.ogg [--beam-size 5] [--language en]

Reads the audio file, runs WhisperModel("small", device="cpu",
compute_type="int8") with beam_size=5 by default, writes the joined
segment text to --out (or stdout). Exits non-zero on any failure.
Designed to be spawned by the OpenClaw local-STT plugin
(extensions/stt-local-cli) and from the jobs runner.

Env (set by caller or defaults below):
    OLLIE_STT_HOME   dir with venv (the venv lives next to this script
                     under .venv; the model cache is shared via HF_HOME
                     under ~/.cache, allowlisted in ollie_watchdog.py)
"""
import argparse
import os
import sys


# Resolve the venv python if we're invoked directly without "source .venv/bin/activate".
# Mirrors the pattern used by tts_say.py on the box.
HERE = os.path.dirname(os.path.abspath(__file__))
_VENV_PY = os.path.join(HERE, ".venv", "bin", "python")
if os.path.exists(_VENV_PY) and os.path.realpath(sys.executable) != os.path.realpath(_VENV_PY):
    # Re-exec self under the venv python so faster-whisper is importable
    # regardless of how the caller invoked us.
    try:
        os.execv(_VENV_PY, [_VENV_PY] + sys.argv)
    except OSError:
        pass  # fall through; we'll fail loudly on import below if deps missing

from faster_whisper import WhisperModel  # noqa: E402  (after venv re-exec)

DEFAULT_MODEL = os.environ.get("OLLIE_STT_MODEL", "small")
DEFAULT_BEAM = 5
DEFAULT_DEVICE = "cpu"
DEFAULT_COMPUTE_TYPE = "int8"
SUPPORTED_EXTS = (".ogg", ".opus", ".wav", ".mp3", ".m4a", ".flac")

_model_cache = {}


def get_model(model_name=DEFAULT_MODEL, device=DEFAULT_DEVICE,
              compute_type=DEFAULT_COMPUTE_TYPE):
    """Lazy + cached: the first call downloads + loads (~464MB, a few seconds);
    subsequent calls reuse the in-process WhisperModel."""
    key = (model_name, device, compute_type)
    m = _model_cache.get(key)
    if m is not None:
        return m
    m = WhisperModel(model_name, device=device, compute_type=compute_type)
    _model_cache[key] = m
    return m


def transcribe(audio_path, *, model_name=DEFAULT_MODEL, beam_size=DEFAULT_BEAM,
               language=None, vad_filter=True):
    """Run faster-whisper on `audio_path`. Returns (text, info_dict) where
    text is the joined segment text and info_dict has 'language', 'duration',
    'segments' (count) — enough for the caller to log without dumping the
    full segment list."""
    if not os.path.isfile(audio_path):
        raise FileNotFoundError(audio_path)
    if not audio_path.lower().endswith(SUPPORTED_EXTS):
        # faster-whisper uses ffmpeg under the hood; many extensions work, but
        # we warn on something obviously wrong rather than silently accepting.
        print(f"warning: extension may not be supported ({audio_path})",
              file=sys.stderr)
    model = get_model(model_name)
    segments, info = model.transcribe(
        audio_path,
        beam_size=beam_size,
        language=language,        # None => auto-detect
        vad_filter=vad_filter,    # skip silence; faster on short voice notes
    )
    parts = [seg.text.strip() for seg in segments if seg.text and seg.text.strip()]
    text = " ".join(parts).strip()
    return text, {
        "language": info.language,
        "duration": info.duration,
        "segments": len(parts),
        "model": model_name,
        "beam_size": beam_size,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_path", required=True,
                    help="path to audio file (ogg/opus/wav/mp3/m4a/flac)")
    ap.add_argument("--out", default=None,
                    help="write transcript here; default: stdout")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"faster-whisper model size (default {DEFAULT_MODEL})")
    ap.add_argument("--beam-size", type=int, default=DEFAULT_BEAM,
                    help=f"beam size (default {DEFAULT_BEAM})")
    ap.add_argument("--language", default=None,
                    help="force language code (e.g. en); default: auto-detect")
    ap.add_argument("--no-vad", action="store_true",
                    help="disable VAD silence-skip (default: enabled)")
    args = ap.parse_args()

    try:
        text, info = transcribe(
            args.in_path,
            model_name=args.model,
            beam_size=args.beam_size,
            language=args.language,
            vad_filter=not args.no_vad,
        )
    except FileNotFoundError:
        print(f"audio not found: {args.in_path}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"transcribe failed: {e}", file=sys.stderr)
        return 3

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w") as f:
            f.write(text + ("\n" if text else ""))
    else:
        print(text)
    # Always log the metadata to stderr so the caller can capture it.
    print(f"[stt] lang={info['language']} dur={info['duration']:.1f}s "
          f"segs={info['segments']} model={info['model']} "
          f"beam={info['beam_size']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
