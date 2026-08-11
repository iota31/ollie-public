"""Engine glue: one entry point that takes a single action, runs it through
the hard policy gate + consent, dispatches to the right rung, and audits.

This is the choke point every actuation passes through (plan §D6: one engine
owns ALL host actions). The MCP `act` tool is a thin wrapper over `act_step`.
"""

from __future__ import annotations

import hashlib
import json

import time

from . import policy as P
from . import shell as L0
from . import uia_actions as L1
from . import browser as L2
from . import pixels as L3
from . import grounding as G
from . import vault as Vault
from . import captcha as Cap
from .consent import normalize_consent_result

# Taint set for secret targets: tuples of (automation_id or "", name or "", window_title or "")
# When a secret is typed into a UIA target, we record its identity here.
# get_text / clipboard_read on a tainted target must refuse or mask.
_SECRET_TAINTED: set[tuple[str, str, str]] = set()


def _taint_key_from_find_kw(find_kw: dict) -> tuple[str, str, str]:
    """Build a stable key for a UIA target from the same keys used in find."""
    return (
        find_kw.get("automation_id") or "",
        find_kw.get("name") or "",
        find_kw.get("window_title") or "",
    )


def _is_tainted_target(find_kw: dict) -> bool:
    return _taint_key_from_find_kw(find_kw) in _SECRET_TAINTED


def _taint_target(find_kw: dict) -> None:
    _SECRET_TAINTED.add(_taint_key_from_find_kw(find_kw))


class Refused(Exception):
    """Raised when policy blocks or the owner denies an action."""


def _preview(kind: str, p: dict) -> str:
    if kind == "shell":
        return f"run shell: {p.get('command', '')[:300]}"
    if kind == "uia":
        tgt = p.get("name") or p.get("automation_id") or p.get("control_type")
        return f"{p.get('op')} UI element '{tgt}' in '{p.get('window_title', '')}'"
    if kind == "window":
        return f"window {p.get('op')} on '{p.get('title', '')}'"
    if kind == "clipboard":
        return f"clipboard {p.get('op')}"
    if kind == "browser":
        op = p.get("op", "")
        tgt = p.get("url") or p.get("selector") or ""
        return f"browser {op} {tgt}"[:200]
    if kind == "pixels":
        op = p.get("op", "")
        if op == "drag":
            return (f"pixels drag ({p.get('x')},{p.get('y')})->"
                    f"({p.get('x2')},{p.get('y2')})")
        if op in ("type_text", "key"):
            # secret_ref: mask the value in previews (never show plaintext)
            if p.get("secret_ref"):
                return f"pixels {op} secret_ref={p.get('secret_ref')}"
            return f"pixels {op} {p.get('value') or p.get('key') or ''}"[:200]
        return f"pixels {op} {p.get('x', '')},{p.get('y', '')}"[:200]
    if kind == "captcha":
        t = p.get("task") or {}
        typ = t.get("type") or "captcha"
        site = t.get("websiteURL") or t.get("websiteKey") or ""
        return f"captcha {typ} {site}"[:200]
    return f"{kind} {p}"


def _classify(kind: str, p: dict) -> P.Decision:
    if kind == "shell":
        return P.classify_action("shell", command=p.get("command", ""), cwd=p.get("cwd"),
                                 external=bool(p.get("external")),
                                 commit=bool(p.get("commit")), effect=p.get("effect"))
    if kind == "clipboard":
        op = (p.get("op") or "").lower()
        return P.classify_action(
            "clipboard_read" if op == "read" else "clipboard_write", op)
    if kind == "browser":
        op = (p.get("op") or "").lower()
        target_text = ""
        # for a click, resolve the target's text so a commit button (send/buy
        # /post...) escalates to confirm even if the planner didn't flag it
        if op == "click" and p.get("selector"):
            try:
                target_text = L2.element_text(p["selector"])
            except Exception:
                target_text = ""
        return P.classify_browser(op, commit=bool(p.get("commit")),
                                  target_text=target_text, effect=p.get("effect"),
                                  key=p.get("key", ""))
    if kind == "captcha":
        return P.classify_action("captcha", "", commit=bool(p.get("commit")), cwd=None)
    return P.classify_action(kind, p.get("op", ""),
                             external=bool(p.get("external")),
                             commit=bool(p.get("commit")), effect=p.get("effect"),
                             key=p.get("key", ""))


def _resolve_secret_if_any(p: dict) -> str | None:
    """If secret_ref is present and valid, return the resolved secret value.
    Never returns a secret if the ref is invalid (caller should treat as error)."""
    ref = p.get("secret_ref")
    if not ref:
        return None
    if not Vault.valid_ref(ref):
        raise Refused(f"invalid secret_ref: {ref!r}")
    return Vault.get(ref)


