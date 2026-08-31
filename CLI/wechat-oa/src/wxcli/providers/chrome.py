"""Visible-Chrome provider for explicit human browser use only."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, cast

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from wxcli.browser import BrowserProfile, ProfileLock
from wxcli.cache import ArticleCache
from wxcli.errors import ErrorCode, NotFoundError, VerificationRequiredError, WxcliError
from wxcli.evidence import EvidenceService
from wxcli.models import Article, Provider
from wxcli.providers.http import PageKind, WeChatPageClassifier
from wxcli.public_article import PublicArticleDocument, PublicArticleParser
from wxcli.public_url import validate_public_url

CHROME_PATH = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
ARTICLE_TIMEOUT_MS = 30_000
RUN_TIMEOUT_SECONDS = 300.0
LOGIN_TIMEOUT_MS = 300_000
LOCK_TIMEOUT_SECONDS = 5.0


class ChromeRun:
    """One bounded persistent context shared by a single request or batch."""

    def __init__(
        self,
        context: Any,
        provider: ChromeProvider,
        *,
        deadline: float,
    ) -> None:
        self._context = context
        self._provider = provider
        self._deadline = deadline

    def get_document(self, url: str, *, no_cache: bool = False) -> PublicArticleDocument:
        normalized_url = validate_public_url(url)
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise WxcliError(ErrorCode.CHROME_ERROR, "The browser run deadline was reached.")
        timeout_ms = max(1, min(ARTICLE_TIMEOUT_MS, int(remaining * 1000)))
        page = self._context.new_page()
        try:
            page.set_default_timeout(timeout_ms)
            page.goto(
                normalized_url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            html = cast(str, page.content())
        except PlaywrightError as error:
            raise WxcliError(ErrorCode.CHROME_ERROR, "Chrome could not read the requested page.") from error
        finally:
            close = getattr(page, "close", None)
            if callable(close):
                close()

        kind = self._provider.classifier.classify(html)
        if kind is PageKind.VERIFICATION:
            raise VerificationRequiredError(
                verification_stage="browser",
                required_action="run_browser_login",
            )
        if kind is PageKind.NOT_FOUND:
            raise NotFoundError("The public article was not found.")
        if kind is PageKind.ERROR:
            raise WxcliError(ErrorCode.PARSING_ERROR, "The WeChat page is not a readable article.")
        document = PublicArticleParser.parse(html, normalized_url, Provider.CHROME)
        if self._provider.cache and not no_cache:
            self._provider.cache.put(normalized_url, document.article)
        self._provider.browser_profile.record_successful_read()
        return document


class ChromeProvider:
    """Fetch public articles through a visible, independent Chrome profile."""

    def __init__(
        self,
        browser_profile: BrowserProfile,
        playwright_factory: Callable[[], Any] = sync_playwright,
        chrome_path: Path = CHROME_PATH,
        cache: ArticleCache | None = None,
    ) -> None:
        self.browser_profile = browser_profile
        self.playwright_factory = playwright_factory
        self.chrome_path = chrome_path
        self.cache = cache
        self.classifier = WeChatPageClassifier()

    def get(self, url: str, *, no_cache: bool = False) -> Article:
        normalized_url = validate_public_url(url)
        if self.cache and not no_cache:
            if article := self.cache.get(normalized_url):
                return article
        return self.get_document(normalized_url, no_cache=no_cache).article

    def get_document(self, url: str, *, no_cache: bool = False) -> PublicArticleDocument:
        """Open a fresh visible page document for explicitly authorized evidence."""
        normalized_url = validate_public_url(url)
        with self.open_run() as run:
            return run.get_document(normalized_url, no_cache=no_cache)

    @contextmanager
    def open_run(self, *, timeout_seconds: float = RUN_TIMEOUT_SECONDS) -> Iterator[ChromeRun]:
        """Launch one visible persistent context and close it after the bounded run."""
        if not self.chrome_path.is_file():
            raise WxcliError(ErrorCode.CHROME_ERROR, "Google Chrome was not found at the configured path.")
        deadline = time.monotonic() + timeout_seconds
        try:
            with ProfileLock(self.browser_profile.profile, timeout=LOCK_TIMEOUT_SECONDS):
                with self.playwright_factory() as playwright:
                    context = playwright.chromium.launch_persistent_context(
                        user_data_dir=str(self.browser_profile.profile),
                        channel="chrome",
                        headless=False,
                        timeout=min(ARTICLE_TIMEOUT_MS, max(1, int(timeout_seconds * 1000))),
                    )
                    try:
                        yield ChromeRun(context, self, deadline=deadline)
                    finally:
                        context.close()
        except WxcliError:
            raise
        except PlaywrightError as error:
            raise WxcliError(ErrorCode.CHROME_ERROR, "Chrome could not open the requested page.") from error

    def open_login(self) -> None:
        """Open a visible login page for up to five minutes; no cookie is exported."""
        if not self.chrome_path.is_file():
            raise WxcliError(ErrorCode.CHROME_ERROR, "Google Chrome was not found at the configured path.")
        try:
            with ProfileLock(self.browser_profile.profile, timeout=LOCK_TIMEOUT_SECONDS):
                with self.playwright_factory() as playwright:
                    context = playwright.chromium.launch_persistent_context(
                        user_data_dir=str(self.browser_profile.profile),
                        channel="chrome",
                        headless=False,
                        timeout=ARTICLE_TIMEOUT_MS,
                    )
                    try:
                        page = context.new_page()
                        page.set_default_timeout(LOGIN_TIMEOUT_MS)
                        page.goto(
                            "https://mp.weixin.qq.com/",
                            wait_until="domcontentloaded",
                            timeout=ARTICLE_TIMEOUT_MS,
                        )
                        page.wait_for_timeout(LOGIN_TIMEOUT_MS)
                    finally:
                        context.close()
        except WxcliError:
            raise
        except PlaywrightError as error:
            raise WxcliError(ErrorCode.CHROME_ERROR, "Chrome could not open the requested page.") from error


class ChromeEvidenceService(EvidenceService):
    """Article Evidence service that reuses one ChromeRun per Hydration batch."""

    def __init__(self, provider: ChromeProvider) -> None:
        super().__init__(provider)
        self._chrome_provider = provider

    @contextmanager
    def batch(self, *, timeout_seconds: float | None = None) -> Iterator[EvidenceService]:
        effective_timeout = RUN_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
        with self._chrome_provider.open_run(timeout_seconds=effective_timeout) as run:
            yield EvidenceService(run)
