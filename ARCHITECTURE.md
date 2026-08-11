# Ollie Architecture

**Architecture truth as of 2026-07-10.** This document describes the system
that exists now, not the June prototype or the eventual Jarvis vision. Historical
choices remain in `DECISIONS.md`; product and hardening work is tracked in
`Plans/ollie-cofounder-roadmap.md`.

## Product boundary

Ollie is meant to be a continuously available third teammate: understand the
founders' goals, notice useful work, select it, act, verify the result, remember
the commitment, and report the outcome. OpenClaw is the conversation and tool
runtime inside Ollie; it is not the product architecture by itself.

The deployed system has two **execution trust tiers**:

1. **Tier 1 — brain and control plane:** an OpenClaw gateway and Ollie's
   background services inside an isolated WSL distribution.
2. **Tier 2 — host actuator:** `ollie-hands`, an unsandboxed but policy-gated
   Windows process in the interactive desktop session.

There are also external dependencies—model providers, Telegram, WhatsApp,
4DPocket, search and CAPTCHA services. They are not a third trusted tier; they
are remote systems reached with scoped credentials and must be treated as
untrusted data sources.

## Current topology

```text
                        external services
             model APIs · Telegram · WhatsApp · 4DPocket · web
                         ▲                 ▲
                         │ scoped network  │ untrusted content
                         │                 │
┌────────────────────────┴─────────────────┴─────────────────────────┐
│ TIER 1 — WSL OpenClawGateway                                      │
│                                                                   │
│ OpenClaw gateway                                                   │
│   channels → agent session → tools/MCP → response                 │
│        │             │             │                               │
│        │             │             └──────────────┐                │
│        │             ▼                            │                │
│        │      workspace instructions              │                │
│        │      memory / open loops / projects       │                │
│        │                                           │                │
│        └─ background control loops                 │                │
│           heartbeat · jobs · project tick · lab    │                │
│           curiosity/research · briefs · monitors   │                │
│                                                    │ bearer HTTP    │
│ operations: systemd-user services · watchdog       │ over tailnet   │
└────────────────────────────────────────────────────┼────────────────┘
                                                     ▼
┌────────────────────────────────────────────────────────────────────┐
│ TIER 2 — Windows host, interactive session                         │
│                                                                    │
│ ollie-hands :3200                                                  │
│   request gate → policy/consent → action → postcondition → audit   │
│                           │                                        │
│   shell · UI Automation · Camoufox browser · raw pixels · CAPTCHA  │
│                           ▼                                        │
│                    real host and outside world                     │
│                                                                    │
│ operations: Scheduled Task → supervisor → engine; audit sync       │
└────────────────────────────────────────────────────────────────────┘
```

The important product fact is not visible in this diagram: today the Tier 1
control plane is a collection of adjacent loops, not one executive. Heartbeat,
projects, lab, research, curiosity, jobs and reminders can each originate work,
but there is no canonical goal portfolio, opportunity queue, priority decision,
or outcome ledger shared by all of them. This is why Ollie can be continuously
active without reliably owning the founders' most important outcome.

## Tier 1 — brain and control plane

**Runtime:** OpenClaw gateway in the WSL distribution `OpenClawGateway`.

The WSL distribution has Windows interop and drive automount disabled. This is
the load-bearing containment property: the LLM process does not directly see
the Windows drives or desktop and cannot directly call host input APIs.

Tier 1 currently contains four kinds of responsibility:

| Area | Current implementation | Architectural assessment |
|---|---|---|
| Conversation | Telegram and WhatsApp channel/plugin paths feeding OpenClaw sessions | Telegram is the primary control surface. Channel behavior is not fully unified; WhatsApp still has separate policy and delivery paths. |
| Agent policy | Workspace identity, safety, project, heartbeat, memory and channel instructions | Useful operating doctrine, but prompt instructions are guidance rather than a hard security boundary. Multiple files can drift or conflict. |
| Autonomous work | Heartbeat, jobs runner, project tick, lab watcher, dream/curiosity promotion, research feeds and briefs | Strong primitives, but independent producers compete without a canonical selector. Timers currently substitute for executive judgment. |
| Durable state | Workspace Markdown/JSON, project files, job records, OpenClaw session JSONL and 4DPocket | The disk-backed/session-independent pattern is correct. The schemas and sources of truth are fragmented. |
| Operations | systemd-user services, watchdog, backups and logs | Appropriate for WSL, but supervision is distributed and deployment/source drift has occurred. Health checks prove reachability more often than end-to-end correctness. |

