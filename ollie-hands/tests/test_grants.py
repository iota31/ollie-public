"""Scoped-authorization grants: strict parsing, exact origins, single-use commit.

`grants.py` was written directly on the box and shipped with no repo test
coverage at all. These tests pin the properties the layer exists to provide:
a grant can only ever NARROW, never widen; origins are exact; and the
consequential-commit allowance is single-use and holder-bound.
"""

import pathlib
import sys
import threading

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ollie_hands import grants as G  # noqa: E402


class AuditStub:
    def __init__(self):
        self.events = []

    def event(self, event, **kwargs):
        self.events.append((event, kwargs))


def _scope(resources=("https://example.com",), effects=("navigation",),
           family="buy a thing", ttl=600):
    return G.Scope.parse({"family": family, "resources": list(resources),
                          "effects": list(effects), "ttl_seconds": ttl})


# --------------------------------------------------------------- origins ---

def test_canonical_resource_reduces_to_bare_origin():
    assert G.canonical_resource("https://Example.com/a/b?c=d#e") == "https://example.com"
    assert G.canonical_resource("example.com") == "https://example.com"
    assert G.canonical_resource("https://example.com:8443/x") == "https://example.com:8443"


@pytest.mark.parametrize("bad", [
    "ftp://example.com",          # non-web scheme
    "https://user:pw@example.com",  # embedded credentials
    "https:///nohost",
    "",
])
def test_canonical_resource_rejects_non_web_and_credentialed_urls(bad):
    with pytest.raises(G.GrantError):
        G.canonical_resource(bad)


def test_declared_resources_must_be_bare_origins():
    """A declared resource carrying a path/query would imply a prefix rule the
    enforcement layer does NOT implement, so it is rejected at parse time."""
    assert G.parse_declared_resource("https://example.com/") == "https://example.com"
    for bad in ["https://example.com/inbox", "https://example.com?a=1",
                "https://example.com#frag"]:
        with pytest.raises(G.GrantError):
            G.parse_declared_resource(bad)


def test_lookalike_host_is_not_in_scope():
    """The membership test is exact-set, so a suffix look-alike never matches."""
    scope = _scope(resources=["https://reddit.com"])
    assert G.canonical_resource("https://reddit.com.attacker.test/x") not in scope.resources


# ----------------------------------------------------------- Scope.parse ---

def test_scope_parse_rejects_unknown_fields_and_effects():
    with pytest.raises(G.GrantError):
        G.Scope.parse({"family": "f", "resources": ["https://a.test"],
                       "effects": ["navigation"], "bonus": 1})
    with pytest.raises(G.GrantError):
        G.Scope.parse({"family": "f", "resources": ["https://a.test"],
                       "effects": ["not_a_real_effect"]})


@pytest.mark.parametrize("raw", [
    {"family": "", "resources": ["https://a.test"], "effects": ["navigation"]},
    {"family": "f", "resources": [], "effects": ["navigation"]},
    {"family": "f", "resources": ["https://a.test"], "effects": []},
    {"family": "f", "resources": ["https://a.test", "https://a.test"],
     "effects": ["navigation"]},
    {"family": "f", "resources": ["https://a.test"],
     "effects": ["navigation", "navigation"]},
    {"family": "f", "resources": ["https://a.test"], "effects": ["navigation"],
     "ttl_seconds": 29},
    {"family": "f", "resources": ["https://a.test"], "effects": ["navigation"],
     "ttl_seconds": 1801},
    {"family": "f", "resources": ["https://a.test"], "effects": ["navigation"],
     "ttl_seconds": True},   # bool is not an acceptable int
])
def test_scope_parse_fails_closed_on_malformed_input(raw):
    with pytest.raises(G.GrantError):
        G.Scope.parse(raw)


def test_scope_summary_names_origins_effects_and_single_use_allowance():
    """The owner approves this text, so it must state what is being granted."""
    s = _scope(resources=["https://a.test"], effects=["navigation", "external_commit"])
    text = s.summary("H-ref1")
    assert "https://a.test" in text
    assert "external_commit" in text
    assert "single-use" in text
    assert "H-ref1" in text


# ------------------------------------------------------- authorize (narrow) ---

