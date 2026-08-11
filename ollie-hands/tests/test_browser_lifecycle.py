import asyncio
import sys
import threading
import types

from ollie_hands import browser


class FakeKeyboard:
    async def press(self, key):
        return None


class FakePage:
    def __init__(self, fail_fill=False, fail_click=False):
        self.url = "about:blank"
        self.calls = []
        self.thread_ids = []
        self.keyboard = FakeKeyboard()
        self.fail_fill = fail_fill
        self.fail_click = fail_click

    def _record(self, name):
        self.calls.append(name)
        self.thread_ids.append(threading.get_ident())

    async def evaluate(self, expression, *args):
        self._record("evaluate")
        if expression == "1":
            return 1
        return "page text"

    async def goto(self, url, **kwargs):
        self._record("goto")
        self.url = url

    async def title(self):
        self._record("title")
        return "Fake title"

    async def screenshot(self, **kwargs):
        self._record("screenshot")

    async def wait_for_selector(self, *args, **kwargs):
        self._record("wait_for_selector")

    async def fill(self, selector, value, **kwargs):
        self._record("fill")
        if self.fail_fill:
            self.fail_fill = False
            raise RuntimeError("Connection closed while reading from the driver")

    async def click(self, selector, **kwargs):
        self._record("click")
        if self.fail_click:
            raise RuntimeError("Target page, context or browser has been closed")

    async def eval_on_selector(self, selector, expression):
        self._record("eval_on_selector")
        return "present"


class FakeContext:
    def __init__(self, page):
        self.pages = [page]


class FakeCamoufox:
    instances = []
    fail_first_fill = False
    fail_first_click = False

    def __init__(self, **kwargs):
        first = not self.__class__.instances
        self.page = FakePage(
            fail_fill=first and self.__class__.fail_first_fill,
            fail_click=first and self.__class__.fail_first_click,
        )
        self.context = FakeContext(self.page)
        self.closed = False
        self.__class__.instances.append(self)

    async def __aenter__(self):
        return self.context

    async def __aexit__(self, *args):
        self.closed = True


def _install_fake_camoufox(monkeypatch):
    FakeCamoufox.instances.clear()
    FakeCamoufox.fail_first_fill = False
    FakeCamoufox.fail_first_click = False
    package = types.ModuleType("camoufox")
    async_api = types.ModuleType("camoufox.async_api")
    async_api.AsyncCamoufox = FakeCamoufox
    package.async_api = async_api
    monkeypatch.setitem(sys.modules, "camoufox", package)
    monkeypatch.setitem(sys.modules, "camoufox.async_api", async_api)


def test_sequential_browser_lifecycle_stays_on_one_loop(monkeypatch, tmp_path):
    browser.shutdown()
    _install_fake_camoufox(monkeypatch)

    assert browser.goto("https://one.example")["url"] == "https://one.example"
    assert browser.screenshot(str(tmp_path / "page.png"))["url"] == "https://one.example"
    assert browser.extract()["text"] == "page text"
    assert browser.goto("https://two.example")["url"] == "https://two.example"

    assert len(FakeCamoufox.instances) == 1
    page = FakeCamoufox.instances[0].page
    assert page.calls.count("goto") == 2
    assert len(set(page.thread_ids)) == 1
    assert page.thread_ids[0] != threading.get_ident()
    browser.shutdown()


def test_sync_browser_api_is_safe_inside_asyncio_context(monkeypatch):
    browser.shutdown()
    _install_fake_camoufox(monkeypatch)

    async def use_browser():
        return browser.goto("https://async-caller.example")

    result = asyncio.run(use_browser())
    assert result["url"] == "https://async-caller.example"
    assert len(FakeCamoufox.instances) == 1
    browser.shutdown()


def test_safe_fill_rebuilds_and_retries_once_after_transport_death(monkeypatch):
    browser.shutdown()
    _install_fake_camoufox(monkeypatch)
    FakeCamoufox.fail_first_fill = True

    result = browser.fill("#email", "person@example.com")

    assert result["filled"] == "#email"
    assert len(FakeCamoufox.instances) == 2
    assert FakeCamoufox.instances[0].closed is True
    assert FakeCamoufox.instances[1].page.calls.count("fill") == 1
    browser.shutdown()


def test_click_is_never_retried_after_transport_death(monkeypatch):
    browser.shutdown()
    _install_fake_camoufox(monkeypatch)
    FakeCamoufox.fail_first_click = True

    try:
        browser.click("#submit")
    except RuntimeError as exc:
        assert "closed" in str(exc).lower()
    else:
        raise AssertionError("transport failure should escape commit-like click")

    assert len(FakeCamoufox.instances) == 1
    assert FakeCamoufox.instances[0].closed is True
    browser.shutdown()


def test_property_match_does_not_return_dom_value(monkeypatch):
    browser.shutdown()
    _install_fake_camoufox(monkeypatch)

    result = browser.property_matches("#password", "value", nonempty=True)

    assert result["matched"] is True
    assert result["nonempty"] is True
    assert "value" not in result
    browser.shutdown()


def test_browser_is_never_launched_headless():
    """HARD PROJECT RULE: the L2 rung must be a VISIBLE stealth browser.

    A headless browser is trivially fingerprintable, which defeats the entire
    point of using Camoufox. This has regressed once already during a merge,
    so it is asserted against the source text of the launch call rather than
    against a live launch (camoufox is not installed on CI/dev machines).
    """
    import ast
    import inspect
    import pathlib

    src = pathlib.Path(browser.__file__).read_text()
    assert "headless=True" not in src, "the browser must never launch headless"

    # Pin it precisely: the AsyncCamoufox(...) call must pass headless=False.
    tree = ast.parse(src)
    launches = [node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "AsyncCamoufox"]
    assert launches, "could not find the AsyncCamoufox launch call"
    for call in launches:
        headless = [kw for kw in call.keywords if kw.arg == "headless"]
        assert headless, "AsyncCamoufox must pass headless explicitly"
        assert headless[0].value.value is False

    # And the launcher really is the function that starts the browser.
    assert "AsyncCamoufox" in inspect.getsource(browser._ensure_started)
