"""Tests for local browser status, profile locking, and mocked visible Chrome."""

from __future__ import annotations

from pathlib import Path

import pytest
import httpx

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
    profile.record_verification()
    assert profile.status().last_verified_at is not None


def test_lock_rejects_another_process(tmp_path: Path) -> None:
    lock = ProfileLock(tmp_path / "profile")
    with lock, pytest.raises(WxcliError) as raised:
        with ProfileLock(tmp_path / "profile"):
            pass
    assert raised.value.code is ErrorCode.CHROME_ERROR


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