def test_authorize_rejects_any_widening():
    store = G.GrantStore(AuditStub())
    granted = _scope(resources=["https://a.test"], effects=["navigation", "draft"])
    g = store.issue(granted)

    # Same scope, subset requirements: allowed.
    assert store.authorize(g.id, granted, required_resources={"https://a.test"},
                           required_effects={"navigation"}) is g

    wider_resource = _scope(resources=["https://a.test", "https://b.test"],
                            effects=["navigation"])
    with pytest.raises(G.GrantError, match="resource_scope_widened"):
        store.authorize(g.id, wider_resource, required_resources=set(),
                        required_effects=set())

    wider_effect = _scope(resources=["https://a.test"],
                          effects=["navigation", "external_commit"])
    with pytest.raises(G.GrantError, match="effect_scope_widened"):
        store.authorize(g.id, wider_effect, required_resources=set(),
                        required_effects=set())

    with pytest.raises(G.GrantError, match="required_resource_out_of_scope"):
        store.authorize(g.id, granted, required_resources={"https://b.test"},
                        required_effects=set())

    with pytest.raises(G.GrantError, match="required_effect_out_of_scope"):
        store.authorize(g.id, granted, required_resources=set(),
                        required_effects={"external_commit"})

    other_family = _scope(resources=["https://a.test"], effects=["navigation"],
                          family="something else")
    with pytest.raises(G.GrantError, match="family_mismatch"):
        store.authorize(g.id, other_family, required_resources=set(),
                        required_effects=set())


def test_authorize_rejects_unknown_and_expired_grants():
    clock = [1000.0]
    store = G.GrantStore(AuditStub(), clock=lambda: clock[0])
    scope = _scope(ttl=30)
    g = store.issue(scope)

    with pytest.raises(G.GrantError, match="unknown_or_expired"):
        store.authorize("not-a-grant", scope, required_resources=set(),
                        required_effects=set())

    clock[0] += 31
    with pytest.raises(G.GrantError, match="unknown_or_expired"):
        store.authorize(g.id, scope, required_resources=set(),
                        required_effects=set())


# ------------------------------------------------- single-use commit lease ---

def test_reserve_commit_is_single_use_and_holder_bound():
    store = G.GrantStore(AuditStub())
    g = store.issue(_scope(effects=["external_commit"]))

    store.reserve_commit(g.id, "task-a")
    # Idempotent for the SAME task: one task may have several commit steps.
    store.reserve_commit(g.id, "task-a")
    # A different task always loses.
    with pytest.raises(G.GrantError, match="commit_already_consumed"):
        store.reserve_commit(g.id, "task-b")


def test_reserve_commit_requires_a_holder():
    store = G.GrantStore(AuditStub())
    g = store.issue(_scope(effects=["external_commit"]))
    with pytest.raises(G.GrantError, match="commit_holder_required"):
        store.reserve_commit(g.id, "")


def test_reservation_survives_the_outcome_and_blocks_reuse_via_authorize():
    """Once reserved, the allowance is spent for good — an uncertain or failed
    outcome must NOT re-open it for a later commit on the same grant."""
    store = G.GrantStore(AuditStub())
    scope = _scope(effects=["navigation", "external_commit"])
    g = store.issue(scope)

    store.reserve_commit(g.id, "task-a")
    with pytest.raises(G.GrantError, match="commit_already_consumed"):
        store.authorize(g.id, scope, required_resources=set(),
                        required_effects={"external_commit"})
    # Non-commit reuse of the same grant is still fine.
    assert store.authorize(g.id, scope, required_resources=set(),
                           required_effects={"navigation"}) is g


def test_exactly_one_concurrent_task_wins_the_commit_reservation():
    store = G.GrantStore(AuditStub())
    g = store.issue(_scope(effects=["external_commit"]))

    winners, losers = [], []
    barrier = threading.Barrier(8)

    def attempt(name):
        barrier.wait()
        try:
            store.reserve_commit(g.id, name)
            winners.append(name)
        except G.GrantError:
            losers.append(name)

    threads = [threading.Thread(target=attempt, args=(f"task-{i}",))
               for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1
    assert len(losers) == 7


def test_expired_grant_cannot_reserve_a_commit():
    clock = [1000.0]
    store = G.GrantStore(AuditStub(), clock=lambda: clock[0])
    g = store.issue(_scope(effects=["external_commit"], ttl=30))
    clock[0] += 31
    with pytest.raises(G.GrantError, match="unknown_or_expired"):
        store.reserve_commit(g.id, "task-a")


def test_revoke_removes_the_grant_once():
    store = G.GrantStore(AuditStub())
    g = store.issue(_scope())
    assert store.revoke(g.id) is True
    assert store.revoke(g.id) is False
    with pytest.raises(G.GrantError, match="unknown_or_expired"):
        store.reserve_commit(g.id, "task-a")


def test_commit_effects_are_a_subset_of_known_effect_categories():
    assert G.COMMIT_EFFECTS <= G.EFFECTS
    assert {"external_commit", "identity_commit", "destructive"} == set(G.COMMIT_EFFECTS)
