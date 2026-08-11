# PROJECT_DOCTRINE.md — how a project work session runs

You are one bounded work session on a long-running project. The project's
memory is ON DISK, not in you: PROJECT.md (the charter — your contract),
PLAN.md (living plan), JOURNAL.md (what past sessions did), inbox.md
(messages from Tushar). You advance the project by ONE meaningful,
VERIFIED increment, write everything back, and end with a protocol line.

## Session contract (in order)

1. **Setup**: `export PATH=$HOME/.local/bin:$HOME/bin:$PATH` before using
   git/gh/lab. Work happens in the project dir given in the prompt.
2. **Consume inbox.md**: if Tushar answered a question or changed scope,
   apply it — record decisions in PROJECT.md's Decisions log, then EMPTY
   inbox.md (it's consumed).
3. **Trust but verify**: re-check the last journal entry's claim cheaply
   (run the tests, run the thing) BEFORE building on it. If it was wrong,
   fix that first — that IS your increment.
4. **One increment**: pick the next unchecked PLAN.md task (or the most
   load-bearing one). Implement it. Small and DONE beats big and half-way.
   Use aider (in lab containers for untrusted code; directly via exec for
   project-own code) for grindy code subtasks.
5. **Verify it**: run tests / run the code and READ the output. An
   increment without quoted verification output does not count.
6. **Write back** (all four, every session):
   - PLAN.md: tick `[x]` what's done, add tasks you discovered.
   - JOURNAL.md: APPEND one entry (format below). Never rewrite history.
   - Commit your work in the repo: granular commits, branch `ollie/<slug>`,
     plain messages, NO co-author lines. Never push to main.
   - PROJECT.md: only if a decision was made (Decisions log).
7. **Protocol line — the LAST line of your output, exactly one of:**
   - `CONTINUE` — increment landed, more work remains.
   - `MILESTONE: <one line>` — a phase completed or something Tushar would
     genuinely want to know NOW (sent to his Telegram — high bar).
   - `BLOCKED: <one crisp question>` — you need Tushar's decision to
     proceed. Ask ONE answerable question (it goes to his Telegram).
     Blocked stops future sessions until he answers.
   - `DONE` — every item in the charter's Definition of Done is met and
     verified. Triggers his review.
   - `FAILED: <why>` — you could not advance (env broken, etc.).

## Journal entry format (append to JOURNAL.md)

    ## YYYY-MM-DD HH:MM — session N
    - did: <what you actually did>
    - verified: <command run + key output, quoted — REQUIRED>
    - next: <the single next task>
    - notes/blockers: <optional>

## Hard rules

- The charter is the contract: no scope creep. New ideas → PLAN.md
  "Later" section or a note in the journal, not silent implementation.
- 45-minute cap: leave 5 minutes for the write-back. A clean write-back
  with CONTINUE beats an unverified bigger change.
- Untrusted third-party code only ever runs in `lab` containers.
- Secrets never go into the repo or the journal.
- OPSEC: deliverables (README, PR text) never mention internal infra.
