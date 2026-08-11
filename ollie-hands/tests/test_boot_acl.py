"""Boot ACL regression tests — P0-C: token-file ACLs and shell principal.

Run anywhere: python -m pytest tests/test_boot_acl.py
"""
import sys
import pathlib
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ollie_hands import config as C  # noqa: E402


def _tmp_cfg(*, bearer_exists=True, approval_exists=True,
             bearer_mode=0o600, approval_mode=0o600,
             cred_exists=True):
    """Build a Config pointing at temp files for ACL testing."""
    d = Path(tempfile.mkdtemp())
    bearer = d / "bearer.token"
    approval = d / "approval.token"
    cred = d / "shelluser.cred"
    if bearer_exists:
        bearer.write_text("test-secret-bearer")
        bearer.chmod(bearer_mode)
    if approval_exists:
        approval.write_text("test-secret-approval")
        approval.chmod(approval_mode)
    if cred_exists:
        cred.write_text("encrypted-cred")
    cfg = C.Config(
        token_file=str(bearer),
        approval_token_file=str(approval),
        audit_dir=str(d / "audit"),
    )
    return cfg, d


def test_boot_acl_passes_with_proper_files():
    cfg, d = _tmp_cfg()
    try:
        C.validate_boot_acl(cfg)
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_boot_acl_fails_bearer_missing():
    cfg, d = _tmp_cfg(bearer_exists=False)
    try:
        try:
            C.validate_boot_acl(cfg)
            assert False, "should have raised BootACLError"
        except C.BootACLError as e:
            assert "bearer.token" in str(e)
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_boot_acl_fails_approval_missing():
    cfg, d = _tmp_cfg(approval_exists=False)
    try:
        try:
            C.validate_boot_acl(cfg)
            assert False, "should have raised BootACLError"
        except C.BootACLError as e:
            assert "approval.token" in str(e)
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_boot_acl_fails_permissive_bearer():
    cfg, d = _tmp_cfg(bearer_mode=0o644)
    try:
        try:
            C.validate_boot_acl(cfg)
            assert False, "should have raised BootACLError"
        except C.BootACLError as e:
            assert "permissive mode" in str(e)
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_boot_acl_fails_permissive_approval():
    cfg, d = _tmp_cfg(approval_mode=0o666)
    try:
        try:
            C.validate_boot_acl(cfg)
            assert False, "should have raised BootACLError"
        except C.BootACLError as e:
            assert "permissive mode" in str(e)
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_boot_acl_accepts_0o600():
    cfg, d = _tmp_cfg(bearer_mode=0o600, approval_mode=0o600)
    try:
        C.validate_boot_acl(cfg)  # should not raise
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_boot_acl_passes_with_proper_files()
    test_boot_acl_fails_bearer_missing()
    test_boot_acl_fails_approval_missing()
    test_boot_acl_fails_permissive_bearer()
    test_boot_acl_fails_permissive_approval()
    test_boot_acl_accepts_0o600()
    print("all boot ACL tests passed")