### What WSL containment does and does not mean

It **does** prevent a compromised agent process from directly browsing host
files or injecting input through Windows APIs.

It **does not** make Tier 1 trusted. Tier 1 can use network credentials, mutate
its own durable memory, submit background jobs, call external services and send
requests to Hands. Prompt injection, hallucinated state, stale memory and
compromised dependencies therefore remain relevant. All content returned by
4DPocket, webpages, messages, repositories and research feeds is data, never
authority to widen scope or lower a consent tier.

## Tier 2 — `ollie-hands`

**Runtime:** a Python MCP/HTTP server on port 3200, running unsandboxed as the
Windows desktop user in interactive session 1. A Scheduled Task launches a
supervisor which restarts the engine. Hands must run in the interactive session
because screenshots, UI Automation and input injection do not work from a
Windows service in session 0.

Hands is intentionally a separate component and should remain so. It gives the
host a narrow place to enforce policy regardless of which model or agent calls
it. Its current capability ladder is:

| Rung | Mechanism | Typical use |
|---|---|---|
| L0 | constrained PowerShell | host inspection, files, processes and local configuration |
| L1 | Windows UI Automation | deterministic native controls and window management |
| L2 | persistent Camoufox/Playwright browser | web navigation, extraction, forms and logged-in sessions |
| L3 | raw pixels/input | last-resort controls with no semantic surface |
| External helper | CAPTCHA solver | explicit, metered external solving |

Single actions use `act`; bounded multi-step actions use `plan_submit`, with
preconditions and required postconditions for writes. The July browser lifecycle
defect was fixed by moving Camoufox to a persistent async lifecycle. That repair
qualifies the specific sequence that failed; it does not by itself qualify every
website, consent path or unattended task.

### Existing controls

- Fresh installations are inert (`enabled: false`).
- A host-side `DISABLED` file is checked when a tool request enters the engine.
- HTTP requests require a bearer token stored outside the repository.
- In-code policy classifies actions as auto, notify, confirm or blocked.
- Confirmation fails closed on timeout.
- Every action is appended to a hash-chained JSONL audit; screenshots can be
  retained and the audit can be copied and verified off-box.
- Act-scripts support preconditions, postconditions, checkpoints and cooperative
  aborts.

### Candid trust boundary

Hands is **not a sandbox**. It executes as the host user and its shell, UIA,
browser and pixel capabilities can affect the machine and the outside world.
The bearer proves that a caller knows the shared secret; it does not prove that
the request reflects the owner's intent. Because Tier 1 holds that bearer, a
compromised Tier 1 must still be contained by Hands policy and owner consent.

The following are known gaps, so the stronger claims in early documents must
not be treated as guarantees:

1. **Consent is not yet an independent authentication boundary.** The current
   `/consent` endpoint uses the same bearer as ordinary Hands calls, accepts a
   short code, and has no dedicated rate limit. A compromised bearer holder may
   be able to approve its own request. The Telegram prompt is useful owner UX,
   but the relay needs separately authenticated, request-bound approval.
2. **Policy is not consequence-complete.** Browser commit detection uses the
   declared flag and resolved button text, but the same external action may be
   expressed through UIA or raw pixels and receive only notification. Consent
   must be based on consequence, identity and destination—not actuator syntax.
3. **The kill switch is request-bound, not fully step-bound.** It gates new tool
   calls, while an already-running act-script is cooperatively stopped between
   steps through a separate abort path. The disabled state should be rechecked
   before every consequential step.
