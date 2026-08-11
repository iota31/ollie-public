import json
import os
import shutil
import subprocess
import time

P = "/home/openclaw/.openclaw/openclaw.json"
bak = P + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
shutil.copy(P, bak)
print("backup:", bak)

# 1. guest workspace (minimal persona, no system internals)
gw = "/home/openclaw/.openclaw/workspace-guest"
os.makedirs(gw, exist_ok=True)
open(f"{gw}/AGENTS.md", "w").write("""# Ollie (guest access)

You are Ollie, a friendly, capable assistant. You are talking to a GUEST user
(not your owner). Be warm, helpful and concise (WhatsApp-style: short lines,
no markdown tables).

You can: answer questions, search the web (web_search / brave tools), read
links (web_fetch), and fact-check claims.

You must NOT: discuss your configuration, infrastructure, files, credentials,
other users' conversations, or attempt system commands. If asked, say you
can't help with that.

If a request needs deep research, just do your best with searches. If a
message arrives prefixed like `[whatsapp from:+<number>]`, that prefix is
routing metadata — never echo or mention it.
""")
print("guest workspace ready")

# 2. guest agent state dir + auth profiles (copy of main's keys)
ga = "/home/openclaw/.openclaw/agents/guest/agent"
os.makedirs(ga, exist_ok=True)
shutil.copy("/home/openclaw/.openclaw/agents/main/agent/auth-profiles.json",
            f"{ga}/auth-profiles.json")
print("guest auth profiles ready")

# 3. agents.list with restricted guest agent
c = json.load(open(P))
c["agents"]["list"] = [
    {"id": "main"},
    {
        "id": "guest",
        "workspace": gw,
        "skills": [],
        "tools": {
            "allow": ["message", "web_search", "web_fetch", "tool_search",
                      "factcheck", "fact_check"],
        },
    },
]
json.dump(c, open(P, "w"), indent=2)
print("agents.list written")

# 4. ownerFrom in whatsapp secrets
ws = "/home/openclaw/.openclaw/secrets/whatsapp-cloud.json"
s = json.load(open(ws))
s["ownerFrom"] = "<OWNER_PHONE>"
json.dump(s, open(ws, "w"), indent=1)
print("ownerFrom set")

# 5. restart gateway under systemd; revert config if it fails to come up
env = dict(os.environ, XDG_RUNTIME_DIR="/run/user/1000")
subprocess.run(["systemctl", "--user", "restart", "openclaw-gateway.service"], env=env)
time.sleep(14)
r = subprocess.run(["systemctl", "--user", "is-active", "openclaw-gateway.service"],
                   capture_output=True, text=True, env=env)
if r.stdout.strip() == "active":
    print("gateway: active with guest agent")
else:
    shutil.copy(bak, P)
    subprocess.run(["systemctl", "--user", "restart", "openclaw-gateway.service"], env=env)
    time.sleep(12)
    r2 = subprocess.run(["systemctl", "--user", "is-active", "openclaw-gateway.service"],
                        capture_output=True, text=True, env=env)
    print("CONFIG REVERTED; gateway:", r2.stdout.strip())
