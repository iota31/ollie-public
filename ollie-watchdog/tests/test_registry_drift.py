"""Tests for S1.3 registry drift detection (check_registry_drift).

Covers the probe contract: missing manifest = feature off (None), clean tree
= None, hash mismatch / missing deployed file / corrupt manifest = actionable
error string naming the component. No real manifest or deployed files — every
test builds its own tmp tree. Run from ollie-watchdog/:

    python3 -m unittest tests.test_registry_drift -v
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import ollie_watchdog as wd  # noqa: E402


def _write(path, content: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)


def _manifest(tmp, entries):
    m = {"version": 1, "generated_at": "2026-07-24T00:00:00Z", "entries": entries}
    path = os.path.join(tmp, "manifest.json")
    _write(path, json.dumps(m).encode())
    return path


class RegistryDriftTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="drift-test-")

    def _run(self, manifest_path):
        with mock.patch.object(wd, "REGISTRY_MANIFEST", manifest_path):
            return wd.check_registry_drift()

    def test_missing_manifest_is_feature_off_not_error(self):
        result = self._run(os.path.join(self.tmp, "no-such-manifest.json"))
        self.assertIsNone(result)

    def test_clean_tree_returns_none(self):
        deployed = os.path.join(self.tmp, "bin/tool.py")
        _write(deployed, b"print('reviewed')\n")
        mpath = _manifest(self.tmp, [{
            "component": "tool", "repo_path": "tool.py", "path": deployed,
            "sha256": hashlib.sha256(b"print('reviewed')\n").hexdigest(),
        }])
        self.assertIsNone(self._run(mpath))

    def test_drifted_hash_names_component(self):
        deployed = os.path.join(self.tmp, "bin/tool.py")
        _write(deployed, b"print('tampered on box')\n")
        mpath = _manifest(self.tmp, [{
            "component": "tool", "repo_path": "tool.py", "path": deployed,
            "sha256": hashlib.sha256(b"print('reviewed')\n").hexdigest(),
        }])
        result = self._run(mpath)
        self.assertIsNotNone(result)
        self.assertIn("REGISTRY DRIFT", result)
        self.assertIn("tool:tool.py", result)
        self.assertIn("differ from reviewed source", result)

    def test_missing_deployed_file_reported(self):
        mpath = _manifest(self.tmp, [{
            "component": "ghost", "repo_path": "ghost.py",
            "path": os.path.join(self.tmp, "bin/ghost.py"),
            "sha256": "0" * 64,
        }])
        result = self._run(mpath)
        self.assertIsNotNone(result)
        self.assertIn("missing", result)
        self.assertIn("ghost:ghost.py", result)

    def test_corrupt_manifest_is_error(self):
        mpath = os.path.join(self.tmp, "manifest.json")
        _write(mpath, b"{not json")
        result = self._run(mpath)
        self.assertIsNotNone(result)
        self.assertIn("unreadable", result)

    def test_multiple_drifts_capped_and_counted(self):
        entries = []
        for i in range(6):
            p = os.path.join(self.tmp, f"bin/f{i}.py")
            _write(p, f"box-version-{i}".encode())
            entries.append({"component": f"comp{i}", "path": p, "sha256": "0" * 64})
        mpath = _manifest(self.tmp, entries)
        result = self._run(mpath)
        self.assertIn("6 deployed file(s)", result)
        self.assertIn("…", result)  # list capped at 4 with ellipsis

    def test_unreadable_file_reported_as_missing_not_crash(self):
        deployed = os.path.join(self.tmp, "bin/noperm.py")
        _write(deployed, b"x")
        os.chmod(deployed, 0o000)
        try:
            mpath = _manifest(self.tmp, [{
                "component": "noperm", "path": deployed, "sha256": "0" * 64,
            }])
            result = self._run(mpath)
            self.assertIsNotNone(result)
            self.assertIn("noperm", result)
        finally:
            os.chmod(deployed, 0o644)

    def test_manifest_age_shown_in_message(self):
        deployed = os.path.join(self.tmp, "bin/tool.py")
        _write(deployed, b"tampered")
        mpath = _manifest(self.tmp, [{
            "component": "tool", "path": deployed, "sha256": "0" * 64,
        }])
        result = self._run(mpath)
        self.assertIn("manifest from 2026-07-24", result)


if __name__ == "__main__":
    unittest.main()
