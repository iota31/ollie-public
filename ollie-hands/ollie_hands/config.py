"""Config + inert-boot + bearer token handling.

Inert-boot contract (Phase 0):
- config ``enabled`` defaults to **false** — a fresh install does nothing.
- a ``DISABLED`` flag-file next to the config is checked on EVERY request
  and overrides ``enabled`` (the global "disable hands" kill switch:
  ``echo. > C:\\ProgramData\\ollie-hands\\DISABLED``).

P0-C boot ACL contract:
- Both bearer.token and approval.token must exist at boot.
- Token files must be owner-readable only (mode 0o600 on Unix; on Windows
  the check is advisory — NTFS ACLs are set by the provisioning script).
- The de-privileged shell credential (shelluser.cred) must exist on Windows
  in production.  On non-Windows or dev boxes the check is skipped.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DIR = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "ollie-hands"
CONFIG_PATH = Path(os.environ.get("OLLIE_HANDS_CONFIG", DEFAULT_DIR / "config.json"))


@dataclass
class Config:
    enabled: bool = False  # inert by default
    host: str = "127.0.0.1"  # install script widens this deliberately
    port: int = 3200
    token_file: str = str(CONFIG_PATH.parent / "bearer.token")
    approval_token_file: str = str(CONFIG_PATH.parent / "approval.token")
    audit_dir: str = str(CONFIG_PATH.parent / "audit")
    # UIA snapshot bounds — keep observe() fast and the payload small
    uia_max_windows: int = 12
    uia_max_depth: int = 3
    uia_max_children: int = 24
    # consent / notify (owner-supplied host-side; never committed)
    telegram_bot_token: str = ""
    owner_chat_id: str = ""
    confirm_timeout: int = 180  # seconds to wait for owner approval, then DENY
    approval_rate_limit_attempts: int = 12
    approval_rate_limit_window: int = 60
    # vision grounding (L3 Tier-2 fallback when UIA finds no element)
    mimo_base_url: str = "https://token-plan-sgp.xiaomimimo.com/v1"
    mimo_api_key: str = ""  # host-side secret; never committed
    mimo_model: str = "mimo-v2.5"
    # captcha solving (noCaptchaAI) — host-side only
    nocaptcha_api_key: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def disabled_flag(self) -> Path:
        return CONFIG_PATH.parent / "DISABLED"

    def hands_enabled(self) -> bool:
        """Checked per-request: config flag AND no kill-switch file."""
        return self.enabled and not self.disabled_flag.exists()

    def bearer_token(self) -> str:
        return Path(self.token_file).read_text(encoding="utf-8").strip()

    def approval_token(self) -> str:
        """Credential accepted only by the owner approval endpoint."""
        return Path(self.approval_token_file).read_text(encoding="utf-8").strip()


def load() -> Config:
    cfg = Config()
    if CONFIG_PATH.exists():
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for key in ("enabled", "host", "port", "token_file",
                    "approval_token_file", "audit_dir",
                    "uia_max_windows", "uia_max_depth", "uia_max_children",
                    "telegram_bot_token", "owner_chat_id", "confirm_timeout",
                    "approval_rate_limit_attempts", "approval_rate_limit_window",
                    "mimo_base_url", "mimo_api_key", "mimo_model",
                    "nocaptcha_api_key"):
            if key in data:
                setattr(cfg, key, data[key])
        cfg.raw = data
    return cfg


def provision(write_config: bool = True) -> Config:
    """Create config dir, default (inert) config, and a bearer token.

    Never overwrites an existing config or token.
    """
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    cfg = Config()
    if write_config and not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps({
            "enabled": False,
            "host": cfg.host,
            "port": cfg.port,
            "token_file": cfg.token_file,
            "approval_token_file": cfg.approval_token_file,
            "audit_dir": cfg.audit_dir,
        }, indent=2), encoding="utf-8")
    # Respect paths in an existing config while preserving no-overwrite.
    cfg = load()
    token_path = Path(cfg.token_file)
    if not token_path.exists():
        token_path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    approval_token_path = Path(cfg.approval_token_file)
    if not approval_token_path.exists():
        approval_token_path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return load()


if __name__ == "__main__":
    c = provision()
    print(f"config: {CONFIG_PATH}")
    print(f"token:  {c.token_file}")
    print(f"approval token: {c.approval_token_file}")
    print(f"enabled (inert-boot should be False): {c.enabled}")


_SHELL_USER_CRED = Path(os.environ.get(
    "PROGRAMDATA", r"C:\ProgramData")) / "ollie-hands" / "shelluser.cred"


class BootACLError(RuntimeError):
    """Raised at engine start when token-file ACLs or shell principal are missing."""


def validate_boot_acl(cfg: Config) -> None:
    """Assert token files exist and are owner-only readable; fail closed otherwise.

    P0-C: approval-token secrecy collapses the authority split if the shell
    principal can read it.  On Unix we enforce mode 0o600; on Windows we
    verify the files exist (NTFS ACLs are set by setup-shell-user.ps1).
    The de-privileged shell credential must also exist on Windows production.
    """
    errors: list[str] = []

    for label, path_str in [("bearer.token", cfg.token_file),
                            ("approval.token", cfg.approval_token_file)]:
        p = Path(path_str)
        if not p.exists():
            errors.append(f"{label} missing at {p}")
            continue
        if sys.platform != "win32":
            mode = p.stat().st_mode
            if mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IXGRP |
                       stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH):
                errors.append(
                    f"{label} at {p} has permissive mode {oct(mode)} — "
                    "expected 0o600 (owner-only)")

    if sys.platform == "win32" and not _SHELL_USER_CRED.exists():
        errors.append(
            f"shelluser.cred missing at {_SHELL_USER_CRED} — "
            "de-privileged shell principal not provisioned; "
            "engine will fall back to elevated shell (fail-closed required)")

    if errors:
        raise BootACLError(
            "boot ACL check failed:\n  " + "\n  ".join(errors))
