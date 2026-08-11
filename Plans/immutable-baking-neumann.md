# Add a simple owner-controlled Hands bypass mode

## Context

Ollie Hands currently asks for owner approval when hard policy classifies an action as `CONFIRM`. This is appropriate by default but too slow for open-ended autonomous work such as farming Reddit karma.

`plan_submit` is not the bypass mechanism. It remains useful for deterministic sequences, but open-ended work needs Ollie’s brain between actions: observe a post, understand it, write a relevant comment, act, verify, and choose the next post. Bypass mode must therefore apply to both ordinary `act` calls and `plan_submit`, without replacing either execution path.

Implement exactly two modes:

- `normal` — current behavior, unchanged.
- `bypass` — skip owner confirmation for actions that policy would normally classify as `CONFIRM`.

Bypass changes only the confirmation step. It does not weaken hard blocks, authentication, auditing, verification, collision handling, abort behavior, or the host kill switch.

## How it works for the owner

The normal flow is **task first**, not mode first:

1. You tell Ollie what to do in ordinary language, exactly as today.
2. Ollie starts working in `normal` mode. Reads and already-safe actions continue normally.
3. At the first action that currently needs confirmation, Telegram shows three choices:
   - **Approve once** — approve only this action or plan; remain in `normal` mode.
   - **Enable bypass & continue** — approve this pending action and switch Hands to `bypass` mode.
   - **Deny** — deny the action; remain in `normal` mode.
4. After choosing bypass, Ollie continues its normal observe → reason → act loop without further confirmation prompts. This is not a fixed script: Ollie can read each Reddit post, think, write a contextual comment, verify it, and decide what to do next.
5. Bypass stays active globally for Hands until you send the exact owner command `hands normal`, restart Hands, or use the existing `DISABLED` kill switch. Telegram confirms every transition, and `hands mode` reports the current state.

For cases where you already know you want autonomy, the exact owner command `hands bypass` may be sent before the task. The task-first three-button prompt is the default UX, so you do not have to decide in advance.

The **Enable bypass & continue** operation must be atomic inside Hands: it consumes the valid pending H-ref, activates bypass, and releases that same waiting action. If the H-ref is missing, expired, already consumed, or unauthorized, neither approval nor mode activation occurs.

## Implementation

1. **Add one in-memory mode switch in Hands.**
   - Create a tiny mode controller with `normal` as the startup default.
   - Support only `normal`, `bypass`, and status lookup.
   - Do not add grants, scopes, profiles, TTLs, persistence, or a second policy engine.
   - A Hands restart resets the mode to `normal`.

2. **Make mode changes owner-only.**
   - Extend the existing Telegram approval keyboard with **Enable bypass & continue**, alongside the existing one-time approve and deny choices.
   - Also support the exact owner commands `hands bypass`, `hands normal`, and `hands mode` for pre-enabling, disabling, and status.
   - Intercept these controls before the LLM, using the same strict Telegram owner identity/authorization checks as inline approvals.
   - Relay mode changes to Hands using the approval-only credential, never the MCP bearer.
   - Ollie, MCP callers, page content, and plan arguments cannot change the mode.

3. **Expose a narrow authenticated Hands mode route.**
   - Add status/set operations alongside the owner-control `/consent` surface, authenticated only by the approval token.
   - Add one atomic owner operation for **approve pending H-ref and enable bypass**; do not implement this as two network calls that could partially succeed.
   - Accept only the two exact mode values.
   - Audit every successful and rejected mode operation using sanitized metadata.

4. **Apply bypass at the existing consent choke points.**
   - In `engine.act_step()`, keep normal policy classification. If the result is `CONFIRM` and mode is `bypass`, skip `consent.confirm()` and dispatch normally.
   - In `executor.run()`, apply the same rule to the existing one-per-plan confirmation.
   - `AUTO` and `NOTIFY` behavior remains unchanged.
   - `BLOCKED` always remains blocked, including in bypass mode.
   - `cfg.hands_enabled()` and the `DISABLED` flag always win.

5. **Keep open-ended quality intact.**
   - Do not force bypass work through `plan_submit`.
   - Ollie may continue using the normal observe → reason → act → verify loop, including generating each Reddit comment with full context.
   - `plan_submit` remains available only where a deterministic group of steps is useful.

6. **Make mode visible and controllable.**
   - Include the current mode in `session_info()`.
   - Telegram replies clearly confirm `normal` or `bypass` and explain that restart resets to normal.
   - Bypass activation must warn that permitted sends/posts/deletes/purchases can execute without another approval.

## Critical files

- `ollie-hands/ollie_hands/mode.py` — minimal in-memory two-state controller.
- `ollie-hands/ollie_hands/engine.py` — direct-action confirmation bypass.
- `ollie-hands/ollie_hands/executor.py` — plan-level confirmation bypass.
- `ollie-hands/ollie_hands/server.py` — owner-only mode route and status exposure.
- `openclaw-ollie-wa-approval/index.js` — strict owner Telegram commands and relay.
- Focused tests under `ollie-hands/tests/` and `openclaw-ollie-wa-approval/test/`.

## Verification

1. Unit-test startup/default mode, exact mode values, status, and restart semantics.
2. Prove normal mode retains current approval behavior for direct actions and plans.
3. Prove **Approve once** releases only the pending action and leaves mode `normal`.
4. Prove **Enable bypass & continue** atomically consumes the pending H-ref, switches to `bypass`, and releases that same action; expired/replayed/unauthorized refs do neither.
5. Prove bypass mode skips confirmation for later permitted `CONFIRM` direct actions and plans.
6. Prove `hands bypass`, `hands normal`, and `hands mode` work only for the authenticated owner and never reach the LLM.
7. Prove `BLOCKED` actions remain blocked and never dispatch in bypass mode.
8. Prove the `DISABLED` kill switch prevents execution in both modes.
9. Prove MCP credentials and unauthorized Telegram users cannot read or change mode.
10. Prove mode changes and bypassed actions are audited without secrets or arbitrary content.
11. Run the existing policy, consent, executor, approval-auth, and plugin callback suites.
12. Before deployment, diff repository files against the live Windows box, deploy only reviewed changes, restart Hands with `restart-host.ps1`, and verify startup mode is `normal`.
13. Give Ollie one natural open-ended task in normal mode; at the first real owner prompt choose **Enable bypass & continue**, verify Ollie continues ordinary observe/reason/act cycles without more prompts, then send `hands normal` and verify the next consequential action requests approval again.

Do not commit, deploy, restart, or run a real owner test without separate authorization after local implementation and review.
