"""Tests for local browser status, profile locking, and mocked visible Chrome."""

from __future__ import annotations

from pathlib import Path
import json

import pytest
import httpx
from playwright.sync_api import Error as PlaywrightError

import wxcli.browser as browser_module
from wxcli.browser import BrowserProfile, ProfileLock
from wxcli.cache import ArticleCache
from wxcli.errors import ErrorCode, WxcliError
from wxcli.models import Provider
from wxcli.providers.chrome import ChromeProvider
from wxcli.providers.http import PublicHttpProvider


class FakePage:
    def set_default_timeout(self, _: int) -> None: pass
    def goto(self, *_: object, **__: object) -> None: pass
    def content(self) -> str:
        return '<h1 id="activity-name">Chrome</h1><div id="js_content">正文</div>'
    def evaluate(self, _: str, __: object) -> list[str]: return []
    def wait_for_timeout(self, _: int) -> None: pass
    def close(self) -> None: pass


class FakeContext:
    def new_page(self) -> FakePage: return FakePage()
    def close(self) -> None: pass


class FakeChromium:
    def launch_persistent_context(self, **_: object) -> FakeContext: return FakeContext()


class FakePlaywright:
    chromium = FakeChromium()
    def __enter__(self) -> FakePlaywright: return self
    def __exit__(self, *_: object) -> None: pass


def test_status_does_not_require_starting_chrome(tmp_path: Path) -> None:
    profile = BrowserProfile(tmp_path / "profile", tmp_path / "state.json")
    assert profile.status().profile_exists is False
    profile.record_successful_read()
    assert profile.status().last_successful_read_at is not None
    assert profile.status().last_verified_at is None


def test_old_browser_status_migrates_as_legacy_without_claiming_success(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        '{"last_verified_at":"2026-08-01T12:00:00+00:00"}',
        encoding="utf-8",
    )
    profile = BrowserProfile(tmp_path / "profile", state)

    status = profile.status()

    assert status.last_successful_read_at is None
    assert status.legacy_last_verified_at is not None
    migrated = json.loads(state.read_text(encoding="utf-8"))
    assert migrated["schema_version"] == "1"
    assert migrated["last_successful_read_at"] is None


def test_lock_rejects_another_process(tmp_path: Path) -> None:
    lock = ProfileLock(tmp_path / "profile")
    with lock, pytest.raises(WxcliError) as raised:
        with ProfileLock(tmp_path / "profile"):
            pass
    assert raised.value.code is ErrorCode.BROWSER_BUSY


def test_lock_waits_only_for_its_bounded_timeout(tmp_path: Path) -> None:
    lock = ProfileLock(tmp_path / "profile")
    with lock, pytest.raises(WxcliError) as raised:
        with ProfileLock(tmp_path / "profile", timeout=0.01, poll_interval=0.001):
            pass
    assert raised.value.code is ErrorCode.BROWSER_BUSY


def test_mocked_chrome_provider_uses_chrome_model(tmp_path: Path) -> None:
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"stub")
    provider = ChromeProvider(
        BrowserProfile(tmp_path / "profile", tmp_path / "state.json"),
        playwright_factory=FakePlaywright,
        chrome_path=chrome,
    )
    article = provider.get("https://mp.weixin.qq.com/s/token")
    assert article.title == "Chrome"
    assert article.provider is Provider.CHROME


def test_chrome_discovers_runtime_media_after_bounded_article_scan(tmp_path: Path) -> None:
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"stub")
    page = FakePage()
    calls: list[object] = []

    def content() -> str:
        return """
        <h1 id="activity-name">Poster</h1>
        <div id="js_content">
          <img data-src="https://mmbiz.qpic.cn/static.jpg">
          <picture><source srcset="https://mmbiz.qpic.cn/picture.webp 2x"></picture>
          <svg><image href="https://mmbiz.qpic.cn/vector.png"></image></svg>
          <video poster="https://mmbiz.qpic.cn/poster.jpg"></video>
          <div style="background-image:url('https://mmbiz.qpic.cn/background.jpg')"></div>
        </div>
        """

    def evaluate(_: str, options: object) -> list[str]:
        calls.append(options)
        return [
            "https://mmbiz.qpic.cn/static.jpg",
            "https://mmbiz.qpic.cn/lazy.jpg",
            "https://mmbiz.qpic.cn/background.jpg",
        ]

    page.content = content  # type: ignore[method-assign]
    page.evaluate = evaluate  # type: ignore[method-assign]

    class MediaContext(FakeContext):
        def new_page(self) -> FakePage: return page

    class MediaChromium:
        def launch_persistent_context(self, **_: object) -> MediaContext: return MediaContext()

    class MediaPlaywright(FakePlaywright):
        chromium = MediaChromium()

    provider = ChromeProvider(
        BrowserProfile(tmp_path / "profile", tmp_path / "state.json"),
        playwright_factory=MediaPlaywright,
        chrome_path=chrome,
    )

    article = provider.get("https://mp.weixin.qq.com/s/token")

    assert article.images == [
        "https://mmbiz.qpic.cn/static.jpg",
        "https://mmbiz.qpic.cn/lazy.jpg",
        "https://mmbiz.qpic.cn/background.jpg",
        "https://mmbiz.qpic.cn/picture.webp",
        "https://mmbiz.qpic.cn/vector.png",
        "https://mmbiz.qpic.cn/poster.jpg",
    ]
    assert calls == [
        {
            "maxSteps": 80,
            "pauseMs": 75,
            "stableRounds": 3,
            "maxElements": 2000,
            "maxImages": 200,
        }
    ]