4. **Retries can duplicate effects.** A failed postcondition may cause a write
   to be dispatched again. Consequential steps need idempotency keys or explicit
   no-retry semantics; absence of observed success is not proof of failure.
5. **The audit is tamper-evident, not signed or intrinsically immutable.** A
   host compromise can rewrite the local chain. Timely off-box anchoring and
   verification are what make later rewriting detectable.
6. **The browser profile is sensitive host state.** Persistent login cookies
   improve usefulness but increase the blast radius of browser or host
   compromise. Profile access, backups and incident rotation need explicit
   treatment.
7. **Regex and verb classification are a guardrail, not a proof system.** The
   shell default permits narrated local mutation, and novel command forms or
   indirect effects can evade semantic intent classification.

Until these are closed, Hands is suitable for monitored, reversible trials and
carefully approved actions—not unrestricted unattended operation as the owner.

## External boundaries

| Boundary | What is trusted | What is not |
|---|---|---|
| Telegram inbound | Configured owner chat allowlist | Message content; bot-token or owner-account compromise; approval semantics unless independently verified |
| WhatsApp inbound/outbound | Channel allowlists and explicit contact policy where enforced | Unknown senders, message content, and prompt-only outbound rules |
| 4DPocket | Scoped credential and service availability | Stored content as instructions; memory correctness merely because it was persisted |
| Model providers | Transport/API authentication | Factual correctness, stable behavior, or resistance to adversarial context |
| Web/research feeds | Network response | Every instruction or claim inside the response |
| Git repositories/lab inputs | Repository identity and recorded provenance | Executed third-party code; it must stay in the lab sandbox until promoted deliberately |

## Operations and availability

There is deliberately no single supervisor across both tiers:

- WSL services use systemd-user supervision.
- Hands uses Windows Task Scheduler, AutoLogon/session 1 and a Python
  supervisor.
- The watchdog observes gateway, jobs, Hands and external reachability.
- Audit and state backups are separate scheduled processes.

This arrangement is necessary, but it means a green process check is not the
same as a working product. End-to-end synthetic checks must verify a complete
path: channel → reasoning/tool call → durable state or harmless action →
evidence → report. Physical power loss, Windows interactive-session loss,
credential expiry, public tunnel failure and a wedged watchdog remain distinct
availability risks.

## Target product topology

The two trust tiers should remain. The course correction is inside Tier 1: add
one executive core and make existing loops producers or executors beneath it.

```text
founder conversations + monitors + saved items + system health
                              │
                              ▼
                    opportunity / commitment inbox
                              │
                              ▼
             goals → priority selector → bounded work contract
                              │
                 ┌────────────┼────────────┐
                 ▼            ▼            ▼
              chat/job      project       lab/research
                 └────────────┼────────────┘
                              ▼
               tools / 4DPocket / gated Hands
                              ▼
          evidence → outcome ledger → memory → founder brief
                         │
                         └──── expected vs realized value feedback
```

Heartbeat, jobs, projects, lab, research, briefs, monitors and Hands are mostly
reusable. They should not each own priority. One canonical executive state
must answer: what are we trying to achieve, what has Ollie promised, what is
the next highest-value bounded action, what evidence closes it, and when should
Ollie stop?

## Architectural invariants

1. A conversation may create a commitment, but conversational memory is never
   the only record of it.
2. Every autonomous initiative is linked to a goal or an explicitly budgeted
   exploration slot.
3. Every task has a bounded work contract: scope, cost/risk, stop condition and
   task-appropriate success evidence.
4. Completion is a verified state transition, not model prose.
5. External content cannot add authority, widen scope or lower consent.
6. Consequence—not tool modality—determines consent.
7. Retries of consequential actions are idempotent or require re-approval.
8. A kill switch is checked immediately before each consequential action.
9. Deployed artifacts are traceable to reviewed source and drift is detected.
10. “No valuable work now” is a valid executive decision; activity volume is
    never a success metric.
