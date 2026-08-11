"""L2 — browser automation through Camoufox's async Playwright API.

The engine exposes synchronous browser verbs, but Camoufox and every object it
creates live on one persistent asyncio loop in a dedicated thread.  Keeping
creation and use on that loop avoids Playwright's sync/async-loop mismatch and
preserves the simple synchronous interface used by the rest of Ollie Hands.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading

PROFILE_DIR = r"C:\OllieChrome\camoufox-profile"

_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_loop_ready = threading.Event()
_loop_lock = threading.Lock()
_operation_lock: asyncio.Lock | None = None

log = logging.getLogger(__name__)

# Live browser state. These objects are only created and touched on _loop.
_cm = None
_ctx = None
_page = None


def _loop_main() -> None:
    global _loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _loop = loop
    _loop_ready.set()
    loop.run_forever()


def _browser_loop() -> asyncio.AbstractEventLoop:
    global _loop_thread
    with _loop_lock:
        if _loop_thread is None or not _loop_thread.is_alive():
            _loop_ready.clear()
            _loop_thread = threading.Thread(
                target=_loop_main, name="camoufox-async", daemon=True
            )
            _loop_thread.start()
    _loop_ready.wait()
    assert _loop is not None
    return _loop


def _transport_closed(exc: BaseException) -> bool:
    """True only for failures which mean the Playwright transport is dead."""
    message = str(exc).lower()
    return any(marker in message for marker in (
        "connection closed while reading from the driver",
        "target page, context or browser has been closed",
        "target page/context/browser has been closed",
        "browser has been closed",
        "browser closed",
        "connection closed",
    ))


def transport_closed(exc: BaseException) -> bool:
    """Public name for _transport_closed; used by the executor to classify
    dispatch/verification failures as outcome_unknown rather than error."""
    return _transport_closed(exc)


async def _run_operation(fn, args, kwargs, *, retry_safe: bool):
    global _operation_lock
    if _operation_lock is None:
        _operation_lock = asyncio.Lock()
    async with _operation_lock:
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            if not _transport_closed(exc):
                raise
            log.exception("Camoufox transport failed during %s; resetting context",
                          fn.__name__)
            await _close_browser()
            if not retry_safe:
                raise
            log.warning("Retrying safe browser operation %s once", fn.__name__)
            return await fn(*args, **kwargs)


def _on_browser_loop(fn=None, *, retry_safe: bool = False):
    """Expose an async browser operation as a synchronous engine function."""

    if fn is None:
        return lambda wrapped: _on_browser_loop(wrapped, retry_safe=retry_safe)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        future = asyncio.run_coroutine_threadsafe(
            _run_operation(fn, args, kwargs, retry_safe=retry_safe),
            _browser_loop(),
        )
        return future.result()

    return wrapper


async def _ensure_started() -> None:
    global _cm, _ctx, _page
    if _ctx is not None:
        return
    from camoufox.async_api import AsyncCamoufox

    _cm = AsyncCamoufox(
        # HARD PROJECT RULE: never headless. A headless browser is trivially
        # detectable and defeats the point of the stealth (Camoufox) rung.
        # This matches the deployed box. Do not "optimize" this back to True.
        headless=False,
        persistent_context=True,
        user_data_dir=PROFILE_DIR,
        os=["windows"],
        humanize=True,
        locale="en-US",
    )
    _ctx = await _cm.__aenter__()
    _page = _ctx.pages[0] if _ctx.pages else await _ctx.new_page()


async def _close_browser() -> None:
    global _cm, _ctx, _page
    if _cm is not None:
        try:
            await _cm.__aexit__(None, None, None)
        except Exception:
            pass
    _cm = _ctx = _page = None


async def _cur_page():
    """Return a live page, rebuilding Camoufox after a browser crash."""
    if _ctx is not None and _page is not None:
        try:
            await _page.evaluate("1")
            return _page
        except Exception:
            await _close_browser()
    await _ensure_started()
    return _page


# ------------------------------------------------------------- read verbs ---

@_on_browser_loop(retry_safe=True)
async def goto(url: str, timeout: int = 30) -> dict:
    page = await _cur_page()
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
    return {"url": page.url, "title": await page.title()}


@_on_browser_loop(retry_safe=True)
async def extract(selector: str = "", timeout: int = 15) -> dict:
    page = await _cur_page()
    if selector:
        try:
            await page.wait_for_selector(
                selector, state="attached", timeout=timeout * 1000
            )
        except Exception:
            pass
        text = await page.evaluate(
            "(sel) => Array.from(document.querySelectorAll(sel))"
            ".map(el => el.innerText).filter(Boolean).join('\\n')",
            selector,
        )
    else:
        text = await page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )
    return {
        "url": page.url,
        "selector": selector or "body",
        "text": (text or "")[:8000],
    }


@_on_browser_loop(retry_safe=True)
async def links(limit: int = 40) -> dict:
    page = await _cur_page()
    data = await page.evaluate(
        "(n) => Array.from(document.querySelectorAll('a[href]'))"
        ".slice(0,n).map(a => ({text:(a.innerText||'').trim().slice(0,80),"
        "href:a.href}))",
        limit,
    )
    return {"url": page.url, "links": data}


@_on_browser_loop(retry_safe=True)
async def screenshot(save_path: str) -> dict:
    page = await _cur_page()
    await page.screenshot(path=save_path, full_page=False)
    return {"url": page.url, "path": save_path}


@_on_browser_loop(retry_safe=True)
async def get_attr(selector: str, attr: str) -> dict:
    page = await _cur_page()
    value = await page.eval_on_selector(
        selector, f"(el) => el.getAttribute({attr!r})"
    )
    return {"selector": selector, "attr": attr, "value": value}


@_on_browser_loop(retry_safe=True)
async def property_matches(selector: str, prop: str, *, equals=None,
                           contains=None, nonempty: bool = False) -> dict:
    """Check a live DOM property without returning its potentially secret value."""
    page = await _cur_page()
    value = await page.eval_on_selector(selector, f"(el) => el[{prop!r}]")
    text = "" if value is None else str(value)
    if equals is not None:
        matched = text == str(equals)
    elif contains is not None:
        matched = str(contains) in text
    elif nonempty:
        matched = bool(text)
    else:
        matched = value is not None
    return {"selector": selector, "property": prop, "matched": matched,
            "nonempty": bool(text)}


# ------------------------------------------------------ interaction verbs ---

@_on_browser_loop
async def click(selector: str, timeout: int = 15) -> dict:
    page = await _cur_page()
    await page.click(selector, timeout=timeout * 1000)
    return {"url": page.url, "clicked": selector}


@_on_browser_loop(retry_safe=True)
async def fill(selector: str, value: str, timeout: int = 15) -> dict:
    page = await _cur_page()
    await page.fill(selector, value, timeout=timeout * 1000)
    return {"url": page.url, "filled": selector}


@_on_browser_loop
async def type_text(selector: str, value: str, timeout: int = 15) -> dict:
    page = await _cur_page()
    await page.type(selector, value, timeout=timeout * 1000)
    return {"url": page.url, "typed": selector}


@_on_browser_loop
async def press(key: str) -> dict:
    page = await _cur_page()
    await page.keyboard.press(key)
    return {"url": page.url, "pressed": key}


@_on_browser_loop(retry_safe=True)
async def element_text(selector: str) -> str:
    page = await _cur_page()
    try:
        element = await page.query_selector(selector)
        return (await element.inner_text() if element else "") or ""
    except Exception:
        return ""


# -------------------------------------------------------------- lifecycle ---

@_on_browser_loop
async def status() -> dict:
    started = _ctx is not None
    return {
        "started": started,
        "url": _page.url if started and _page else None,
        "profile": PROFILE_DIR,
    }


@_on_browser_loop
async def shutdown() -> dict:
    await _close_browser()
    return {"closed": True}
