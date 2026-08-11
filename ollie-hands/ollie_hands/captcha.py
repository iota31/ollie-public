"""CAPTCHA solving via noCaptchaAI (external service).

Thin, auditable client. The engine exposes this as kind="captcha" (and a
dedicated solve_captcha tool) so every solve goes through policy + audit.

Supported usage:
- Token tasks (ReCaptchaV2TaskProxyless, ReCaptchaV3TaskProxyless, hCaptcha,
  Turnstile, etc.): service returns a solution token you inject into the page.
- Image classification / OCR: service returns answers/indices/text.

Flow: createTask -> poll getTaskResult until solved/failed.

Key is host-side only (cfg.nocaptcha_api_key). Never sent to the brain.
"""

from __future__ import annotations

import time
from typing import Any

import requests

API_BASE = "https://api.nocaptchaai.com"
DEFAULT_TIMEOUT = 90
POLL_INTERVAL = 2.0
MAX_POLLS = 60  # ~2 minutes at 2s interval


class CaptchaError(Exception):
    """Raised for service errors, bad keys, or unsolved results."""


def _post(path: str, payload: dict[str, Any], *, timeout: int = 30) -> dict[str, Any]:
    url = f"{API_BASE}{path}"
    try:
        r = requests.post(url, json=payload, timeout=timeout)
    except requests.RequestException as e:
        raise CaptchaError(f"network error calling {path}: {e}") from e
    if r.status_code >= 400:
        # surface the body for debugging without leaking the key
        body = (r.text or "")[:500]
        raise CaptchaError(f"HTTP {r.status_code} from {path}: {body}")
    try:
        data = r.json()
    except Exception:
        raise CaptchaError(f"non-JSON response from {path}: {(r.text or '')[:200]}")
    return data


def create_task(client_key: str, task: dict[str, Any]) -> dict[str, Any]:
    """Create a solve task. Returns {"taskId": "...", "status": "...", ...}."""
    if not client_key:
        raise CaptchaError("noCaptchaAI clientKey is not configured")
    payload = {
        "clientKey": client_key,
        "source": "ollie-hands",
        "version": "1.0.0",
        "task": task,
    }
    return _post("/createTask", payload)


def get_task_result(client_key: str, task_id: str) -> dict[str, Any]:
    """Poll result for a taskId. Returns {"status": "solved|processing|failed", "solution": ...}."""
    if not client_key:
        raise CaptchaError("noCaptchaAI clientKey is not configured")
    payload = {"clientKey": client_key, "taskId": task_id}
    return _post("/getTaskResult", payload)


def solve(task: dict[str, Any], *, client_key: str, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """
    High-level helper: create + poll until solved or timeout.

    Returns the final result dict from getTaskResult (contains "solution" on success).
    Raises CaptchaError on failure/timeout.
    """
    if not client_key:
        raise CaptchaError("noCaptchaAI clientKey is not configured on the host")

    created = create_task(client_key, task)
    task_id = created.get("taskId") or created.get("id")
    if not task_id:
        raise CaptchaError(f"createTask did not return taskId: {created}")

    status = created.get("status") or "processing"
    start = time.monotonic()
    polls = 0

    while status not in ("solved", "ready") and polls < MAX_POLLS:
        if time.monotonic() - start > timeout:
            raise CaptchaError(f"timeout after {timeout}s waiting for task {task_id}")
        time.sleep(POLL_INTERVAL)
        res = get_task_result(client_key, task_id)
        status = res.get("status") or status
        if status in ("solved", "ready"):
            return res
        if status in ("failed", "error"):
            raise CaptchaError(f"solve failed: {res}")
        polls += 1

    # final check
    res = get_task_result(client_key, task_id)
    if (res.get("status") or "") in ("solved", "ready"):
        return res
    raise CaptchaError(f"unsolved after polling: {res}")


# ---------------- convenience wrappers (optional for common cases) ------------

def solve_recaptcha_v2_proxyless(
    website_url: str, website_key: str, *, client_key: str, timeout: int = DEFAULT_TIMEOUT
) -> str:
    """Returns the g-recaptcha-response token string."""
    task = {"type": "ReCaptchaV2TaskProxyless", "websiteURL": website_url, "websiteKey": website_key}
    res = solve(task, client_key=client_key, timeout=timeout)
    sol = res.get("solution") or {}
    token = sol.get("gRecaptchaResponse") or sol.get("token") or sol.get("recaptchaToken")
    if not token:
        raise CaptchaError(f"no token in solution: {res}")
    return token


def solve_recaptcha_v3_proxyless(
    website_url: str,
    website_key: str,
    *,
    client_key: str,
    action: str | None = None,
    min_score: float | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    task: dict[str, Any] = {"type": "ReCaptchaV3TaskProxyless", "websiteURL": website_url, "websiteKey": website_key}
    if action:
        task["pageAction"] = action
    if min_score is not None:
        task["minScore"] = min_score
    res = solve(task, client_key=client_key, timeout=timeout)
    sol = res.get("solution") or {}
    token = sol.get("gRecaptchaResponse") or sol.get("token")
    if not token:
        raise CaptchaError(f"no token in solution: {res}")
    return token


def solve_hcaptcha_proxyless(
    website_url: str, website_key: str, *, client_key: str, timeout: int = DEFAULT_TIMEOUT
) -> str:
    task = {"type": "HCaptchaTaskProxyless", "websiteURL": website_url, "websiteKey": website_key}
    res = solve(task, client_key=client_key, timeout=timeout)
    sol = res.get("solution") or {}
    token = sol.get("gRecaptchaResponse") or sol.get("token") or sol.get("hCaptchaResponse")
    if not token:
        raise CaptchaError(f"no token in solution: {res}")
    return token


def solve_image_ocr(image_b64: str, *, client_key: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Simple text/OCR image CAPTCHA. Returns the recognized text."""
    task = {"type": "TextCaptchaTaskProxyless", "image": image_b64}
    res = solve(task, client_key=client_key, timeout=timeout)
    sol = res.get("solution") or {}
    text = sol.get("text") or sol.get("answer") or sol.get("captcha")
    if not text:
        raise CaptchaError(f"no text in solution: {res}")
    return text


def solve_recaptcha_v2_classification(
    images: list[str], question_type: str | int, *, client_key: str, timeout: int = DEFAULT_TIMEOUT
) -> list[int] | dict[str, Any]:
    """
    Grid/image classification (the "click all tiles" reCAPTCHA v2).
    `images` are base64-encoded image bytes (without data: prefix).
    Returns the service's answer (often a list of selected indices or a dict).
    """
    task = {"type": "ReCaptchaV2Classification", "questionType": str(question_type), "image": images}
    res = solve(task, client_key=client_key, timeout=timeout)
    sol = res.get("solution") or res.get("answer") or {}
    # Common shapes: list of indices, or {"answer": [...]}
    if isinstance(sol, list):
        return sol
    if isinstance(sol, dict):
        if "answer" in sol:
            return sol["answer"]
        return sol
    return sol
