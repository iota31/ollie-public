"""Real Camoufox lifecycle deploy gate using only a loopback HTML form.

This intentionally does not mock Camoufox or Playwright.  It is skipped when
not running on Windows or when Camoufox is not installed; on a provisioned
Hands host, a missing/broken browser binary is a test failure.
"""

from __future__ import annotations

import contextlib
import http.server
import importlib.util
import sys
import threading

import pytest

from ollie_hands import browser


pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="real Camoufox gate runs on the Windows Hands host"
)


FORM = b"""<!doctype html>
<html><head><title>Hands browser lifecycle</title></head>
<body>
  <label>Email <input id="email" name="email" type="email"></label>
  <output id="mirror"></output>
  <script>
    const email = document.querySelector('#email');
    email.addEventListener('input', () => {
      document.querySelector('#mirror').textContent = email.value;
    });
  </script>
</body></html>"""


class _FormHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(FORM)))
        self.end_headers()
        self.wfile.write(FORM)

    def log_message(self, _format, *args):
        pass


@contextlib.contextmanager
def _loopback_form():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FormHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/form"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_camoufox_survives_sequential_goto_extract_and_fills(
        monkeypatch, tmp_path):
    if importlib.util.find_spec("camoufox") is None:
        pytest.skip("Camoufox is not installed")

    # Never share or lock the service's persistent profile during this gate.
    monkeypatch.setattr(browser, "PROFILE_DIR", str(tmp_path / "camoufox-profile"))
    browser.shutdown()
    try:
        with _loopback_form() as url:
            assert browser.goto(url)["title"] == "Hands browser lifecycle"
            assert browser.extract("#email")["url"] == url

            browser.fill("#email", "first@example.invalid")
            assert browser.extract("#mirror")["text"] == "first@example.invalid"

            browser.fill("#email", "second@example.invalid")
            assert browser.extract("#mirror")["text"] == "second@example.invalid"

            state = browser.status()
            assert state["started"] is True
            assert state["url"] == url
    finally:
        browser.shutdown()
