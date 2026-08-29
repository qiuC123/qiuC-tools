"""Visible-Chrome provider for explicit human browser use only."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, cast

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from wxcli.browser import BrowserProfile, ProfileLock
from wxcli.cache import ArticleCache
from wxcli.errors import ErrorCode, NotFoundError, VerificationRequiredError, WxcliError
from wxcli.models import Article, Provider
from wxcli.providers.http import PageKind, WeChatPageClassifier
from wxcli.public_article import PublicArticleDocument, PublicArticleParser
from wxcli.public_url import validate_public_url

CHROME_PATH = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
TIMEOUT_MS = 300_000


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
        html = self._open(normalized_url)
        kind = self.classifier.classify(html)
        if kind is PageKind.VERIFICATION:
            raise VerificationRequiredError()
        if kind is PageKind.NOT_FOUND:
            raise NotFoundError("The public article was not found.")
        if kind is PageKind.ERROR:
            raise WxcliError(ErrorCode.PARSING_ERROR, "The WeChat page is not a readable article.")
        document = PublicArticleParser.parse(html, normalized_url, Provider.CHROME)
        if self.cache and not no_cache:
            self.cache.put(normalized_url, document.article)
        return document

    def open_login(self) -> None:
        """Open a visible login page for up to five minutes; no cookie is exported."""
        self._open("https://mp.weixin.qq.com/", keep_open=True)
        self.browser_profile.record_verification()

    def _open(self, url: str, *, keep_open: bool = False) -> str:
        if not self.chrome_path.is_file():
            raise WxcliError(ErrorCode.CHROME_ERROR, "Google Chrome was not found at the configured path.")
        try:
            with ProfileLock(self.browser_profile.profile):
                with self.playwright_factory() as playwright:
                    context = playwright.chromium.launch_persistent_context(
                        user_data_dir=str(self.browser_profile.profile),
                        channel="chrome",
                        headless=False,
                        timeout=TIMEOUT_MS,
                    )
                    try:
                        page = context.new_page()
                        page.set_default_timeout(TIMEOUT_MS)
                        page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                        if keep_open:
                            page.wait_for_timeout(TIMEOUT_MS)
                        return cast(str, page.content())
                    finally:
                        context.close()
        except WxcliError:
            raise
        except PlaywrightError as error:
            raise WxcliError(ErrorCode.CHROME_ERROR, "Chrome could not open the requested page.") from error
