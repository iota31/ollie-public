"""Read-only/pre-submit Reddit smoke for the real Windows Camoufox runtime.

This intentionally navigates the real site and fills only a reserved `.invalid`
address. It never clicks Continue, submits a form, solves a CAPTCHA, or uses a
credential. Run it only as an explicit deployment qualification step.
"""

from __future__ import annotations

import json
import time

from ollie_hands import browser


def emit(stage: str, result) -> None:
    print(json.dumps({"stage": stage, "result": result}, default=str))


def main() -> None:
    browser.shutdown()
    try:
        emit("goto", browser.goto("https://www.reddit.com/register/", timeout=45))
        # Reddit initially renders a skeleton. Let its client-side application
        # settle so the test exercises the PageError path from the real site.
        time.sleep(8)
        emit("extract", browser.extract(timeout=20))

        selector = ""
        for candidate in ("input[name='email']", "input[type='email']", "input"):
            try:
                browser.get_attr(candidate, "type")
                selector = candidate
                break
            except Exception:
                continue
        if not selector:
            raise RuntimeError("Reddit rendered no inspectable input after settling")

        emit("input", {"selector": selector})
        browser.fill(selector, "hands-smoke@example.invalid", timeout=20)
        emit("fill_verified", browser.property_matches(
            selector, "value", equals="hands-smoke@example.invalid"
        ))
        emit("status", browser.status())
    finally:
        browser.shutdown()


if __name__ == "__main__":
    main()
