"""Act-script schema v1 + validation + script-level classification (plan §D2).

The brain emits a JSON *plan*; the engine runs it locally at machine speed
(no model call per deterministic step). Consent is decided ONCE for the whole
script (the highest tier among its steps) and bound to a stable hash, so a
materially different script re-consents.

Schema (v1):
{
  "title": "rename today's screenshots",
  "steps": [
    {
      "id": "s1",                       # unique within the script
      "kind": "shell|uia|window|clipboard",
      "args": { ... same fields as the `act` tool ... },
      "preconditions": [ {type, ...}, ... ],   # checked BEFORE the action
      "postcondition": {type, ...},            # REQUIRED on write steps
      "on_fail": "retry|repair|escalate|abort",# default escalate
      "checkpoint": false,              # if true, pause for the brain after
      "timeout": 30                     # per-step seconds (optional)
    }, ...
  ]
}
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Literal, NotRequired, TypedDict

from . import policy as P
from .grants import EFFECTS, GrantError, Scope, canonical_resource

VALID_KINDS = {"shell", "uia", "window", "clipboard", "browser", "captcha", "pixels"}
VALID_ON_FAIL = {"retry", "repair", "escalate", "abort"}
SUPPORTED_CONDITION_TYPES = frozenset({
    "foreground", "window_exists", "window_absent", "uia_exists",
    "uia_absent", "uia_text", "file_exists", "file_absent",
    "shell_exit_zero", "web_url", "web_text", "web_property",
})
# write kinds/ops that MUST declare a postcondition (verify-after-act)
_WRITE_UIA_OPS = {"invoke", "set_value", "type_text"}
_WRITE_BROWSER_OPS = {"click", "fill", "type_text", "press", "select", "submit"}


def browser_step_effect_and_resource(step_id: str, args: dict) -> tuple[str, str | None]:
    """Return the mechanically derived browser effect and requested origin.

    Does NOT require or reject effects - that is a policy decision.
    Missing effects on writes are classified CONFIRM by classify_browser.
    Incompatible effects are also classified CONFIRM by classify_browser.
    """
    op = (args.get("op") or "").lower()
    effect = args.get("effect")
    category, _commit, valid = P._effect(effect)
    if not valid:
        raise ScriptError(f"step {step_id}: invalid browser effect envelope")
    # Only validate category is in EFFECTS if one was provided
    if category is not None and category not in EFFECTS:
        raise ScriptError(f"step {step_id}: browser {op} requires a valid effect category")
    derived_effect = category or ("navigation" if op == "goto" else "observe")
    resource = None
    if args.get("url") is not None:
        try:
            resource = canonical_resource(args["url"])
        except GrantError as exc:
            raise ScriptError(f"step {step_id}: {exc}") from exc
    return derived_effect, resource


class ConditionInput(TypedDict):
    """Minimal typed condition for wire schema; engine parse() is strict."""
    type: str
    equals: NotRequired[str]
    contains: NotRequired[str]
    selector: NotRequired[str]
    property: NotRequired[str]
    nonempty: NotRequired[bool]


class PlanStepInput(TypedDict):
    """Strong tool-input schema; runtime validation still belongs to parse()."""

    id: str
    kind: Literal[
        "shell", "uia", "window", "clipboard", "browser", "pixels", "captcha"
    ]
    args: dict
    preconditions: NotRequired[list[ConditionInput]]
    postcondition: NotRequired[ConditionInput | None]
    on_fail: NotRequired[Literal["retry", "repair", "escalate", "abort"]]
    checkpoint: NotRequired[bool]
    timeout: NotRequired[int]


class AuthorizationInput(TypedDict, total=False):
    """Permissive at schema layer; engine parse() via Scope is strict."""
    family: str
    resources: list[str]
    effects: list[Literal[
        "observe", "navigation", "session_preference", "draft", "progress",
        "external_commit", "identity_commit", "destructive",
    ]]
    ttl_seconds: int
    grant_id: str


class ScriptError(ValueError):
    """Raised when an act-script is malformed."""


@dataclass
class Step:
    id: str
    kind: str
    args: dict
    preconditions: list = field(default_factory=list)
    postcondition: dict | None = None
    on_fail: str = "escalate"
    checkpoint: bool = False
    timeout: int = 30
    decision: P.Decision | None = None  # filled by classify

    def is_write(self) -> bool:
        if self.kind == "shell":
            return self.decision is not None and self.decision.consent != P.AUTO
        if self.kind == "uia":
            return (self.args.get("op") or "").lower() in _WRITE_UIA_OPS
        if self.kind == "window":
            return (self.args.get("op") or "").lower() not in {"", "focus"}
        if self.kind == "clipboard":
            return (self.args.get("op") or "").lower() == "write"
        if self.kind == "browser":
            return (self.args.get("op") or "").lower() in _WRITE_BROWSER_OPS
        if self.kind == "captcha":
            # a solve itself is a side-effect (external credit usage); treat as write for postcond requirement
            return True
        if self.kind == "pixels":
            return (self.args.get("op") or "").lower() not in {"cursor_pos"}
        return True


@dataclass
class Script:
    title: str
    steps: list[Step]
    raw: dict
    consent: P.Decision  # overall (max tier)
    blocked_step: str | None  # id of the first blocked step, if any
    authorization: Scope | None = None
    grant_id: str = ""
    required_resources: set[str] = field(default_factory=set)
    required_effects: set[str] = field(default_factory=set)

    @property
    def hash(self) -> str:
        canon = json.dumps(self.raw, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode()).hexdigest()


def _classify_step(step: Step) -> P.Decision:
    if step.kind == "shell":
        return P.classify_action(
            "shell", command=step.args.get("command", ""),
            external=bool(step.args.get("external")),
            commit=bool(step.args.get("commit")), effect=step.args.get("effect"))
    if step.kind == "clipboard":
        op = (step.args.get("op") or "").lower()
        return P.classify_action(
            "clipboard_read" if op == "read" else "clipboard_write", op)
    if step.kind == "uia":
        op = (step.args.get("op") or "").lower()
        # type_text is a write verb the policy treats like set_value
        return P.classify_action(
            "uia", "set_value" if op == "type_text" else op,
            external=bool(step.args.get("external")),
            commit=bool(step.args.get("commit")), effect=step.args.get("effect"))
    if step.kind == "browser":
        return P.classify_browser((step.args.get("op") or "").lower(),
                                  commit=bool(step.args.get("commit")),
                                  effect=step.args.get("effect"),
                                  key=step.args.get("key", ""))
    if step.kind == "captcha":
        return P.classify_action("captcha", "", commit=bool(step.args.get("commit")))
    if step.kind == "pixels":
        op = (step.args.get("op") or "").lower()
        return P.classify_action(
            "pixels", op, external=bool(step.args.get("external")),
            commit=bool(step.args.get("commit")), effect=step.args.get("effect"),
            key=step.args.get("key", ""))
    return P.classify_action(
        step.kind, step.args.get("op", ""),
        external=bool(step.args.get("external")),
        commit=bool(step.args.get("commit")), effect=step.args.get("effect"))


_TIER_ORDER = {P.AUTO: 0, P.NOTIFY: 1, P.CONFIRM: 2, P.BLOCKED: 3}


def parse(plan: dict) -> Script:
    """Validate a plan dict into a Script, classifying every step."""
    if not isinstance(plan, dict):
        raise ScriptError("plan must be an object")
    steps_raw = plan.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ScriptError("plan.steps must be a non-empty array")

    authorization = None
    grant_id = ""
    if plan.get("authorization") is not None:
        try:
            authorization = Scope.parse(plan["authorization"])
        except GrantError as exc:
            raise ScriptError(str(exc)) from exc
        grant_id = str(plan["authorization"].get("grant_id") or "")

    seen_ids: set[str] = set()
    steps: list[Step] = []
    required_resources: set[str] = set()
    required_effects: set[str] = set()
    overall = P.Decision(P.AUTO if False else "T0", P.AUTO, "empty")
    blocked_id: str | None = None

    for i, sr in enumerate(steps_raw):
        if not isinstance(sr, dict):
            raise ScriptError(f"step {i} must be an object")
        sid = str(sr.get("id") or f"s{i + 1}")
        if sid in seen_ids:
            raise ScriptError(f"duplicate step id: {sid}")
        seen_ids.add(sid)
        kind = (sr.get("kind") or "").lower()
        if kind not in VALID_KINDS:
            raise ScriptError(f"step {sid}: invalid kind {kind!r}")
        on_fail = (sr.get("on_fail") or "escalate").lower()
        if on_fail not in VALID_ON_FAIL:
            raise ScriptError(f"step {sid}: invalid on_fail {on_fail!r}")
        args = sr.get("args") or {}
        if not isinstance(args, dict):
            raise ScriptError(f"step {sid}: args must be an object")
        derived_effect = None
        derived_resource = None
        if kind == "browser":
            derived_effect, derived_resource = browser_step_effect_and_resource(sid, args)
        pre = sr.get("preconditions") or []
        if not isinstance(pre, list):
            raise ScriptError(f"step {sid}: preconditions must be an array")
        post = sr.get("postcondition")
        if post is not None and not isinstance(post, dict):
            raise ScriptError(f"step {sid}: postcondition must be an object")
        for label, conditions in (("precondition", pre),
                                  ("postcondition", [post] if post else [])):
            for condition in conditions:
                if not isinstance(condition, dict):
                    raise ScriptError(
                        f"step {sid}: {label} must be an object")
                condition_type = (condition.get("type") or "").lower()
                if condition_type not in SUPPORTED_CONDITION_TYPES:
                    raise ScriptError(
                        f"step {sid}: unsupported {label} type "
                        f"{condition_type!r}")

        step = Step(id=sid, kind=kind, args=args, preconditions=pre,
                    postcondition=post, on_fail=on_fail,
                    checkpoint=bool(sr.get("checkpoint", False)),
                    timeout=int(sr.get("timeout", 30)))
        step.decision = _classify_step(step)
        if authorization is not None:
            if kind != "browser":
                raise ScriptError("scoped authorization currently supports browser steps only")
            required_effects.add(derived_effect)
            if derived_resource:
                required_resources.add(derived_resource)
        is_blocked = step.decision.consent == P.BLOCKED
        if is_blocked and blocked_id is None:
            blocked_id = sid

        # write steps MUST verify themselves (the verify-after-act rule) — but a
        # BLOCKED step never executes, so don't mask the policy block behind a
        # 'missing postcondition' schema error (the block reason must win).
        if not is_blocked and step.is_write() and not step.postcondition:
            raise ScriptError(
                f"step {sid}: write actions require a postcondition "
                f"(verify-after-act). Add one (e.g. uia_text/file_exists).")

        if _TIER_ORDER[step.decision.consent] > _TIER_ORDER[overall.consent]:
            overall = step.decision
        steps.append(step)

    if authorization is not None:
        if not required_resources:
            # Repair plans operating on the current page are still bound to the
            # approved resource envelope; executor verifies live URL.
            required_resources = set(authorization.resources)
        if not required_resources.issubset(authorization.resources):
            raise ScriptError("plan contains a browser URL outside authorization.resources")
        if not required_effects.issubset(authorization.effects):
            raise ScriptError("plan effects exceed authorization.effects")
    return Script(title=str(plan.get("title") or "untitled"), steps=steps,
                  raw=plan, consent=overall, blocked_step=blocked_id,
                  authorization=authorization, grant_id=grant_id,
                  required_resources=required_resources,
                  required_effects=required_effects)
