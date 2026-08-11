"""Tamper-evidence tests for the audit hash chain (plan Track D2).

Pure (no Windows deps): writes to a temp dir, then proves the chain detects
every class of tampering — body edits, reordering, deletion — and localises the
break. Also proves restart-continuity and tolerance of legacy pre-chain records.

Run:  python -m pytest tests/test_audit_chain.py   (or)   python tests/test_audit_chain.py
"""
import json
import sys
import pathlib
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ollie_hands import audit as AUD  # noqa: E402


def _fresh():
    d = tempfile.mkdtemp(prefix="audit-test-")
    return d, AUD.Audit(d)


def _only_file(d):
    fs = sorted(pathlib.Path(d).glob("audit-*.jsonl"))
    assert fs, "no audit file written"
    return fs[-1]


def test_clean_chain_verifies():
    d, a = _fresh()
    for i in range(5):
        a.event("act", args={"i": i}, status="ok")
    res = AUD.verify_chain(d)
    assert res["ok"] is True, res
    assert res["chained"] == 5
    assert res["legacy"] == 0
    assert res["break"] is None


def test_first_record_links_to_genesis():
    d, a = _fresh()
    a.event("boot")
    line = _only_file(d).read_text(encoding="utf-8").splitlines()[0]
    rec = json.loads(line)
    assert rec["prev"] == AUD.GENESIS
    assert rec["hash"]


def test_body_tamper_is_detected_and_localised():
    d, a = _fresh()
    ids = [a.event("act", args={"n": i}) for i in range(4)]
    fp = _only_file(d)
    lines = fp.read_text(encoding="utf-8").splitlines()
    # flip a field in record #3 WITHOUT recomputing its hash (an attacker edit)
    rec = json.loads(lines[2])
    rec["status"] = "TAMPERED"
    lines[2] = json.dumps(rec, ensure_ascii=False)
    fp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    res = AUD.verify_chain(d)
    assert res["ok"] is False
    assert res["break"]["id"] == ids[2]
    assert "altered" in res["break"]["reason"]


def test_deletion_breaks_the_chain():
    d, a = _fresh()
    for i in range(4):
        a.event("act", args={"n": i})
    fp = _only_file(d)
    lines = fp.read_text(encoding="utf-8").splitlines()
    del lines[1]  # excise a record; the next record's prev now dangles
    fp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    res = AUD.verify_chain(d)
    assert res["ok"] is False
    assert "link" in res["break"]["reason"] or "deletion" in res["break"]["reason"]


def test_reorder_breaks_the_chain():
    d, a = _fresh()
    for i in range(4):
        a.event("act", args={"n": i})
    fp = _only_file(d)
    lines = fp.read_text(encoding="utf-8").splitlines()
    lines[1], lines[2] = lines[2], lines[1]  # swap two records
    fp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    res = AUD.verify_chain(d)
    assert res["ok"] is False


def test_restart_continues_the_chain():
    d, a = _fresh()
    a.event("act", args={"a": 1})
    a.event("act", args={"a": 2})
    # simulate restart: a NEW Audit on the same dir must resume from last hash
    a2 = AUD.Audit(d)
    a2.event("act", args={"a": 3})
    res = AUD.verify_chain(d)
    assert res["ok"] is True, res
    assert res["chained"] == 3


def test_legacy_records_tolerated_then_chained():
    """Pre-chain records (no hash) at the transition are tolerated; the chain
    starts cleanly after them and still verifies."""
    d = tempfile.mkdtemp(prefix="audit-legacy-")
    fp = pathlib.Path(d) / "audit-20000101.jsonl"
    fp.write_text(
        json.dumps({"id": "old1", "tool": "act", "status": "ok"}) + "\n"
        + json.dumps({"id": "old2", "tool": "act", "status": "ok"}) + "\n",
        encoding="utf-8")
    a = AUD.Audit(d)          # resumes -> genesis (no prior hash)
    a.event("act", args={"new": 1})
    a.event("act", args={"new": 2})
    res = AUD.verify_chain(d)
    assert res["ok"] is True, res
    assert res["legacy"] == 2
    assert res["chained"] == 2


def test_dropping_a_chained_hash_is_flagged_after_start():
    """Once chaining has begun, a record that loses its `hash` (an attacker
    stripping the chain field) is flagged, not silently treated as legacy."""
    d, a = _fresh()
    a.event("act", args={"n": 1})
    a.event("act", args={"n": 2})
    fp = _only_file(d)
    lines = fp.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[1])
    rec.pop("hash")
    lines[1] = json.dumps(rec, ensure_ascii=False)
    fp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    res = AUD.verify_chain(d)
    assert res["ok"] is False
    assert "unchained" in res["break"]["reason"]


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
            passed += 1
        except Exception:
            print(f"  FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(fns)} audit-chain tests passed")
    sys.exit(0 if passed == len(fns) else 1)
