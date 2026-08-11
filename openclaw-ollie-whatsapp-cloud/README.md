# `ollie-whatsapp-cloud` — WhatsApp Cloud API channel plugin (deployed copy)

OpenClaw channel plugin for inbound/outbound WhatsApp via the official Meta
Cloud API (Graph v21.0). `index.js` here is the **built, deployed artifact**
(snapshot of the live file), not hand-maintained source. Deploys to
`/home/openclaw/.openclaw/plugins/ollie-whatsapp-cloud/` on the box (WSL
`OpenClawGateway` on `<TAILSCALE_IP>`); loaded by the gateway on startup.

This snapshot (2026-06-09) includes: sender envelope `[whatsapp from:+<digits>]`
prepended to inbound text, embedded-path model-chain failover, and a 75s
soft-turn-timeout that auto-converts slow turns into background jobs
(see `../ollie-jobs/`).