def _dispatch(kind: str, p: dict, *, cfg=None) -> dict:
    if kind == "shell":
        return L0.run(p["command"], cwd=p.get("cwd") or None,
                      timeout=int(p.get("timeout", 60)))
    if kind == "uia":
        op = (p.get("op") or "").lower()
        find_kw = {k: p[k] for k in ("name", "control_type", "automation_id",
                                     "window_title") if p.get(k)}
        if op == "get_text":
            if _is_tainted_target(find_kw):
                # HIGH-1: refuse readback from a secret-tainted target
                raise Refused("get_text refused on secret-tainted target")
            return L1.get_text(**find_kw)
        if op == "locate":  # tiered grounding: UIA first, then vision
            return G.locate(name=p.get("name", ""),
                            query=p.get("value", ""),
                            control_type=p.get("control_type", ""),
                            window_title=p.get("window_title", ""))
        if op == "locate_vision":  # force the vision tier (eval / canvas targets)
            return G.locate_vision(p.get("value", "") or p.get("name", ""))
        if op == "invoke":
            return L1.invoke(**find_kw)
        if op == "set_value":
            val = _resolve_secret_if_any(p)
            if val is not None:
                _taint_target(find_kw)  # HIGH-1: mark target as secret-tainted
                return L1.set_value(val, **find_kw)
            return L1.set_value(p.get("value", ""), **find_kw)
        if op == "type_text":
            val = _resolve_secret_if_any(p)
            if val is not None:
                _taint_target(find_kw)  # HIGH-1: mark target as secret-tainted
                return L1.type_text(val, **find_kw, _secret=True)
            return L1.type_text(p.get("value", ""), **find_kw)
        raise Refused(f"unknown uia op: {op}")
    if kind == "window":
        return L1.window_op(p.get("op", ""), p.get("title", ""),
                            x=int(p.get("x", 0)), y=int(p.get("y", 0)),
                            width=int(p.get("width", 0)),
                            height=int(p.get("height", 0)))
    if kind == "clipboard":
        op = (p.get("op") or "").lower()
        if op == "read":
            # HIGH-1 + clipboard residue: if any secret taint exists, refuse clipboard read
            # (coarse but safe; we don't track clipboard provenance here)
            if _SECRET_TAINTED:
                raise Refused("clipboard read refused after secret typing")
            return L1.clipboard_read()
        return L1.clipboard_write(p.get("value", "") or p.get("text", ""))
    if kind == "browser":
        op = (p.get("op") or "").lower()
        if op == "goto":
            return L2.goto(p.get("url", ""), timeout=int(p.get("timeout", 30)))
        if op == "extract":
            return L2.extract(p.get("selector", ""), timeout=int(p.get("timeout", 15)))
        if op == "links":
            return L2.links(int(p.get("limit", 40)))
        if op == "screenshot":
            return L2.screenshot(p.get("path") or p.get("save_path", ""))
        if op == "get_attr":
            return L2.get_attr(p.get("selector", ""), p.get("attr", ""))
        if op == "property_matches":
            return L2.property_matches(
                p.get("selector", ""), p.get("property", ""),
                equals=p.get("equals"), contains=p.get("contains"),
                nonempty=bool(p.get("nonempty")),
            )
        if op == "click":
            return L2.click(p.get("selector", ""), timeout=int(p.get("timeout", 15)))
        if op == "fill":
            val = _resolve_secret_if_any(p)
            if val is not None:
                return L2.fill(p.get("selector", ""), val,
                               timeout=int(p.get("timeout", 15)))
            return L2.fill(p.get("selector", ""), p.get("value", ""),
                           timeout=int(p.get("timeout", 15)))
        if op == "type_text":
            val = _resolve_secret_if_any(p)
            if val is not None:
                return L2.type_text(p.get("selector", ""), val,
                                    timeout=int(p.get("timeout", 15)))
            return L2.type_text(p.get("selector", ""), p.get("value", ""),
                                timeout=int(p.get("timeout", 15)))
        if op == "press":
            return L2.press(p.get("key", ""))
        if op == "status":
            return L2.status()
        if op == "element_text":
            return L2.element_text(p.get("selector", ""))
        raise Refused(f"unknown browser op: {op}")
    if kind == "pixels":
        op = (p.get("op") or "").lower()
        if op == "move":
            return L3.move(p.get("x", 0), p.get("y", 0))
        if op == "click":
            return L3.click(p.get("x", 0), p.get("y", 0),
                            button=p.get("button", "left"),
                            double=bool(p.get("double")))
        if op == "drag":
            return L3.drag(p.get("x", 0), p.get("y", 0),
                           p.get("x2", 0), p.get("y2", 0),
                           button=p.get("button", "left"))
        if op == "scroll":
            return L3.scroll(int(p.get("amount", 0)),
                             horizontal=bool(p.get("horizontal")))
        if op == "type_text":
            val = _resolve_secret_if_any(p)
            if val is not None:
                return L3.type_text(val, _secret=True)
            return L3.type_text(p.get("value", "") or p.get("text", ""))
        if op == "key":
            return L3.key(p.get("key", ""))
        if op == "cursor_pos":
            return L3.cursor_pos()
        raise Refused(f"unknown pixels op: {op}")
    if kind == "captcha":
        # External solve — the 'task' dict is passed through as-is.
        task = p.get("task") or {}
        if not isinstance(task, dict) or not task:
            raise Refused("captcha solve requires a non-empty 'task' dict")
        # The engine resolves the key (host-only). Do NOT accept a key from the caller.
        # cfg may be passed explicitly (from act_step) or resolved from the module global
        # (populated by server at import time for executor paths).
        c = cfg if cfg is not None else globals().get("cfg")
        key = (getattr(c, "nocaptcha_api_key", "") if c is not None else "").strip()
        if not key:
            raise Refused("noCaptchaAI key is not configured on the host")
        # Optional overrides from params (timeout, etc.) with sane defaults.
        to = int(p.get("timeout", 90))
        res = Cap.solve(task, client_key=key, timeout=to)
        return res
    raise Refused(f"unknown action kind: {kind}")


