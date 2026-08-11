"""Regression tests for the repository-owned OpenClaw Hands timeout seam."""

from __future__ import annotations

import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-host.ps1"
README = ROOT / "README.md"


def _installer_hands_snippet() -> str:
    text = INSTALLER.read_text(encoding="utf-8")
    start = text.index('""hands""')
    end = text.index('Kill switch:', start)
    return text[start:end]


def test_installer_prints_supported_openclaw_request_timeout():
    snippet = _installer_hands_snippet()
    match = re.search(r'""timeout""\s*:\s*(\d+)', snippet)
    assert match, "Hands OpenClaw wiring must set the supported timeout field"
    assert int(match.group(1)) == 240


def test_openclaw_timeout_exceeds_confirm_window():
    snippet = _installer_hands_snippet()
    call_timeout = int(re.search(r'""timeout""\s*:\s*(\d+)', snippet).group(1))

    config_source = (ROOT / "ollie_hands" / "config.py").read_text(encoding="utf-8")
    confirm_timeout = int(
        re.search(r"confirm_timeout:\s*int\s*=\s*(\d+)", config_source).group(1)
    )
    assert call_timeout > confirm_timeout


def test_readme_documents_timeout_units_and_purpose():
    text = README.read_text(encoding="utf-8")
    assert "`timeout` to `240` seconds" in text
    assert "180-second owner-confirmation window" in text