def test_chrome_keeps_static_article_when_optional_media_scan_fails(tmp_path: Path) -> None:
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"stub")
    page = FakePage()
    closed = False

    def content() -> str:
        return """
        <h1 id="activity-name">Static article</h1>
        <div id="js_content"><img data-src="https://mmbiz.qpic.cn/static.jpg"></div>
        """

    def evaluate(_: str, __: object) -> list[str]:
        raise PlaywrightError("optional scan failed")

    def close() -> None:
        nonlocal closed
        closed = True

    page.content = content  # type: ignore[method-assign]
    page.evaluate = evaluate  # type: ignore[method-assign]
    page.close = close  # type: ignore[method-assign]

    class ScanFailureContext(FakeContext):
        def new_page(self) -> FakePage: return page

    class ScanFailureChromium:
        def launch_persistent_context(self, **_: object) -> ScanFailureContext:
            return ScanFailureContext()

    class ScanFailurePlaywright(FakePlaywright):
        chromium = ScanFailureChromium()

    provider = ChromeProvider(
        BrowserProfile(tmp_path / "profile", tmp_path / "state.json"),
        playwright_factory=ScanFailurePlaywright,
        chrome_path=chrome,
    )

    article = provider.get("https://mp.weixin.qq.com/s/token")

    assert article.title == "Static article"
    assert article.images == ["https://mmbiz.qpic.cn/static.jpg"]
    assert closed is True


def test_chrome_success_is_shared_with_http_cache(tmp_path: Path) -> None:
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"stub")
    cache = ArticleCache(tmp_path / "cache")
    url = "https://mp.weixin.qq.com/s/token"
    chrome_provider = ChromeProvider(
        BrowserProfile(tmp_path / "profile", tmp_path / "state.json"),
        playwright_factory=FakePlaywright,
        chrome_path=chrome,
        cache=cache,
    )

    chrome_article = chrome_provider.get(url)
    transport = httpx.MockTransport(lambda request: pytest.fail("cache must avoid HTTP"))
    with httpx.Client(transport=transport) as client:
        cached_article = PublicHttpProvider(client, cache).get(url)

    assert cached_article == chrome_article
    assert cached_article.provider is Provider.CHROME


def test_chrome_no_cache_neither_reads_nor_writes_cache(tmp_path: Path) -> None:
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"stub")
    cache = ArticleCache(tmp_path / "cache")
    url = "https://mp.weixin.qq.com/s/token"
    provider = ChromeProvider(
        BrowserProfile(tmp_path / "profile", tmp_path / "state.json"),
        playwright_factory=FakePlaywright,
        chrome_path=chrome,
        cache=cache,
    )

    provider.get(url, no_cache=True)

    assert cache.get(url) is None


def test_one_chrome_run_reuses_context_and_fresh_pages(tmp_path: Path) -> None:
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"stub")
    context = FakeContext()

    class CountingChromium:
        launches = 0
        def launch_persistent_context(self, **_: object) -> FakeContext:
            self.launches += 1
            return context

    chromium = CountingChromium()

    class CountingPlaywright:
        def __init__(self) -> None:
            self.chromium = chromium
        def __enter__(self) -> CountingPlaywright: return self
        def __exit__(self, *_: object) -> None: pass

    provider = ChromeProvider(
        BrowserProfile(tmp_path / "profile", tmp_path / "state.json"),
        playwright_factory=CountingPlaywright,
        chrome_path=chrome,
    )
    with provider.open_run() as run:
        first = run.get_document("https://mp.weixin.qq.com/s/T1")
        second = run.get_document("https://mp.weixin.qq.com/s/T2")

    assert chromium.launches == 1
    assert first.article.title == second.article.title == "Chrome"
    assert provider.browser_profile.status().last_successful_read_at is not None