def _is_secret_step(params: dict) -> bool:
    """True if this step will type a secret (so we must suppress screenshots
    on its audit record and keep the value out of previews/args)."""
    return bool(params.get("secret_ref"))


def _audit_args(kind: str, preview: str, params: dict, is_secret: bool) -> dict:
    """Build the args dict for act audit records.
    For secret steps, explicitly record secret_ref + a masked value marker
    so the trail shows the ref was used without ever containing the plaintext."""
    a = {"kind": kind, "preview": preview}
    if is_secret:
        a["secret_ref"] = params.get("secret_ref")
        a["value"] = "***"
    return a


def act_step(kind: str, params: dict, *, cfg, audit, consent, mode=None) -> dict:
    """Policy-gate + consent + dispatch + audit a single action.

    Returns a result dict (always JSON-serialisable). Refusals are returned,
    not raised, so the caller/brain gets a structured 'no'.
    """
    kind = (kind or "").lower()
    hands_enabled = getattr(cfg, "hands_enabled", None)
    if callable(hands_enabled) and not hands_enabled():
        audit.event("act", args={"kind": kind}, status="refused",
                    detail="hands disabled")
        return {"action": kind, "status": "blocked", "error": "hands disabled"}
    decision = _classify(kind, params)
    preview = _preview(kind, params)
    base = {"action": kind, "preview": preview, "policy": decision.to_dict()}

    # Screenshot suppression for secret entry: never attach a shot to the
    # act audit record when a secret_ref is being resolved inside _dispatch.
    # (observe() still works; this only gates shots tied to *this* act event.)
    secret_step = _is_secret_step(params)
    shot_for_audit: str | None = None  # always None for secret steps (future-proof)

    if decision.consent == P.BLOCKED:
        audit.event("act", args={"kind": kind, "preview": preview},
                    status="blocked", detail=decision.reason,
                    screenshot=shot_for_audit)
        return {**base, "status": "blocked",
                "error": f"blocked by policy: {decision.reason}"}

    # Captcha preflight: if kind is captcha and host has no key, block cleanly
    # (no network attempt; consistent "blocked" UX for missing capability).
    if kind == "captcha":
        k = (getattr(cfg, "nocaptcha_api_key", "") or "").strip()
        if not k:
            audit.event("act", args={"kind": kind, "preview": preview},
                        status="blocked", detail="noCaptchaAI key not configured",
                        screenshot=shot_for_audit)
            return {**base, "status": "blocked",
                    "error": "noCaptchaAI key is not configured on the host"}

    bypassed = decision.consent == P.CONFIRM and mode is not None and mode.is_bypass()
    if decision.consent == P.CONFIRM and not bypassed:
        canon = json.dumps({"kind": kind, "params": params}, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False)
        action_digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()
        consent_result = consent.confirm(preview, script_hash=action_digest)
        approved = normalize_consent_result(consent_result)
        if not approved:
            audit.event("act", args={"kind": kind, "preview": preview},
                        status="denied", detail="owner denied / timeout",
                        screenshot=shot_for_audit)
            return {**base, "status": "denied",
                    "error": "owner did not approve (or timed out)"}

    t0 = time.monotonic()
    try:
        result = _dispatch(kind, params, cfg=cfg)
    except Exception as e:
        audit.event("act", args={"kind": kind, "preview": preview},
                    status="error", detail=str(e)[:300],
                    screenshot=shot_for_audit)
        return {**base, "status": "error", "error": str(e)[:500]}

    audit.event("act", args={**_audit_args(kind, preview, params, secret_step),
                             **({"mode": "bypass"} if bypassed else {})},
                status="ok", duration_ms=int((time.monotonic() - t0) * 1000),
                detail="confirm bypassed" if bypassed else decision.consent,
                screenshot=shot_for_audit)

    if decision.consent == P.NOTIFY:
        consent.notify(f"did: {preview}")

    # MED-1: for secret steps, mask the returned result as well (no element.name,
    # no typed_len, etc.). Keep the structure shape but drop sensitive keys.
    if secret_step:
        masked_result = None
        if isinstance(result, dict):
            masked_result = {k: v for k, v in result.items()
                             if k not in ("element", "typed_len")}
            result = masked_result

    # For captcha solves, ensure the audit preview/args never saw a key
    # (the key is resolved on the host in _dispatch and never present in params).
    if kind == "captcha":
        base["action"] = "captcha"
        # result may contain tokens/answers; pass through (caller injects).
        return {**base, "status": "ok", "result": result}

    return {**base, "status": "ok", "result": result}
