# PROJECT: link-shortener (pilot)

**Stakeholder:** Tushar · **Chartered:** 2026-06-11 (approved in chat)
**Status meaning:** this is the PILOT of the project tier — besides the
deliverable itself, we're proving the loop (sessions, journal, blocked
round-trips, review).

## Goal

A small, self-hosted link shortener for onllm: clean API, durable
storage, tests, honest README. The kind of utility we'd actually run.

## Scope (v1)

- `POST /shorten` `{"url": ...}` → `{"code", "short_url"}`; idempotent
  for the same URL.
- `GET /<code>` → 301 to the original URL; unknown code → 404 JSON.
- `GET /stats/<code>` → `{"url", "created_at", "hits"}`.
- Durable storage that survives restarts.
- Input validation (reject non-http(s) schemes); no auth in v1 (LAN tool).
- Tests covering the above; README with quickstart; runs on the box.

## Out of scope (v1)

Custom aliases, expiry, auth/multi-user, analytics dashboards, Docker.
Note ideas in PLAN.md "Later" — do not build them.

## Open decisions (ask via BLOCKED when you actually need them)

- Public short domain + where this ultimately gets hosted (box? VPS?) —
  Tushar has not decided. v1 runs on the box; binding host/port choice
  and any domain wiring need his call.
- Final repo home: try `gh repo create onllm-dev/onlink --private`; if
  your PAT can't create repos, BLOCK and ask.

## Definition of Done (all verified, all true)

1. All scope endpoints implemented and covered by passing tests
   (`pytest` green, output quoted in journal).
2. Storage survives a process restart (test or demonstrated).
3. README: what it is, quickstart, API examples.
4. Code lives on branch `ollie/link-shortener` and is delivered as a PR
   (target repo per the open decision above).
5. Service demonstrably running on the box answering all three endpoints
   (curl transcript in journal).
6. Tushar has reviewed and said "done".

## Decisions log

- 2026-06-11: Project chartered as project-tier pilot (Tushar, in chat).
