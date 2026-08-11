#!/usr/bin/env python3
"""Minimal one-off to drive browser secret_ref and dump results + audit."""
import glob, json, os, time, urllib.request, datetime
from pathlib import Path

BEARER = "<BEARER_TOKEN_REDACTED>"
BASE = "http://<TAILSCALE_IP>:3200/mcp"
secret_ref = "vtest_pw"
expected = "<TEST_PASSWORD_REDACTED>"

hdr = {"Authorization": f"Bearer {BEARER}", "Content-Type": "application/json",
       "Accept": "application/json, text/event-stream"}

sid, _ = (lambda b: (urllib.request.urlopen(
    urllib.request.Request(BASE, data=json.dumps(b).encode(), headers=hdr, method="POST"),
    timeout=30).headers.get("mcp-session-id"), None))(
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "dbg", "version": "1"}}})
hdr["mcp-session-id"] = sid

def call(name, args, timeout=30):
    global sid
    r = urllib.request.urlopen(
        urllib.request.Request(BASE, data=json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": name, "arguments": args}}).encode(), headers=hdr, method="POST"),
        timeout=timeout)
    out = None
    for ln in r.read().decode().splitlines():
        if ln.startswith("data: "): out = json.loads(ln[6:])
    sid = r.headers.get("mcp-session-id") or sid
    c = (out or {}).get("result", {}).get("content", [])
    txt = next((x["text"] for x in c if x.get("type") == "text"), "{}")
    try: return json.loads(txt)
    except: return {"raw": txt[:300]}

print("goto...")
g = call("act", {"kind": "browser", "op": "goto",
                 "url": "data:text/html,<!doctype html><meta charset=utf-8><title>t</title><body><input id=s></body>",
                 "timeout": 20})
print("goto:", g)

print("fill secret_ref...")
f = call("act", {"kind": "browser", "op": "fill", "selector": "#s", "secret_ref": secret_ref, "timeout": 10})
print("fill:", f)

print("get_attr value...")
ga = call("act", {"kind": "browser", "op": "get_attr", "selector": "#s", "attr": "value", "timeout": 8})
print("get_attr:", ga)

print("extract...")
ex = call("act", {"kind": "browser", "op": "extract", "selector": "#s", "timeout": 8})
print("extract:", ex)

# Audit grep
d = Path(r"C:\ProgramData\ollie-hands\audit")
today = datetime.datetime.now().strftime("%Y%m%d")
fs = sorted(glob.glob(os.path.join(d, f"audit-{today}*.jsonl")) or glob.glob(os.path.join(d, "audit-*.jsonl")))
af = fs[-1] if fs else None
plain = False
ref_lines = []
if af:
    with open(af, encoding="utf-8") as fh:
        for ln in fh:
            if expected in ln: plain = True
            if secret_ref in ln or "secret_ref" in ln:
                ref_lines.append(ln.strip()[:900])
print("audit file:", af)
print("plain in audit:", plain)
print("ref lines:", len(ref_lines))
for ln in ref_lines[-2:]:
    print("  ", ln)
