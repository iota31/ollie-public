"""Consent + owner notification (plan §D3, §Safety).

Engine-OWNED, deliberately independent of the brain: a hijacked brain can
neither forge an approval nor suppress a notification, because this code —
not the LLM — talks to Telegram and gates the action.

- notify  : one-way Telegram sendMessage. Safe: it does NOT call getUpdates,
            so it never collides with the gateway's bot long-poll.
- confirm : send a prompt with a high-entropy one-time challenge and the
            action digest, then BLOCK until the owner approves via the
            approval-credential-authenticated /consent endpoint. On
            timeout we DENY. We never auto-approve.

The owner->/consent relay is independent of the MCP caller: it needs the
approval-only credential and must echo both challenge and digest.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# The only ref shape this engine ever mints (see begin_confirm). Anything else
# is malformed by construction, so it is rejected at resolve() and is never
# echoed verbatim into the audit trail.
HANDS_REF_RE = re.compile(r"^H-[A-Za-z0-9_-]{1,61}$")


def _audit_ref(ref: str) -> str:
    """Sanitize a caller-supplied ref before it reaches the audit trail.

    Refs arrive from the network. Logging one verbatim would let a caller
    inject arbitrary text (newlines, fake key=value pairs) into audit records
    that operators and the watchdog read. Anything that is not a well-formed
    H-ref is logged as the empty string instead.
    """
    return ref if isinstance(ref, str) and HANDS_REF_RE.fullmatch(ref) else ""


@dataclass
class ConsentRequest:
    """Returned by await_confirm to describe a pending approval challenge."""
    ref: str
    preview: str
    approved: bool = False


@dataclass
class _SendResult:
    """Internal classification for _send_with_result outcomes."""
    success: bool
    message_id: int | None = None
    definitive_rejection: bool = False
    ambiguous_failure: bool = False
    http_status: int | None = None
    classification: str = "unknown"


class PendingConsent:
    def __init__(self, code: str, preview: str, action_digest: str,
                 expires_at: float) -> None:
        self.code = code
        self.preview = preview
        self.action_digest = action_digest
        self.event = threading.Event()
        self.approved = False
        self.expires_at = expires_at

    @property
    def ref(self) -> str:
        """H-ref alias for the pending code (used by inline approval flow)."""
        return self.code


class Consent:
    _SendResult = _SendResult

    def __init__(self, cfg, audit) -> None:
        self.cfg = cfg
        self.audit = audit
        self._pending: dict[str, PendingConsent] = {}
        self._lock = threading.Lock()
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    # ----------------------------------------------------------- telegram ---
    def _send(self, text: str) -> int | None:
        token = self.cfg.telegram_bot_token
        chat = self.cfg.owner_chat_id
        if not token or not chat:
            return False
        data = urllib.parse.urlencode({
            "chat_id": chat, "text": text, "disable_web_page_preview": "true",
        }).encode()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            with urllib.request.urlopen(url, data=data, timeout=10) as resp:
                if resp.status != 200:
                    return None
                payload = json.loads(resp.read().decode("utf-8"))
                return payload.get("result", {}).get("message_id")
        except Exception:
            return None

    def _edit(self, message_id: int, text: str) -> bool:
        """Replace a task-status bubble; callers fall back to a new message."""
        token = self.cfg.telegram_bot_token
        chat = self.cfg.owner_chat_id
        if not token or not chat:
            return False
        data = urllib.parse.urlencode({
            "chat_id": chat, "message_id": message_id, "text": text,
            "disable_web_page_preview": "true",
        }).encode()
        url = f"https://api.telegram.org/bot{token}/editMessageText"
        try:
            with urllib.request.urlopen(url, data=data, timeout=10) as resp:
                return resp.status == 200
        except Exception:
            return False

    # ------------------------------------------------------------- notify ---
    def notify(self, message: str) -> None:
        """Fire-and-forget narration to the owner."""
        self._send(f"🤖 Ollie (hands): {message}")

    def task_started(self, title: str, steps: int) -> int | None:
        """Create the one owner-visible status bubble for a narrated plan."""
        message = f"running: {title} · 0/{steps}"
        message_id = self._send(f"🤖 Ollie (hands): {message}")
        return message_id

    def task_finished(self, message_id: int | None, title: str, *, status: str,
                      step: int, total: int, detail: str = "") -> None:
        """Update the start bubble in place, avoiding start/done message spam."""
        labels = {"ok": "done", "escalated": "stopped",
                  "outcome_unknown": "uncertain", "aborted": "aborted",
                  "timeout": "timed out", "checkpoint": "checkpoint",
                  "paused_collision": "paused"}
        label = labels.get(status, status)
        suffix = f" · {detail[:140]}" if detail else ""
        text = f"🤖 Ollie (hands): {label}: {title} · {step}/{total}{suffix}"
        edited = bool(message_id) and self._edit(message_id, text)
        if not edited:
            self._send(text)

    # ------------------------------------------------------------ confirm ---
    # NOTE: `confirm` is defined ONCE, below, in the H-ref pending API section.
    # A second, earlier definition used to live here and was silently shadowed
    # (Python keeps the last binding), so it was dead code that could mislead a
    # reader into thinking the plain-text challenge flow was still live.

    def resolve(self, code: str, approve: bool, script_hash: str = "",
                client_id: str = "unknown", enable_bypass=None) -> dict:
        """Atomically consume one approval challenge after all checks pass."""
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts[client_id]
            window = int(getattr(self.cfg, "approval_rate_limit_window", 60))
            limit = int(getattr(self.cfg, "approval_rate_limit_attempts", 12))
            while attempts and attempts[0] <= now - window:
                attempts.popleft()
            safe_ref = _audit_ref(code)
            if len(attempts) >= limit:
                self.audit.event("confirm", args={"ref": safe_ref, "stage": "resolve"},
                                 status="rate_limited", detail=f"ref={safe_ref}")
                return {"ok": False, "error": "rate limit exceeded",
                        "error_code": "rate_limited"}
            attempts.append(now)

            pc = self._pending.get(code)
            if pc is None or now >= pc.expires_at:
                self._pending.pop(code, None)
                self.audit.event("confirm", args={"ref": safe_ref, "stage": "resolve"},
                                 status="missing" if pc is None else "expired",
                                 detail=f"ref={safe_ref}")
                return {"ok": False, "error": "unknown or expired code",
                        "error_code": "unknown_or_expired"}
            # A challenge bound to an action digest can only be resolved by
            # presenting that exact digest; the ref alone is not sufficient.
            # (The relay obtains the digest from the owner-typed reply or from
            # the approval-token-authenticated inventory.)
            if pc.action_digest:
                if not script_hash:
                    self.audit.event("confirm", args={"ref": safe_ref, "stage": "resolve"},
                                     status="digest_required", detail=f"ref={safe_ref}")
                    return {"ok": False, "error": "action digest required",
                            "error_code": "digest_required"}
                if not secrets.compare_digest(script_hash, pc.action_digest):
                    self.audit.event("confirm", args={"ref": safe_ref, "stage": "resolve"},
                                     status="digest_mismatch", detail=f"ref={safe_ref}")
                    return {"ok": False, "error": "action digest mismatch",
                            "error_code": "digest_mismatch"}
            # Pop under the same lock as lookup: exactly one concurrent
            # resolver can consume this challenge.
            self._pending.pop(code)
            pc.approved = approve
            if enable_bypass is not None:
                enable_bypass()
        self.audit.event("confirm", args={"ref": safe_ref, "stage": "resolve",
                                          "mode": "bypass" if enable_bypass else "normal"},
                         status="approved" if approve else "denied",
                         detail=f"ref={safe_ref}")
        pc.event.set()
        return {"ok": True, "code": code, "approved": approve,
                "mode": "bypass" if enable_bypass else "normal"}

    # --------------------------------------------------- H-ref pending API ---

    @staticmethod
    def _build_approval_keyboard(ref: str):
        """Build inline keyboard markup for H-ref approvals. Returns None for overlong refs."""
        if not isinstance(ref, str) or not ref:
            return None
        # Telegram callback_data limit is 64 bytes. Prefix is "ollie_approval:v1:a:" (21) or "d" (21).
        # Total payload must be <= 64, so ref portion must be short.
        a_payload = f"ollie_approval:v1:a:{ref}"
        b_payload = f"ollie_approval:v1:b:{ref}"
        d_payload = f"ollie_approval:v1:d:{ref}"
        if max(map(len, (a_payload, b_payload, d_payload))) > 64:
            return None
        return {
            "inline_keyboard": [
                [{"text": "Approve once", "callback_data": a_payload},
                 {"text": "Deny", "callback_data": d_payload}],
                [{"text": "Enable bypass & continue", "callback_data": b_payload}],
            ]
        }

    def _send_with_result(self, text: str, reply_markup=None):
        """Send a message and classify the outcome for fallback decisions.

        Returns _SendResult with:
          - success + message_id on success
          - definitive_rejection=True for markup-related 400s (fallback allowed)
          - ambiguous_failure=True for auth/rate/other transient errors (no fallback)
        """
        token = self.cfg.telegram_bot_token
        chat = self.cfg.owner_chat_id
        if not token or not chat:
            return _SendResult(success=False, ambiguous_failure=True,
                               classification="missing_credentials")

        params = {
            "chat_id": chat,
            "text": text,
            "disable_web_page_preview": "true",
        }
        if reply_markup is not None:
            params["reply_markup"] = json.dumps(reply_markup)

        data = urllib.parse.urlencode(params).encode()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            with urllib.request.urlopen(url, data=data, timeout=10) as resp:
                if resp.status == 200:
                    payload = json.loads(resp.read().decode("utf-8"))
                    mid = payload.get("result", {}).get("message_id")
                    return _SendResult(success=True, message_id=mid,
                                       http_status=resp.status, classification="accepted")
                return _SendResult(success=False, ambiguous_failure=True,
                                   http_status=resp.status, classification="http_status")
        except urllib.error.HTTPError as e:
            try:
                # Prefer direct e.read() which works on HTTPError regardless of fp.
                # Some tests construct HTTPError with a custom fp; e.read() still
                # surfaces the body via the error object's read path.
                raw = None
                if hasattr(e, "read"):
                    try:
                        raw = e.read()
                    except Exception:
                        raw = None
                # If e.read() didn't yield bytes, try fp as a last resort.
                if not isinstance(raw, (bytes, bytearray)):
                    fp = getattr(e, "fp", None)
                    if fp is not None and hasattr(fp, "read"):
                        try:
                            raw = fp.read()
                        except Exception:
                            pass
                if isinstance(raw, (bytes, bytearray)):
                    body = json.loads(raw.decode("utf-8"))
                else:
                    body = {}
            except Exception:
                body = {}
            desc = (body.get("description") or "").lower()
            params_blob = json.dumps(body.get("parameters") or {}).lower()
            # Definitive rejection: reply_markup problems (caller may fall back to plain text)
            if e.code == 400 and ("reply_markup" in desc or "reply_markup" in params_blob):
                return _SendResult(success=False, definitive_rejection=True,
                                   http_status=e.code,
                                   classification="definitive_markup_rejection")
            # Ambiguous/transient: auth, rate limits, chat blocks, generic 4xx/5xx
            classification = "auth" if e.code in (401, 403) else "rate_limit" if e.code == 429 else "http_error"
            return _SendResult(success=False, ambiguous_failure=True,
                               http_status=e.code, classification=classification)
        except Exception:
            return _SendResult(success=False, ambiguous_failure=True,
                               classification="transport_error")

    def begin_confirm(self, preview: str, script_hash: str = "", *,
                      ref_prefix: str = "H-") -> PendingConsent:
        """Create a pending H-ref approval and store it. Does NOT send; caller delivers."""
        if not script_hash:
            # Fail closed. resolve() only demands a digest when one was bound at
            # creation time, so an unbound pending would be approvable with the
            # ref alone — exactly the digest-binding bypass this gate exists to
            # prevent. Every real caller has a script/action digest.
            raise ValueError("approval requires an action/script digest")
        # 128 bits of entropy (24-char ref): unguessable before rate limits,
        # and still fits Telegram's 64-byte callback_data budget
        # ("ollie_approval:v1:a:" is 21 bytes + 24 = 45 ≤ 64).
        ref = f"{ref_prefix}{secrets.token_urlsafe(16).rstrip('=')}"
        expires_at = time.monotonic() + getattr(self.cfg, "confirm_timeout", 60)
        pc = PendingConsent(ref, preview, script_hash, expires_at)
        with self._lock:
            self._pending[ref] = pc
        # Audit: pending created. No preview/digest/text — only the ref and
        # bounded stage metadata for correlation.
        self.audit.event(
            "confirm",
            args={"ref": ref, "stage": "pending_created"},
            status="pending_created",
            detail=f"ref={ref}",
        )
        return pc

    def update_pending_preview(self, ref: str, preview: str) -> bool:
        """Update the preview text for a pending ref (used before deliver)."""
        with self._lock:
            pc = self._pending.get(ref)
            if pc is None or time.monotonic() >= pc.expires_at:
                return False
            pc.preview = preview
        return True

    def deliver_pending(self, ref: str) -> bool:
        """Send the pending approval prompt for ref. Falls back to plain text on definitive keyboard rejection.

        Returns True if any send attempt was accepted (pending remains for resolution).
        Returns False only on ambiguous total failure (no pending change).

        Every send outcome is audited with the approval-lineage correlation
        schema: the H-ref, the callback namespace/version, the exact approve/
        deny callback payloads (which contain only the ref), and Telegram's
        message_id. Digests, tokens and preview text are never audited here.
        """
        with self._lock:
            pc = self._pending.get(ref)
            if pc is None or time.monotonic() >= pc.expires_at:
                self.audit.event("confirm", args={"ref": ref, "stage": "deliver"},
                                 status="missing" if pc is None else "expired",
                                 detail=f"ref={ref}")
                if pc is not None:
                    self._pending.pop(ref, None)
                return False
            preview = pc.preview
            digest = pc.action_digest

        # The owner can always approve manually; under digest-bound resolution
        # the typed fallback must name the digest, as the original flow did.
        if digest:
            text = f"{preview}\n\nTap a button, or reply:  approve {ref} {digest}"
        else:
            text = f"{preview}\n\nTap a button, or reply:  approve {ref}"

        # Build keyboard; may be None for overlong refs (fallback to plain).
        kb = self._build_approval_keyboard(ref)

        if kb is not None:
            cb_a = f"ollie_approval:v1:a:{ref}"
            cb_d = f"ollie_approval:v1:d:{ref}"
            lineage = {"cb_ns": "ollie_approval", "cb_ver": "v1",
                       "cb_a": cb_a, "cb_d": cb_d}
            res = self._send_with_result(text, reply_markup=kb)
            if res.success:
                self.audit.event(
                    "confirm",
                    args={"ref": ref, "stage": "send", "mode": "keyboard",
                          "message_id": res.message_id, **lineage},
                    status="keyboard_accepted",
                    detail=f"ref={ref}",
                )
                return True
            if res.definitive_rejection:
                self.audit.event(
                    "confirm",
                    args={"ref": ref, "stage": "send", "mode": "keyboard",
                          "classification": res.classification, **lineage},
                    status="keyboard_rejected",
                    detail=f"ref={ref}",
                )
                # Definitive rejection of markup: fall back to plain text once.
                plain = self._send(text)
                if plain is not None:
                    self.audit.event(
                        "confirm",
                        args={"ref": ref, "stage": "send", "mode": "plain_fallback",
                              "message_id": plain},
                        status="plain_accepted",
                        detail=f"ref={ref}",
                    )
                    return True
                self.audit.event(
                    "confirm",
                    args={"ref": ref, "stage": "send", "mode": "plain_fallback"},
                    status="plain_send_failed",
                    detail=f"ref={ref}",
                )
                return False
            # Ambiguous failure: do not retry, do not fall back, do not auto-approve.
            self.audit.event(
                "confirm",
                args={"ref": ref, "stage": "send", "mode": "keyboard",
                      "classification": res.classification, **lineage},
                status="keyboard_send_failed",
                detail=f"ref={ref}",
            )
            return False

        # Overlong ref: no keyboard fits the 64-byte callback limit; plain text.
        mid = self._send(text)
        if mid is not None:
            self.audit.event(
                "confirm",
                args={"ref": ref, "stage": "send", "mode": "plain_no_keyboard",
                      "message_id": mid},
                status="plain_accepted",
                detail=f"ref={ref}",
            )
            return True
        self.audit.event(
            "confirm",
            args={"ref": ref, "stage": "send", "mode": "plain_no_keyboard"},
            status="plain_send_failed",
            detail=f"ref={ref}",
        )
        return False

    def await_confirm(self, pending: PendingConsent) -> ConsentRequest:
        """Block until the given pending ref is resolved or times out. Returns a ConsentRequest."""
        if not isinstance(pending, PendingConsent):
            self.audit.event("confirm", args={"stage": "await"},
                             status="invalid_input", detail="pending_type_invalid")
            return ConsentRequest("", "", False)
        remaining = max(0.0, pending.expires_at - time.monotonic())
        self.audit.event("confirm", args={"ref": pending.ref, "stage": "wait"},
                         status="start", detail=f"ref={pending.ref}")
        resolved = pending.event.wait(timeout=remaining)
        with self._lock:
            removed = self._pending.pop(pending.ref, None)
        decided = pending.approved if resolved else False
        terminal = "approved" if decided else "denied" if resolved else "timeout"
        self.audit.event(
            "confirm",
            args={"ref": pending.ref, "stage": "wait",
                  "cleanup": "removed" if removed is not None else "already_absent"},
            status=terminal,
            detail=f"ref={pending.ref}",
        )
        return ConsentRequest(pending.ref, pending.preview, decided)

    def confirm(self, preview: str, script_hash: str = "", *,
                ref_prefix: str = "H-") -> tuple[bool, str]:
        """Block until approved via /consent, or DENY on timeout.

        Returns (approved, ref). Callers MUST route this through
        normalize_consent_result — a non-empty tuple is always truthy, so raw
        truthiness would read a DENIAL as an approval.
        """
        # Create pending with H-ref (raises if no digest is bound).
        pc = self.begin_confirm(preview, script_hash, ref_prefix=ref_prefix)
        # Deliver (keyboard or fallback)
        self.deliver_pending(pc.ref)
        # Await resolution
        req = self.await_confirm(pc)
        # Always return tuple for normalizer; legacy normalize_consent_result handles both.
        return (req.approved, req.ref)


def normalize_consent_result(res):
    """Normalize legacy and new consent.confirm return shapes to a bool.

    Accepts:
      - bare bool (legacy direct return)
      - (approved: bool, ref: str) tuple (inline approval flow)

    Returns the boolean decision; any other shape fails closed (False).
    """
    if isinstance(res, bool):
        return res
    if isinstance(res, (list, tuple)) and len(res) >= 1:
        val = res[0]
        if isinstance(val, bool):
            return val
    return False


# ------------------------------------------------- server route helpers ---

def consent_inventory_response(consent: Consent):
    """Return a Starlette JSONResponse for GET /consent inventory."""
    from starlette.responses import JSONResponse
    try:
        consent.audit.event("consent_http", args={"stage": "inventory"},
                            status="ok", detail="stage=inventory status=ok")
    except Exception:
        pass
    now = time.monotonic()
    rows = []
    with consent._lock:
        for ref, pc in list(consent._pending.items()):
            if now >= pc.expires_at:
                consent._pending.pop(ref, None)
                continue
            rows.append({
                "ref": ref,
                "preview": pc.preview,
                # The bound action digest is exposed to the approval-token
                # holder so button-tap approvals can present it (strict
                # digest-bound resolution). Same audience as the refs
                # themselves; no new exposure class.
                "script_hash": pc.action_digest,
                "expires_in": max(0, int(pc.expires_at - now)),
            })
    return JSONResponse({"ok": True, "pending": rows})


def consent_post_response(payload: dict, consent: Consent, client_id: str = "relay",
                          mode=None):
    """Handle POST /consent payload and return a Starlette JSONResponse."""
    from starlette.responses import JSONResponse
    # Strict: only 'ref' is accepted. Legacy 'code' without 'ref' is malformed.
    ref = str(payload.get("ref", "") or "").strip()
    # Audit-first: log every /consent result with a single canonical event so
    # operators can see all rejection paths in chronological order. Never
    # logs the request body, script_hash, bearer/approval token, or preview.
    def _audit_consent(stage: str, status: str, *, ref_arg: str = "",
                       extra_args: dict | None = None) -> None:
        # Refs arrive from the network; a malformed one is never echoed verbatim
        # into the audit trail (log-injection defense), it is logged as "".
        safe_ref = _audit_ref(ref_arg)
        try:
            consent.audit.event(
                "consent_http",
                args={"stage": stage,
                      "ref": safe_ref,
                      "client_id": client_id,
                      **(extra_args or {})},
                status=status,
                detail=f"stage={stage} status={status} ref={safe_ref}".strip(),
            )
        except Exception:
            # Audit failures must never alter the HTTP response. The bare
            # `except Exception` mirrors the existing consent.py convention:
            # it does not change fail-closed behavior because we re-emit
            # any error state via the HTTP body, not by mutating the reply.
            pass

    # If caller supplied 'code' but no 'ref', treat as obsolete/malformed (tests assert).
    if not ref and payload.get("code"):
        _audit_consent("parse", "malformed_ref", ref_arg="")
        return JSONResponse({"ok": False, "error": "ref must be H-...", "error_code": "malformed_ref"}, status_code=400)

    approve = payload.get("approve", None)
    enable_bypass = payload.get("enable_bypass", False)
    script_hash = str(payload.get("script_hash", "") or "")

    # Basic format validation: must be present and start with H-
    if not ref or not ref.startswith("H-"):
        _audit_consent("parse", "malformed_ref", ref_arg=ref or "")
        return JSONResponse({"ok": False, "error": "ref must be H-...", "error_code": "malformed_ref"}, status_code=400)

    # Malformed ref characters/length checks (tests expect malformed_ref before
    # digest_required). HANDS_REF_RE is the single source of truth for the shape.
    if not HANDS_REF_RE.fullmatch(ref) or len(ref) > 64:
        _audit_consent("parse", "malformed_ref", ref_arg=ref)
        return JSONResponse({"ok": False, "error": "ref must be H-...", "error_code": "malformed_ref"}, status_code=400)

    if not isinstance(approve, bool):
        _audit_consent("parse", "invalid_approve", ref_arg=ref)
        return JSONResponse({"ok": False, "error": "approve must be boolean", "error_code": "invalid_approve"}, status_code=400)
    if not isinstance(enable_bypass, bool) or (enable_bypass and not approve):
        _audit_consent("parse", "invalid_enable_bypass", ref_arg=ref)
        return JSONResponse({"ok": False,
                             "error": "enable_bypass requires approve=true",
                             "error_code": "invalid_enable_bypass"}, status_code=400)
    if enable_bypass and mode is None:
        _audit_consent("resolve", "mode_unavailable", ref_arg=ref)
        return JSONResponse({"ok": False, "error": "mode unavailable",
                             "error_code": "mode_unavailable"}, status_code=503)

    # script_hash is optional for resolve; only required when a digest was bound at creation time.
    # resolve() will return digest_required if one was bound and not supplied.
    out = consent.resolve(
        ref, approve, script_hash, client_id=client_id,
        enable_bypass=(lambda: mode.set("bypass")) if enable_bypass else None,
    )
    if out.get("ok"):
        decision = "approved" if out.get("approved", approve) else "denied"
        _audit_consent("resolve", decision, ref_arg=ref,
                       extra_args={"approve": out.get("approved", approve)})
        return JSONResponse({"ok": True, "ref": out.get("code", ref),
                             "approved": out.get("approved", approve),
                             "mode": out.get("mode", "normal")})

    statuses = {"digest_required": 400, "digest_mismatch": 409,
                "unknown_or_expired": 404, "rate_limited": 429}
    code = out.get("error_code", "bad_request")
    status = statuses.get(code, 400)
    # Map every terminal rejection reason to a stable audit status.
    audit_status = {
        "digest_required": "digest_required",
        "digest_mismatch": "digest_mismatch",
        "unknown_or_expired": "unknown_or_expired",
        "rate_limited": "rate_limited",
    }.get(code, "bad_request")
    _audit_consent("resolve", audit_status, ref_arg=ref)
    return JSONResponse({"ok": False, "error": out.get("error", code),
                         "error_code": code}, status_code=status)