def test_browser_login_does_not_claim_a_successful_article_read(tmp_path: Path) -> None:
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"stub")
    profile = BrowserProfile(tmp_path / "profile", tmp_path / "state.json")
    provider = ChromeProvider(
        profile,
        playwright_factory=FakePlaywright,
        chrome_path=chrome,
    )

    provider.open_login()

    assert profile.status().last_successful_read_at is None


def test_interactive_verification_waits_for_exact_article_and_returns_document(
    tmp_path: Path,
) -> None:
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"stub")
    page = FakePage()
    snapshots = iter(
        [
            "<p>请完成安全验证</p>",
            '<h1 id="activity-name">Verified</h1><div id="js_content">正文</div>',
            '<h1 id="activity-name">Verified</h1><div id="js_content">正文</div>',
        ]
    )
    observed_waits: list[int] = []

    page.content = lambda: next(snapshots)  # type: ignore[method-assign]
    page.wait_for_timeout = observed_waits.append  # type: ignore[method-assign]

    class InteractiveContext(FakeContext):
        def new_page(self) -> FakePage: return page

    class InteractiveChromium:
        def launch_persistent_context(self, **_: object) -> InteractiveContext:
            return InteractiveContext()

    class InteractivePlaywright(FakePlaywright):
        chromium = InteractiveChromium()

    profile = BrowserProfile(tmp_path / "profile", tmp_path / "state.json")
    provider = ChromeProvider(
        profile,
        playwright_factory=InteractivePlaywright,
        chrome_path=chrome,
    )

    document = provider.get_document_interactive("https://mp.weixin.qq.com/s/T1")

    assert document.article.title == "Verified"
    assert observed_waits == [500]
    assert profile.status().last_successful_read_at is not None


def test_interactive_verification_reports_closed_window(tmp_path: Path) -> None:
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"stub")

    class ClosedPage(FakePage):
        def content(self) -> str: return "<p>请完成安全验证</p>"
        def is_closed(self) -> bool: return True

    class ClosedContext(FakeContext):
        def new_page(self) -> ClosedPage: return ClosedPage()

    class ClosedChromium:
        def launch_persistent_context(self, **_: object) -> ClosedContext:
            return ClosedContext()

    class ClosedPlaywright(FakePlaywright):
        chromium = ClosedChromium()

    provider = ChromeProvider(
        BrowserProfile(tmp_path / "profile", tmp_path / "state.json"),
        playwright_factory=ClosedPlaywright,
        chrome_path=chrome,
    )

    with pytest.raises(WxcliError) as raised:
        provider.get_document_interactive("https://mp.weixin.qq.com/s/T1")

    assert raised.value.code is ErrorCode.VERIFICATION_REQUIRED
    assert raised.value.details["verification_outcome"] == "window_closed"


def test_interactive_verification_has_a_hard_deadline(tmp_path: Path) -> None:
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"stub")

    class VerificationPage(FakePage):
        def content(self) -> str: return "<p>请完成安全验证</p>"

    class VerificationContext(FakeContext):
        def new_page(self) -> VerificationPage: return VerificationPage()

    class VerificationChromium:
        def launch_persistent_context(self, **_: object) -> VerificationContext:
            return VerificationContext()

    class VerificationPlaywright(FakePlaywright):
        chromium = VerificationChromium()

    timestamps = iter([0.0, 0.0, 1.0])
    provider = ChromeProvider(
        BrowserProfile(tmp_path / "profile", tmp_path / "state.json"),
        playwright_factory=VerificationPlaywright,
        chrome_path=chrome,
        monotonic=lambda: next(timestamps),
    )

    with pytest.raises(WxcliError) as raised:
        provider.get_document_interactive(
            "https://mp.weixin.qq.com/s/T1",
            timeout_seconds=0.5,
        )

    assert raised.value.code is ErrorCode.VERIFICATION_REQUIRED
    assert raised.value.details["verification_outcome"] == "timeout"


