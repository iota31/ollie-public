"""Unit tests for the captcha client (no network).

Mocks the HTTP layer so we test create/poll/extract logic and error paths.
"""

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from unittest.mock import patch
from ollie_hands import captcha as C  # noqa: E402


class FakeResp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


def test_create_and_poll_success_token():
    created = {"taskId": "abc123", "status": "processing"}
    solved = {"status": "solved", "solution": {"gRecaptchaResponse": "TOKEN123"}}

    with patch("ollie_hands.captcha.requests.post", side_effect=[
        type("R", (), {"status_code": 200, "json": (lambda self: created)})(),
        type("R", (), {"status_code": 200, "json": (lambda self: solved)})(),
    ]) as post:
        res = C.solve({"type": "ReCaptchaV2TaskProxyless", "websiteURL": "https://ex", "websiteKey": "k"},
                      client_key="key-xyz", timeout=10)
        assert res["status"] == "solved"
        assert res["solution"]["gRecaptchaResponse"] == "TOKEN123"
        assert post.call_count == 2


def test_poll_until_ready():
    created = {"taskId": "t1", "status": "processing"}
    p1 = {"status": "processing"}
    p2 = {"status": "processing"}
    done = {"status": "solved", "solution": {"token": "T"}}

    with patch.object(C, "_post", side_effect=[created, p1, p2, done]):
        res = C.solve({"type": "HCaptchaTaskProxyless", "websiteURL": "u", "websiteKey": "k"},
                      client_key="k", timeout=30)
        assert res["solution"]["token"] == "T"


def test_failed_status_raises():
    created = {"taskId": "t2"}
    failed = {"status": "failed", "error": "bad"}

    with patch.object(C, "_post", side_effect=[created, failed]):
        try:
            C.solve({"type": "X"}, client_key="k", timeout=10)
            assert False, "should have raised"
        except C.CaptchaError as e:
            assert "failed" in str(e).lower()


def test_timeout_raises():
    created = {"taskId": "t3", "status": "processing"}
    # always processing
    with patch.object(C, "_post", side_effect=[created] + [{"status": "processing"} for _ in range(100)]):
        try:
            C.solve({"type": "X"}, client_key="k", timeout=1)  # very short
            assert False, "should have raised"
        except C.CaptchaError as e:
            assert "timeout" in str(e).lower()


def test_convenience_recaptcha_v2_extracts_token():
    created = {"taskId": "t4"}
    solved = {"status": "solved", "solution": {"gRecaptchaResponse": "GTOK"}}
    with patch.object(C, "_post", side_effect=[created, solved]):
        tok = C.solve_recaptcha_v2_proxyless("https://ex", "SITEKEY", client_key="k", timeout=10)
        assert tok == "GTOK"


def test_image_ocr_extracts_text():
    created = {"taskId": "t5"}
    solved = {"status": "solved", "solution": {"text": "abc123"}}
    with patch.object(C, "_post", side_effect=[created, solved]):
        txt = C.solve_image_ocr("BASE64IMG==", client_key="k", timeout=10)
        assert txt == "abc123"


def test_missing_key_raises_before_network():
    try:
        C.solve({"type": "X"}, client_key="", timeout=5)
        assert False
    except C.CaptchaError as e:
        assert "key" in str(e).lower()


if __name__ == "__main__":
    test_create_and_poll_success_token()
    test_poll_until_ready()
    test_failed_status_raises()
    test_timeout_raises()
    test_convenience_recaptcha_v2_extracts_token()
    test_image_ocr_extracts_text()
    test_missing_key_raises_before_network()
    print("all captcha client tests passed")