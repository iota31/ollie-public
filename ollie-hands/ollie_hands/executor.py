"""Executor — runs an act-script locally at machine speed (plan §D2, §executor
semantics). Consent is decided ONCE for the whole script; thereafter steps run
without a model call each. Escalates to the brain only on a failed condition,
policy boundary, explicit checkpoint, collision, or timeout.

Per step:  preconditions -> dispatch -> verify postcondition.
on_fail (precondition): retry | repair | escalate | abort.
verify-after-act (postcondition): re-observe once, then escalate without
repeating an action whose outcome is unknown.

A task registry backs task_status / task_abort. Abort is cooperative: checked
between steps and around the per-step action.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from . import browser as L2
from . import conditions as Cond
from . import engine as Eng
from . import policy as P
from .consent import normalize_consent_result
from .grants import GrantError, canonical_resource


def _runtime_browser_decision(step) -> P.Decision | None:
    """Re-classify a browser step at runtime for live context escalation.

    Uses the step's declared effect and commit flag, and — for click/press/
    select — resolves the ACTUAL target text from the live DOM via the
    browser's element_text so a commit-like control (send/post/buy/connect/
    submit…) escalates to CONFIRM even when the plan declared a reversible
    effect (navigation/progress/…) or omitted the commit flag.  A hijacked
    planner cannot launder a consequential control past this gate.

    Returns None for non-browser steps.
    """
    if step.kind != "browser":
        return None
    args = step.args or {}
    op = (args.get("op") or "").lower()
    effect = args.get("effect")
    commit = bool(args.get("commit"))
    key = args.get("key", "")
    target_text = ""
    # Resolve the live target text for controls whose text can carry a commit
    # meaning. Reuse the browser's existing element_text read (never a new verb).
    if op in {"click", "press", "select"} and args.get("selector"):
        try:
            target_text = Eng.L2.element_text(args["selector"]) or ""
        except Exception:
            target_text = ""
    try:
        return P.classify_browser(op, effect=effect, commit=commit,
                                  target_text=target_text, key=key)
    except Exception:
        return None


def _enforce_live_resource(url: str | None, required_resources: set[str]) -> None:
    """Fail closed unless the live browser origin is an approved origin.

    canonical_resource reduces any URL to a bare scheme://host[:port] origin,
    so this is an EXACT origin-set membership test — a page that navigated to
    a look-alike host (e.g. reddit.com.attacker.com) or any unlisted origin is
    rejected. A missing/unparseable URL also fails closed.
    """
    if not required_resources:
        return
    if not url:
        raise GrantError("live_url_unavailable")
    try:
        live = canonical_resource(url)
    except Exception:
        raise GrantError("live_url_unparseable")
    if live not in required_resources:
        raise GrantError("live_resource_out_of_scope")


def _live_browser_url() -> str | None:
    """Read the actual browser status URL. None if unavailable/unstarted."""
    try:
        st = Eng.L2.status()
    except Exception:
        return None
    if not isinstance(st, dict):
        return None
    return st.get("url")


# task_id -> {status, title, step, total, results, abort(Event), started, ...}
_TASKS: dict[str, dict] = {}
_TLOCK = threading.Lock()


class ExecutionCancelled(RuntimeError):
    """Raised when an abort/kill gate closes at an execution boundary."""

    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(detail)
        self.stage = stage


@dataclass
class GrantContext:
    """Per-run scoped-authorization runtime context.

    Binds the executor's live guards to a single issued/reused grant:
    the approved origins (for live-URL enforcement) and a store+id+holder
    triple used to atomically reserve the single-use commit allowance.
    """
    store: object
    grant_id: str
    holder: str
    required_resources: set[str]

    def step_is_commit(self, step) -> bool:
        """A browser step whose declared effect consumes the commit allowance.

        Uses the mechanically derived effect category (never planner trust):
        COMMIT_EFFECTS members (external_commit/identity_commit/destructive)
        are the single-use commit surface.
        """
        if step.kind != "browser":
            return False
        eff, _commit, valid = P._effect((step.args or {}).get("effect"))
        if not valid:
            # An invalid envelope is already classified CONFIRM upstream; treat
            # it as commit-bearing so it cannot slip past the reservation.
            return True
        return bool(eff in P.COMMIT_EFFECTS)

    def reserve_commit(self) -> None:
        self.store.reserve_commit(self.grant_id, self.holder)


def _ensure_runnable(rec: dict, gate: Callable[[], bool] | None,
                     stage: str) -> None:
    """Fail closed immediately before an action or observation boundary."""
    if _aborted(rec):
        raise ExecutionCancelled(stage, "task abort requested")
    if gate is None:
        return
    try:
        enabled = gate()
    except Exception as exc:  # noqa: BLE001 - a broken gate must fail closed
        raise ExecutionCancelled(
            stage, f"execution gate check failed: {str(exc)[:200]}") from exc
    if not enabled:
        raise ExecutionCancelled(stage, "execution gate is closed")


def get_status(task_id: str) -> dict | None:
    with _TLOCK:
        t = _TASKS.get(task_id)
        if not t:
            return None
        return {k: v for k, v in t.items() if k != "abort"}


def request_abort(task_id: str) -> bool:
    with _TLOCK:
        t = _TASKS.get(task_id)
        if not t:
            return False
        t["abort"].set()
        return True


def _new_task(script) -> tuple[str, dict]:
    tid = uuid.uuid4().hex[:12]
    rec = {"task_id": tid, "status": "running", "title": script.title,
           "step": 0, "total": len(script.steps), "results": [],
           "consent": script.consent.consent, "hash": script.hash,
           "abort": threading.Event(), "started": time.time()}
    with _TLOCK:
        _TASKS[tid] = rec
    return tid, rec


def _aborted(rec) -> bool:
    return rec["abort"].is_set()


def _script_preview(script) -> str:
    lines = [f"Task: {script.title}  ({len(script.steps)} steps, "
             f"consent={script.consent.consent})"]
    for st in script.steps:
        lines.append(f"  {st.id}. {Eng._preview(st.kind, st.args)}")
    return "\n".join(lines)


def _finish_narration(consent, script, rec: dict,
                      message_id: int | None) -> None:
    """Emit one terminal signal, preferably by editing the start bubble."""
    if script.consent.consent != P.NOTIFY:
        return
    if hasattr(consent, "task_finished"):
        consent.task_finished(message_id, script.title, status=rec["status"],
                              step=rec["step"], total=rec["total"],
                              detail=rec.get("error", ""))
    else:  # compatibility for embedders and test doubles
        consent.notify(f"{rec['status']}: {script.title} · "
                       f"{rec['step']}/{rec['total']}")


def _run_step(step, *, cfg, audit, rec,
              gate: Callable[[], bool] | None = None,
              grant_ctx: "GrantContext | None" = None) -> dict:
    """Run a single validated step. Returns an outcome dict."""
    last_shell_exit = None
    action_dispatched = False

    def cancelled(exc: ExecutionCancelled) -> dict:
        if action_dispatched:
            detail = ("action was dispatched but outcome verification stopped; "
                      f"no action was repeated: {exc}")
            audit.event("step", args={"id": step.id, "kind": step.kind},
                        status="outcome_unknown", detail=detail[:200])
            return {"id": step.id, "status": "outcome_unknown",
                    "stage": exc.stage, "detail": detail}
        audit.event("step", args={"id": step.id, "kind": step.kind},
                    status="cancelled", detail=str(exc)[:200])
        return {"id": step.id, "status": "cancelled", "stage": exc.stage,
                "detail": str(exc)}

    # 1) preconditions, with on_fail handling
    if step.preconditions:
        try:
            _ensure_runnable(rec, gate, "precondition")
        except ExecutionCancelled as exc:
            return cancelled(exc)
        ok, detail = Cond.check_all(step.preconditions)
        if not ok:
            if step.on_fail == "retry":
                for _ in range(3):
                    time.sleep(0.6)
                    try:
                        _ensure_runnable(rec, gate, "precondition_retry")
                    except ExecutionCancelled as exc:
                        return cancelled(exc)
                    ok, detail = Cond.check_all(step.preconditions)
                    if ok:
                        break
            elif step.on_fail == "repair":
                # cheap repair: let the UI settle, re-evaluate once
                time.sleep(1.0)
                try:
                    _ensure_runnable(rec, gate, "precondition_recovery")
                except ExecutionCancelled as exc:
                    return cancelled(exc)
                ok, detail = Cond.check_all(step.preconditions)
        if not ok:
            return {"id": step.id, "status": "escalate",
                    "stage": "precondition", "detail": detail,
                    "on_fail": step.on_fail}

    # 2) dispatch the action (raw — consent already handled at script level)
    try:
        _ensure_runnable(rec, gate, "action")

        # Runtime browser decision: re-classify with live context (resolved
        # target text etc) so a commit-like control on the actual page escalates
        # beyond the plan-time effect declaration. Nothing has been dispatched
        # yet, so escalating here is always safe.
        if step.kind == "browser":
            rt = _runtime_browser_decision(step)
            if (rt is not None and rt.consent == P.CONFIRM
                    and (step.decision is None
                         or step.decision.consent != P.CONFIRM)):
                audit.event("plan", args={"id": step.id},
                            status="runtime_effect_escalated", detail=rt.reason)
                return {"id": step.id, "status": "escalate", "stage": "runtime",
                        "detail": f"runtime_effect_escalated: {rt.reason}"}

        # Scoped-authorization runtime guards (only when a grant backs this run).
        op = (step.args or {}).get("op", "").lower() if step.kind == "browser" else ""
        if grant_ctx is not None and step.kind == "browser":
            # (Inv4a) BEFORE every non-goto interaction, read the ACTUAL live
            # browser URL and fail closed unless it is inside the approved
            # origins. A page that navigated itself out of scope cannot be acted
            # on. goto is exempted here (its own destination is validated at
            # parse time and its landing is checked below).
            if op != "goto":
                try:
                    _enforce_live_resource(_live_browser_url(),
                                           grant_ctx.required_resources)
                except GrantError as ge:
                    audit.event("step", args={"id": step.id, "kind": step.kind},
                                status="escalate", detail=str(ge)[:200])
                    return {"id": step.id, "status": "escalate",
                            "stage": "live_resource",
                            "detail": f"live_resource_check: {ge}"}
            # (Inv3) Reserve the single-use commit allowance ATOMICALLY,
            # immediately before the first commit-effect dispatch. A second or
            # concurrent task loses the reservation and never dispatches. The
            # reservation stands regardless of the eventual outcome.
            if grant_ctx.step_is_commit(step):
                try:
                    grant_ctx.reserve_commit()
                except GrantError as ge:
                    audit.event("step", args={"id": step.id, "kind": step.kind},
                                status="escalate", detail=str(ge)[:200])
                    return {"id": step.id, "status": "grant_rejected",
                            "stage": "commit_reserve", "detail": str(ge)}

        action_dispatched = True
        result = Eng._dispatch(step.kind, {**step.args, "timeout": step.timeout}, cfg=cfg)
        if step.kind == "shell":
            last_shell_exit = result.get("exit_code")

        # (Inv4b) AFTER a goto, enforce the LANDED final URL against the approved
        # origins. If the navigation escaped scope, the action already happened,
        # so we never re-dispatch — we return outcome_unknown so the brain must
        # re-decide rather than silently continue on an out-of-scope page.
        if grant_ctx is not None and step.kind == "browser" and op == "goto":
            landed = result.get("url") if isinstance(result, dict) else None
            if landed is None:
                landed = _live_browser_url()
            try:
                _enforce_live_resource(landed, grant_ctx.required_resources)
            except GrantError as ge:
                audit.event("step", args={"id": step.id, "kind": step.kind},
                            status="outcome_unknown",
                            detail=f"landed_out_of_scope: {ge}"[:200])
                return {"id": step.id, "status": "outcome_unknown",
                        "stage": "landed_resource",
                        "detail": "navigation dispatched but landed outside the "
                                  f"authorized origins; not repeated: {ge}"}

        # 3) postcondition (verify-after-act) — write steps always have one
        if step.postcondition:
            try:
                _ensure_runnable(rec, gate, "postcondition")
            except ExecutionCancelled as exc:
                return cancelled(exc)
            try:
                ok, detail = Cond.check(step.postcondition, last_shell_exit=last_shell_exit)
            except Exception as e:  # noqa: BLE001
                # Transport death during verification: the action may already
                # have happened and there is nothing to verify against. This
                # is outcome_unknown, never a re-check and never a repeat.
                if not L2.transport_closed(e):
                    raise
                detail = ("postcondition check lost the browser transport; "
                          "the outcome is unknowable and the action was not "
                          f"repeated: {str(e)[:200]}")
                audit.event("step", args={"id": step.id, "kind": step.kind},
                            status="outcome_unknown", detail=detail[:200])
                return {"id": step.id, "status": "outcome_unknown",
                        "stage": "postcondition", "detail": detail}
            if not ok:
                # The action may already have happened. Repeating it could duplicate
                # an irreversible side effect, so only let the world settle and
                # re-observe once before escalating an explicitly unknown outcome.
                try:
                    _ensure_runnable(rec, gate, "postcondition_recovery")
                    time.sleep(0.6)
                    _ensure_runnable(rec, gate, "postcondition_recheck")
                except ExecutionCancelled as exc:
                    return cancelled(exc)
                try:
                    ok, detail2 = Cond.check(step.postcondition,
                                             last_shell_exit=last_shell_exit)
                except Exception as e:  # noqa: BLE001
                    if not L2.transport_closed(e):
                        raise
                    ok, detail2 = False, ("transport lost during re-observation: "
                                          f"{str(e)[:150]}")
                if not ok:
                    audit.event("step", args={"id": step.id},
                                status="outcome_unknown", detail=detail2)
                    return {"id": step.id, "status": "outcome_unknown",
                            "stage": "postcondition",
                            "detail": "action was dispatched but its outcome could "
                                      "not be verified after two observations; "
                                      f"action was not repeated: {detail2}"}

        audit.event("step", args={"id": step.id, "kind": step.kind},
                    status="ok", detail=step.decision.consent if step.decision else "")
        return {"id": step.id, "status": "ok",
                "result": result if step.kind == "shell" else
                          (result if isinstance(result, dict) else {"ok": True})}
    except ExecutionCancelled as exc:
        return cancelled(exc)
    except Exception as e:  # noqa: BLE001
        # Transport death on/after dispatch: the action may already have
        # completed, so the outcome is unknowable — never re-verify against a
        # world we cannot observe, and never repeat the action.
        if action_dispatched and L2.transport_closed(e):
            detail = ("action was dispatched but the browser transport died; "
                      "the outcome is unknowable and the action was not "
                      f"repeated: {str(e)[:200]}")
            audit.event("step", args={"id": step.id, "kind": step.kind},
                        status="outcome_unknown", detail=detail[:200])
            return {"id": step.id, "status": "outcome_unknown",
                    "stage": "action", "detail": detail}
        audit.event("step", args={"id": step.id, "kind": step.kind},
                    status="error", detail=str(e)[:200])
        return {"id": step.id, "status": "error", "stage": "action",
                "detail": str(e)[:300]}


def run(script, *, cfg, audit, consent, mode=None, script_timeout: int = 600,
        gate: Callable[[], bool] | None = None, grant_store=None) -> dict:
    """Top-level: consent gate once, then run all steps. Returns task summary."""
    tid, rec = _new_task(script)

    # --- script-level consent (decided ONCE, bound to hash) ---------------
    if script.blocked_step is not None:
        blk = next(s for s in script.steps if s.id == script.blocked_step)
        audit.event("plan", args={"title": script.title, "hash": script.hash},
                    status="blocked", detail=blk.decision.reason)
        rec.update(status="blocked",
                   error=f"step {blk.id} blocked: {blk.decision.reason}")
        return get_status(tid)

    # --- scoped grant + consent: for an authorization plan WITHOUT a grant_id,
    # the owner MUST approve the concise scope summary BEFORE any grant is
    # issued — regardless of whether the plan's step tiers are only NOTIFY.
    # Reusing an existing grant_id does not re-confirm (the owner already
    # approved that scope) but is still validated by GrantStore.authorize.
    # A denied/timed-out authorization issues NOTHING.
    authorization = getattr(script, "authorization", None)
    needs_scope_consent = (authorization is not None
                           and grant_store is not None
                           and not script.grant_id)

    preview = _script_preview(script)
    task_message_id = None
    grant_ctx: GrantContext | None = None
    bypassed = (script.consent.consent == P.CONFIRM and mode is not None
                and mode.is_bypass())

    def _start_narration() -> int | None:
        if hasattr(consent, "task_started"):
            return consent.task_started(script.title, len(script.steps))
        consent.notify(f"running: {script.title} · 0/{len(script.steps)}")
        return None

    def _audit_start() -> None:
        audit.event("plan", args={"title": script.title, "hash": script.hash,
                                  "steps": len(script.steps),
                                  **({"mode": "bypass"} if bypassed else {})},
                    status="start",
                    detail="confirm bypassed" if bypassed else script.consent.consent)

    # P0-A: consent.confirm returns (approved, ref); a non-empty tuple is always
    # truthy, so raw truthiness treats DENIAL as approval. normalize_consent_result
    # is the single place that invariant lives (engine.act uses the same helper),
    # and any unrecognized shape fails closed.
    if needs_scope_consent:
        scope_summary = authorization.summary(script.hash)
        if not normalize_consent_result(
                consent.confirm(scope_summary, script_hash=script.hash)):
            audit.event("plan", args={"title": script.title, "hash": script.hash},
                        status="denied",
                        detail="owner did not approve scope (or timeout)")
            rec.update(status="denied",
                       error="owner did not approve scope (or timeout)")
            return get_status(tid)
        # Only after approval do we issue the grant.
        g = grant_store.issue(authorization)
        rec["grant_id"] = g.id
        grant_ctx = GrantContext(store=grant_store, grant_id=g.id, holder=tid,
                                 required_resources=set(script.required_resources))
        _audit_start()
        # For a NOTIFY-tier authorization plan, still open the narration bubble.
        if script.consent.consent == P.NOTIFY:
            task_message_id = _start_narration()
    else:
        # Reuse path: validate the existing grant, then apply the normal
        # per-plan consent tier below.
        if authorization is not None and grant_store is not None and script.grant_id:
            try:
                grant_store.authorize(
                    script.grant_id,
                    authorization,
                    required_resources=script.required_resources,
                    required_effects=script.required_effects,
                )
            except GrantError as e:
                audit.event("plan", args={"title": script.title, "hash": script.hash},
                            status="grant_rejected", detail=str(e)[:200])
                rec.update(status="grant_rejected", error=str(e))
                return get_status(tid)
            rec["grant_id"] = script.grant_id
            grant_ctx = GrantContext(store=grant_store, grant_id=script.grant_id,
                                     holder=tid,
                                     required_resources=set(script.required_resources))

        if script.consent.consent == P.CONFIRM and not bypassed:
            if not normalize_consent_result(
                    consent.confirm(preview, script_hash=script.hash)):
                audit.event("plan", args={"title": script.title, "hash": script.hash},
                            status="denied", detail="owner did not approve")
                rec.update(status="denied", error="owner did not approve (or timeout)")
                return get_status(tid)
        elif script.consent.consent == P.NOTIFY:
            task_message_id = _start_narration()

        _audit_start()

    # --- run steps --------------------------------------------------------
    from . import observe as obs
    from . import pixels as L3
    baseline_input = obs.last_input_tick()
    deadline = time.time() + script_timeout

    for i, step in enumerate(script.steps, 1):
        with _TLOCK:
            rec["step"] = i
        if _aborted(rec):
            rec.update(status="aborted")
            audit.event("plan", args={"hash": script.hash}, status="aborted",
                        detail=f"at step {step.id}")
            break
        if time.time() > deadline:
            rec.update(status="timeout", error=f"script timeout at {step.id}")
            break
        # human-collision: real input since baseline that ISN'T Ollie's own L3
        # injection (pixels records its injected tick). A human at the keyboard
        # always outranks Ollie — pause. UIA/shell don't inject; L3 does.
        cur_input = obs.last_input_tick()
        if cur_input != baseline_input and cur_input != L3.last_injected_tick():
            if script.consent.consent != P.NOTIFY:
                consent.notify(f"paused: you used the box during '{script.title}'. "
                               f"Stopped before step {step.id}.")
            rec.update(status="paused_collision",
                       error=f"human input detected before {step.id}")
            audit.event("plan", args={"hash": script.hash},
                        status="paused_collision", detail=step.id)
            break
        baseline_input = cur_input  # absorb Ollie's own injection

        outcome = _run_step(step, cfg=cfg, audit=audit, rec=rec, gate=gate,
                            grant_ctx=grant_ctx)
        with _TLOCK:
            rec["results"].append(outcome)

        if outcome["status"] == "grant_rejected":
            rec.update(status="grant_rejected",
                       error=f"step {step.id} {outcome.get('stage', '')}: "
                             f"{outcome['detail']}")
            audit.event("plan", args={"hash": script.hash},
                        status="grant_rejected",
                        detail=outcome.get("detail", "")[:200])
            _finish_narration(consent, script, rec, task_message_id)
            return get_status(tid)

        if outcome["status"] == "cancelled":
            rec.update(status="aborted",
                       error=f"step {step.id} {outcome['stage']}: "
                             f"{outcome['detail']}")
            audit.event("plan", args={"hash": script.hash}, status="aborted",
                        detail=outcome.get("detail", "")[:200])
            _finish_narration(consent, script, rec, task_message_id)
            return get_status(tid)

        if outcome["status"] == "outcome_unknown":
            # The single-use commit allowance is reserved/consumed atomically
            # BEFORE the dispatch (see _run_step), so an uncertain outcome never
            # needs to consume here — the allowance is already spent for good and
            # cannot be re-used by any later or concurrent commit.
            rec.update(status="outcome_unknown",
                       error=f"step {step.id} {outcome['stage']}: "
                             f"{outcome['detail']}")
            audit.event("plan", args={"hash": script.hash},
                        status="outcome_unknown",
                        detail=outcome.get("detail", "")[:200])
            _finish_narration(consent, script, rec, task_message_id)
            return get_status(tid)

        if outcome["status"] in ("escalate", "error"):
            if step.on_fail == "abort" and outcome["stage"] == "precondition":
                rec.update(status="aborted",
                           error=f"step {step.id} precondition failed: "
                                 f"{outcome['detail']}")
            else:
                rec.update(status="escalated",
                           error=f"step {step.id} {outcome['stage']}: "
                                 f"{outcome['detail']}")
            audit.event("plan", args={"hash": script.hash}, status=rec["status"],
                        detail=outcome.get("detail", "")[:200])
            _finish_narration(consent, script, rec, task_message_id)
            return get_status(tid)

        if step.checkpoint:
            rec.update(status="checkpoint",
                       error=f"checkpoint after {step.id} — brain to continue")
            _finish_narration(consent, script, rec, task_message_id)
            return get_status(tid)

    else:  # loop completed without break
        rec.update(status="ok")
        audit.event("plan", args={"hash": script.hash}, status="ok",
                    detail=f"{len(script.steps)} steps")

    _finish_narration(consent, script, rec, task_message_id)
    return get_status(tid)
