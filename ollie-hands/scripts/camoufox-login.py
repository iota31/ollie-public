r"""Headed Camoufox login session — the human-only step that seeds Ollie's
browser identity with Tushar's logins.

The engine (browser.py) drives Camoufox HEADLESS against a persistent profile.
That profile starts empty of logins, so authenticated web tasks have no
session. This script opens the SAME profile with the SAME OS fingerprint but
HEADED, so Tushar can sign into his sites by hand (passwords, 2FA). Cookies
land in the profile and the engine inherits them on its next headless launch.

Run this AS THE ENGINE'S USER (`Source`) in a session with a visible desktop
(physically at the box, or RDP'd in). Do NOT run while the engine is mid
browser task — a Firefox profile can only be open in one process at a time.

    C:\ollie-hands\venv\Scripts\python.exe C:\ollie-hands\scripts\camoufox-login.py [url ...]

Config (PROFILE_DIR / os / humanize) is kept identical to ollie_hands.browser
on purpose: same fingerprint = the logins you make here are the logins the
engine presents.
"""

from __future__ import annotations

import sys

# Must match ollie_hands/browser.py exactly (same profile, same fingerprint).
PROFILE_DIR = r"C:\OllieChrome\camoufox-profile"

DEFAULT_URLS = ["about:blank"]


def main() -> int:
    urls = sys.argv[1:] or DEFAULT_URLS
    from camoufox.sync_api import Camoufox

    print("Launching headed Camoufox on profile:", PROFILE_DIR)
    print("Log in to your sites, then return here and press Enter to close")
    print("(closing this way flushes cookies to the profile cleanly).")

    with Camoufox(headless=False, persistent_context=True,
                  user_data_dir=PROFILE_DIR, os=["windows"],
                  humanize=True, locale="en-US") as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(urls[0])
        for u in urls[1:]:
            ctx.new_page().goto(u)
        try:
            input("\n>>> Press Enter when you're done logging in... ")
        except (EOFError, KeyboardInterrupt):
            pass
    print("Profile saved. The engine will use these logins on its next run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
