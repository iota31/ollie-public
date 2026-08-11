#!/usr/bin/env python3
"""reel_understand — turn a video/reel URL into fact-checkable text.

Hybrid pipeline (each half is best-effort; either alone is useful):
  A) 4DPocket ingestion (Ollie's own account): submit the URL, poll the
     item, harvest caption/title/description/sections. This also PERSISTS
     the reel in Ollie's KB — over time a corpus of everything checked.
     IG reels: caption + IG's auto alt-text (no audio — that's half B).
  B) On-box audio: yt-dlp downloads the audio track, Groq Whisper
     transcribes it. This captures the SPOKEN claim, which 4DPocket's
     processors don't get for IG/TikTok.

Output: one JSON object on stdout:
  { "url", "platform", "title", "caption", "transcript",
    "fourdpocket_item_id", "notes": [ ...what failed/degraded... ] }

Usage: reel_understand.py <url> [--timeout 240]
Exit 0 if at least one half produced content; 1 if both failed.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

FOURDP_BASE = "http://<TAILSCALE_IP_VPS>:4040/api/v1"
FOURDP_HOST_HEADER = "localhost:4040"  # proxy 421s without it
OLLIE_PAT_FILE = "/home/openclaw/.openclaw/secrets/fourdpocket-ollie.pat"
WA_SECRETS = "/home/openclaw/.openclaw/secrets/whatsapp-cloud.json"  # groqApiKey lives here
YTDLP = "/home/openclaw/.local/bin/yt-dlp"
POLL_INTERVAL_S = 5


def fourdp_req(method, path, pat, body=None, timeout=30):
    req = urllib.request.Request(
        f"{FOURDP_BASE}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Host": FOURDP_HOST_HEADER,
            "Authorization": f"Bearer {pat}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def harvest_item(item):
    """Pull the useful text out of an ItemRead payload."""
    out = {}
    for k in ("title", "description", "content"):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()[:4000]
    return out


def fourdp_ingest(url, pat, deadline, notes):
    """Submit URL (409 = already saved -> reuse), poll until text appears."""
    item_id = None
    try:
        item = fourdp_req("POST", "/items", pat, {"url": url})
        item_id = item.get("id")
    except urllib.error.HTTPError as e:
        if e.code == 409:
            try:
                chk = fourdp_req("GET", f"/items/check-url?url={urllib.parse.quote(url, safe='')}", pat)
                item_id = chk.get("item_id") or chk.get("id")
                notes.append("4dpocket: URL already saved, reusing item")
            except Exception as e2:  # noqa: BLE001
                notes.append(f"4dpocket: dup but lookup failed: {e2}")
        else:
            notes.append(f"4dpocket: submit failed HTTP {e.code}")
    except Exception as e:  # noqa: BLE001
        notes.append(f"4dpocket: unreachable: {e}")
    if not item_id:
        return None, {}

    best = {}
    while time.time() < deadline:
        try:
            item = fourdp_req("GET", f"/items/{item_id}", pat)
            best = harvest_item(item) or best
            # processed when description/content went beyond the bare URL
            if best.get("content") or best.get("description"):
                break
        except Exception as e:  # noqa: BLE001
            notes.append(f"4dpocket: poll error: {e}")
            break
        time.sleep(POLL_INTERVAL_S)
    if not best:
        notes.append("4dpocket: item created but no text extracted in time")
    return item_id, best


def transcribe(url, deadline, notes):
    """yt-dlp audio -> Groq Whisper. Returns transcript or ''."""
    try:
        groq_key = json.load(open(WA_SECRETS)).get("groqApiKey", "")
    except Exception:  # noqa: BLE001
        groq_key = ""
    if not groq_key:
        notes.append("audio: no groq key available")
        return ""
    with tempfile.TemporaryDirectory() as td:
        out_tmpl = f"{td}/audio.%(ext)s"
        budget = max(30, int(deadline - time.time()) - 30)
        r = subprocess.run(
            [YTDLP, "-x", "--audio-format", "mp3", "--audio-quality", "9",
             "--ffmpeg-location", "/home/openclaw/bin",
             "--max-filesize", "40M", "-o", out_tmpl, "--no-playlist",
             "--quiet", "--no-warnings", url],
            capture_output=True, text=True, timeout=budget,
        )
        if r.returncode != 0:
            notes.append(f"audio: yt-dlp failed: {(r.stderr or '')[:200]}")
            return ""
        import glob
        files = glob.glob(f"{td}/audio.*")
        if not files:
            notes.append("audio: yt-dlp produced no file")
            return ""
        audio = files[0]
        # multipart by hand to avoid extra deps
        import uuid
        boundary = uuid.uuid4().hex
        raw = open(audio, "rb").read()
        body = b""
        for name, val in (("model", "whisper-large-v3-turbo"),):
            body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                     f"name=\"{name}\"\r\n\r\n{val}\r\n").encode()
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                 f"filename=\"audio.mp3\"\r\nContent-Type: audio/mpeg\r\n\r\n").encode()
        body += raw + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            data=body, method="POST",
            headers={"Authorization": f"Bearer {groq_key}",
                     # Groq's Cloudflare 403s the default Python-urllib UA
                     "User-Agent": "curl/8.5.0",
                     "Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return (json.loads(resp.read().decode()).get("text") or "").strip()
        except Exception as e:  # noqa: BLE001
            notes.append(f"audio: whisper failed: {e}")
            return ""


def detect_platform(url):
    for pat, name in ((r"instagram\.com", "instagram"), (r"tiktok\.com", "tiktok"),
                      (r"youtu\.?be", "youtube"), (r"(twitter|x)\.com", "x"),
                      (r"facebook\.com|fb\.watch", "facebook")):
        if re.search(pat, url):
            return name
    return "generic"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--timeout", type=int, default=240)
    args = ap.parse_args()
    deadline = time.time() + args.timeout
    notes = []

    try:
        pat = open(OLLIE_PAT_FILE).read().strip()
    except OSError:
        pat = ""
        notes.append("4dpocket: ollie PAT file missing")

    item_id, harvested = (None, {})
    if pat:
        item_id, harvested = fourdp_ingest(args.url, pat, deadline, notes)
    transcript = transcribe(args.url, deadline, notes)

    result = {
        "url": args.url,
        "platform": detect_platform(args.url),
        "title": harvested.get("title", ""),
        "caption": harvested.get("description", "") or harvested.get("content", ""),
        "transcript": transcript,
        "fourdpocket_item_id": item_id,
        "notes": notes,
    }
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if (transcript or result["caption"] or result["title"]) else 1


if __name__ == "__main__":
    sys.exit(main())
