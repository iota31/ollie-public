# Ollie — Safety-First Autonomous AI Agent System

> **Public showcase of the Ollie project's architecture and safety design.**
> This repository contains the safety-critical components, architecture documentation,
> and implementation plans from the full Ollie system. Operational credentials and
> deployment-specific values have been redacted.

---

## What is Ollie?

Ollie is a continuously available autonomous AI teammate designed to understand goals,
notice useful work, prioritize it, act safely, verify results, and report outcomes —
without constant human supervision.

The core engineering challenge is not capability — it's **containment, auditability,
and safe autonomy**. Ollie is built around the premise that an autonomous agent
operating on a real host computer, with access to real credentials and real external
services, must be designed to fail safely, resist compromise, and produce
tamper-evident evidence of everything it does.

## Safety Architecture

### Two-Tier Execution Trust Model

```
┌─────────────────────────────────────────────────────────────┐
│ TIER 1 — Isolated WSL Environment (Brain)                   │
│   • Windows interop DISABLED                                │
│   • Drive automount DISABLED                                │
│   • LLM cannot directly see host files or call host APIs    │
│   • Contains: agent logic, memory, background jobs          │
│                                          │ bearer HTTP      │
│                                          │ over tailnet     │
└──────────────────────────────────────────┼──────────────────┘
                                           ▼
┌─────────────────────────────────────────────────────────────┐
│ TIER 2 — Host Actuator (ollie-hands)                        │
│   • Hard policy engine (LLM-uneditable)                     │
│   • T0: read-only → auto-approve                            │
│   • T1/T2: local mutations → notify owner                   │
│   • T3: acts-as-user → BLOCK until human approval           │
│   • T4: security/audit tamper → HARD BLOCKED always         │
│   • SHA-256 hash-chained tamper-evident audit trail          │
│   • Human approval gates via Telegram (fails closed)        │
└─────────────────────────────────────────────────────────────┘
```

### Key Safety Properties

| Property | Implementation |
|----------|---------------|
| **Agent containment** | WSL distribution with Windows interop and automount disabled — LLM process cannot reach host files or input APIs |
| **Tiered action policy** | Hard-coded policy engine classifies every host action (T0–T4); destructive actions require human approval, security-relevant actions are unconditionally blocked |
| **Human-in-the-loop** | Consequential actions block execution and await owner approval via Telegram. Fails closed on timeout — no default-allow |
| **Tamper-evident audit** | Append-only JSONL audit trail with SHA-256 hash chains. Every record links to the prior record's hash. Off-box verification detects any edit, deletion, or reordering |
| **Sandboxed untrusted code** | Third-party repositories and POCs run only inside disposable `ollie-lab` environments, never on the host |
| **Candid threat modeling** | `ARCHITECTURE.md` explicitly models agent compromise, prompt injection, and untrusted-input boundaries — including what the architecture does NOT protect against |
| **Spend and iteration bounds** | Background jobs enforce cost ceilings, time limits, and iteration caps before auto-escalating to the owner |

## Repository Structure

```
ollie-hands/              # Host actuator: policy engine, consent, execution, audit
  ├── ollie_hands/
  │   ├── policy.py       # Tiered action classification (T0–T4)
  │   ├── audit.py        # SHA-256 hash-chained tamper-evident logging
  │   ├── auth.py         # Bearer authentication middleware
  │   ├── consent.py      # Human approval flow (Telegram integration)
  │   ├── actions/        # Sandboxed action executors (shell, browser, UI)
  │   └── server.py       # Starlette engine with BearerMiddleware
  └── tests/

ollie-executive/          # Goal/commitment/evidence ledger and priority selector
ollie-jobs/               # Background jobs, heartbeat, spend ceilings, work digests
ollie-lab/                # Disposable sandbox for untrusted code execution
ollie-watchdog/           # Runtime health monitoring and alerting
ollie-research/           # Research ingestion and candidates
ollie-stt/, ollie-tts/    # Local speech I/O (privacy-preserving, on-device)
ollie-guest/              # Guest isolation and setup

openclaw-ollie-hands-approval/   # Telegram approval plugin
openclaw-ollie-wa-approval/      # WhatsApp approval with owner/guest routing
openclaw-ollie-whatsapp-cloud/   # WhatsApp Cloud API channel plugin

Plans/                    # Implementation plans and architecture decisions
workspace/                # Agent identity and operating doctrine
scripts/                  # Operations and deployment scripts

ARCHITECTURE.md           # Full topology and candid trust-model analysis
DECISIONS.md              # Historical architectural decisions and rationale
RUNBOOK.md                # Live operations and disaster recovery
```

## Key Files to Read

If you're evaluating this project for its safety design:

1. **[`ARCHITECTURE.md`](ARCHITECTURE.md)** — Start here. Full system topology with an honest
   assessment of what the trust model protects against and what it doesn't.

2. **[`ollie-hands/ollie_hands/policy.py`](ollie-hands/ollie_hands/policy.py)** — The hard policy
   engine. Classifies every host action into trust tiers. Uses path normalization to prevent
   directory traversal. LLM-uneditable by design.

3. **[`ollie-hands/ollie_hands/audit.py`](ollie-hands/ollie_hands/audit.py)** — Cryptographically-linked
   append-only audit trail. SHA-256 hash chains with off-box verification logic.

4. **[`ollie-hands/ollie_hands/consent.py`](ollie-hands/ollie_hands/consent.py)** — Human approval
   flow. Blocks execution for T3 actions, sends approval request to owner, fails closed on timeout.

5. **[`Plans/`](Plans/)** — Real implementation plans showing how safety decisions were made,
   debugged, and evolved under production conditions.

## Design Philosophy

This project reflects several beliefs about building autonomous AI systems:

- **Safety is architecture, not prompting.** Prompt instructions are guidance, not a security
  boundary. The trust model is enforced by code that the LLM cannot modify.
- **Fail closed, not open.** When approval times out, the action is denied. When a health check
  fails, the system halts. When containment is uncertain, the action is blocked.
- **Candor over theater.** The architecture document explicitly states what is and isn't protected.
  Security claims that omit known gaps are worse than no claims at all.
- **Auditability is non-negotiable.** Every consequential action produces a tamper-evident record.
  If you can't verify what the agent did, you can't trust what the agent did.

## Context

This is a personal project built by [Tushar Shukla](https://linkedin.com/in/tushar--shukla)
as a practical exploration of autonomous AI agent safety — applying production engineering
experience to the problem of making AI systems that can act in the real world without
unacceptable risk.

Ollie runs on real infrastructure, handles real tasks, and operates under real constraints.
The safety design is not theoretical — it was built to protect a system that actually
executes shell commands, browses the web, and sends messages on behalf of its owner.

---

*Operational credentials and deployment-specific identifiers have been redacted from this
public showcase. The full system runs on private infrastructure.*
