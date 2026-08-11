"""Owner-approved, narrowly scoped authorization leases.

Grants complement (never replace) exact-digest approvals.  An owner approves a
scope once; repaired plans may reuse it only when their mechanically derived
requirements are a subset.  Unknown resources/effects fail closed.
"""
from __future__ import annotations

from dataclasses import dataclass
import secrets
import threading
import time
from urllib.parse import urlparse

from . import policy as P

EFFECTS = P.EFFECT_CATEGORIES
COMMIT_EFFECTS = P.COMMIT_EFFECTS


class GrantError(ValueError):
    pass


def canonical_resource(value: str) -> str:
    if not isinstance(value, str):
        raise GrantError("authorization.resources entries must be strings")
    value = value.strip().lower()
    parsed = urlparse(value if "://" in value else f"https://{value}")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise GrantError(f"invalid web resource: {value!r}")
    if parsed.username or parsed.password:
        raise GrantError(f"invalid web resource: {value!r}")
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def parse_declared_resource(value: str) -> str:
    resource = canonical_resource(value)
    parsed = urlparse(value.strip().lower() if isinstance(value, str) else value)
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise GrantError(f"web resources must be exact origins: {value!r}")
    return resource


@dataclass(frozen=True)
class Scope:
    family: str
    resources: frozenset[str]
    effects: frozenset[str]
    ttl_seconds: int

    @classmethod
    def parse(cls, raw: dict) -> "Scope":
        if not isinstance(raw, dict):
            raise GrantError("authorization must be an object")
        unknown_fields = set(raw) - {
            "family", "resources", "effects", "ttl_seconds", "grant_id",
        }
        if unknown_fields:
            raise GrantError(
                f"unknown authorization fields: {sorted(unknown_fields)}")
        family_raw = raw.get("family")
        if not isinstance(family_raw, str):
            raise GrantError("authorization.family must be a string")
        family = family_raw.strip()
        if not family or len(family) > 128:
            raise GrantError("authorization.family must be 1..128 characters")
        resources_raw = raw.get("resources")
        effects_raw = raw.get("effects")
        if not isinstance(resources_raw, list) or not resources_raw:
            raise GrantError("authorization.resources must be a non-empty array")
        if not isinstance(effects_raw, list) or not effects_raw:
            raise GrantError("authorization.effects must be a non-empty array")
        resources_list = [parse_declared_resource(x) for x in resources_raw]
        if len(set(resources_list)) != len(resources_list):
            raise GrantError("authorization.resources must not contain duplicates")
        effects_list: list[str] = []
        for value in effects_raw:
            if not isinstance(value, str):
                raise GrantError("authorization.effects entries must be strings")
            effect = value.strip().lower()
            if not effect:
                raise GrantError("authorization.effects entries must be non-empty")
            if effect not in EFFECTS:
                raise GrantError(f"unknown authorization effects: {[effect]}")
            effects_list.append(effect)
        if len(set(effects_list)) != len(effects_list):
            raise GrantError("authorization.effects must not contain duplicates")
        ttl_raw = raw.get("ttl_seconds", 600)
        if isinstance(ttl_raw, bool) or not isinstance(ttl_raw, int):
            raise GrantError("authorization.ttl_seconds must be an integer")
        if ttl_raw < 30 or ttl_raw > 1800:
            raise GrantError("authorization.ttl_seconds must be 30..1800")
        grant_id = raw.get("grant_id")
        if grant_id is not None:
            if not isinstance(grant_id, str):
                raise GrantError("authorization.grant_id must be a string")
            grant_id = grant_id.strip()
            if not grant_id or len(grant_id) > 256:
                raise GrantError("authorization.grant_id must be 1..256 characters")
        return cls(family, frozenset(resources_list), frozenset(effects_list), ttl_raw)

    def summary(self, ref: str) -> str:
        return (
            "Approve browser task scope:\n\n"
            f"Objective: {self.family}\n"
            f"Origins: {', '.join(sorted(self.resources))}\n"
            f"Allowed effects: {', '.join(sorted(self.effects))}\n"
            f"TTL: {self.ttl_seconds}s\n"
            "Consequential commit allowance: single-use\n"
            f"Ref: {ref}\n\n"
            "Reply approve or deny. If another request is pending, reply "
            f"approve {ref} or deny {ref}."
        )