def test_browser_clear_removes_session_state_only(tmp_path: Path) -> None:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    (profile_dir / "cookie-store").write_text("browser-owned", encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text("{}", encoding="utf-8")
    unrelated_policy = tmp_path / "browser-policy.json"
    unrelated_policy.write_text("policy", encoding="utf-8")

    BrowserProfile(profile_dir, state).clear()

    assert not profile_dir.exists()
    assert not state.exists()
    assert unrelated_policy.read_text(encoding="utf-8") == "policy"


def test_browser_state_write_failure_is_safe_and_removes_temporary_file(
    tmp_path: Path, monkeypatch
) -> None:
    profile = BrowserProfile(tmp_path / "profile", tmp_path / "state.json")
    monkeypatch.setattr(
        browser_module.os,
        "replace",
        lambda *args: (_ for _ in ()).throw(OSError("disk")),
    )

    with pytest.raises(WxcliError) as raised:
        profile.record_successful_read()

    assert raised.value.code is ErrorCode.LOCAL_CONFIGURATION_ERROR
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize(
    ("html", "error_code"),
    [
        ("<p>请完成安全验证</p>", ErrorCode.VERIFICATION_REQUIRED),
        ("<p>页面不存在</p>", ErrorCode.NOT_FOUND),
        ("<p>unexpected</p>", ErrorCode.PARSING_ERROR),
    ],
)
def test_chrome_run_classifies_non_article_pages(
    tmp_path: Path, html: str, error_code: ErrorCode
) -> None:
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"stub")

    class HtmlPage(FakePage):
        def content(self) -> str: return html
        def evaluate(self, _: str, __: object) -> list[str]:
            pytest.fail("non-article pages must not run the media scan")

    class HtmlContext(FakeContext):
        def new_page(self) -> HtmlPage: return HtmlPage()

    class HtmlChromium:
        def launch_persistent_context(self, **_: object) -> HtmlContext: return HtmlContext()

    class HtmlPlaywright(FakePlaywright):
        chromium = HtmlChromium()

    provider = ChromeProvider(
        BrowserProfile(tmp_path / "profile", tmp_path / "state.json"),
        playwright_factory=HtmlPlaywright,
        chrome_path=chrome,
    )

    with pytest.raises(WxcliError) as raised:
        provider.get_document("https://mp.weixin.qq.com/s/T1")

    assert raised.value.code is error_code
    if error_code is ErrorCode.VERIFICATION_REQUIRED:
        assert raised.value.details == {
            "verification_stage": "browser",
            "required_action": "run_browser_login",
        }


def test_chrome_run_deadline_and_playwright_failure_are_structured(tmp_path: Path) -> None:
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"stub")
    provider = ChromeProvider(
        BrowserProfile(tmp_path / "profile", tmp_path / "state.json"),
        playwright_factory=FakePlaywright,
        chrome_path=chrome,
    )
    with provider.open_run(timeout_seconds=0) as run:
        with pytest.raises(WxcliError) as deadline:
            run.get_document("https://mp.weixin.qq.com/s/T1")
    assert deadline.value.code is ErrorCode.CHROME_ERROR

    class BrokenPage(FakePage):
        closed = False
        def goto(self, *_: object, **__: object) -> None:
            raise PlaywrightError("page crashed")
        def close(self) -> None:
            self.closed = True

    class BrokenContext(FakeContext):
        page = BrokenPage()
        def new_page(self) -> BrokenPage: return self.page

    broken_context = BrokenContext()

    class BrokenChromium:
        def launch_persistent_context(self, **_: object) -> BrokenContext: return broken_context

    class BrokenPlaywright(FakePlaywright):
        chromium = BrokenChromium()

    broken = ChromeProvider(
        BrowserProfile(tmp_path / "broken-profile", tmp_path / "broken-state.json"),
        playwright_factory=BrokenPlaywright,
        chrome_path=chrome,
    )
    with pytest.raises(WxcliError) as failed:
        broken.get_document("https://mp.weixin.qq.com/s/T1")
    assert failed.value.code is ErrorCode.CHROME_ERROR
    assert broken_context.page.closed is True


def test_missing_chrome_is_a_structured_error(tmp_path: Path) -> None:
    provider = ChromeProvider(
        BrowserProfile(tmp_path / "profile", tmp_path / "state.json"),
        chrome_path=tmp_path / "missing.exe",
    )
    with pytest.raises(WxcliError) as article:
        provider.get_document("https://mp.weixin.qq.com/s/T1")
    with pytest.raises(WxcliError) as login:
        provider.open_login()
    assert article.value.code is ErrorCode.CHROME_ERROR
    assert login.value.code is ErrorCode.CHROME_ERROR