@dataclass
class Grant:
    id: str
    scope: Scope
    expires_at: float
    commit_consumed: bool = False
    # Opaque holder token of the task that reserved the single-use commit.
    # None until the first commit dispatch reserves it; once set, only that
    # holder may proceed with commit dispatches on this grant.
    commit_holder: str | None = None


class GrantStore:
    def __init__(self, audit, clock=time.monotonic) -> None:
        self.audit = audit
        self.clock = clock
        self._lock = threading.Lock()
        self._grants: dict[str, Grant] = {}

    def issue(self, scope: Scope) -> Grant:
        grant = Grant(secrets.token_urlsafe(18), scope,
                      self.clock() + scope.ttl_seconds)
        with self._lock:
            self._grants[grant.id] = grant
        self.audit.event("grant", args={"grant_id": grant.id,
                         "family": scope.family,
                         "resources": sorted(scope.resources),
                         "effects": sorted(scope.effects)}, status="issued",
                         detail=f"expires_in={scope.ttl_seconds}s")
        return grant

    def authorize(self, grant_id: str, scope: Scope, *,
                  required_resources: set[str], required_effects: set[str]) -> Grant:
        now = self.clock()
        with self._lock:
            grant = self._grants.get(grant_id)
            if grant is None or now >= grant.expires_at:
                self._grants.pop(grant_id, None)
                reason = "unknown_or_expired"
            elif scope.family != grant.scope.family:
                reason = "family_mismatch"
            elif not scope.resources.issubset(grant.scope.resources):
                reason = "resource_scope_widened"
            elif not scope.effects.issubset(grant.scope.effects):
                reason = "effect_scope_widened"
            elif not required_resources.issubset(grant.scope.resources):
                reason = "required_resource_out_of_scope"
            elif not required_effects.issubset(grant.scope.effects):
                reason = "required_effect_out_of_scope"
            elif required_effects & COMMIT_EFFECTS and grant.commit_consumed:
                reason = "commit_already_consumed"
            else:
                self.audit.event("grant", args={"grant_id": grant.id},
                                 status="reused", detail=scope.family)
                return grant
        self.audit.event("grant", args={"grant_id": grant_id},
                         status="rejected", detail=reason)
        raise GrantError(reason)

    def consume_commit(self, grant_id: str) -> None:
        with self._lock:
            grant = self._grants.get(grant_id)
            if grant is None or self.clock() >= grant.expires_at:
                raise GrantError("unknown_or_expired")
            if grant.commit_consumed:
                raise GrantError("commit_already_consumed")
            grant.commit_consumed = True
        self.audit.event("grant", args={"grant_id": grant_id},
                         status="commit_consumed")

    def reserve_commit(self, grant_id: str, holder: str) -> None:
        """Atomically claim the single-use commit allowance for `holder`.

        Called immediately BEFORE the first commit-effect dispatch of a task.
        The winner is recorded so the outcome (success/failure/unknown) never
        re-opens the allowance: once reserved it is consumed for good.  A
        second/concurrent task (different holder) always loses.  The SAME
        holder re-reserving is idempotent (a task may have >1 commit step, all
        covered by its single owner reservation).  Fails closed on unknown/
        expired grant or on any competing holder.
        """
        if not holder:
            raise GrantError("commit_holder_required")
        with self._lock:
            grant = self._grants.get(grant_id)
            if grant is None or self.clock() >= grant.expires_at:
                self._grants.pop(grant_id, None)
                reason = "unknown_or_expired"
            elif grant.commit_holder is None and not grant.commit_consumed:
                grant.commit_holder = holder
                grant.commit_consumed = True
                self.audit.event("grant", args={"grant_id": grant_id},
                                 status="commit_reserved", detail=holder[:64])
                return
            elif grant.commit_holder == holder:
                # Idempotent re-reservation by the same task.
                return
            else:
                reason = "commit_already_consumed"
        self.audit.event("grant", args={"grant_id": grant_id},
                         status="rejected", detail=reason)
        raise GrantError(reason)

    def revoke(self, grant_id: str, reason: str = "revoked") -> bool:
        with self._lock:
            removed = self._grants.pop(grant_id, None)
        if removed is not None:
            self.audit.event("grant", args={"grant_id": grant_id},
                             status="revoked", detail=reason[:120])
        return removed is not None
