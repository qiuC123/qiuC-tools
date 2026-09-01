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
MEDIA_SCAN_MAX_MS = 7_000
MEDIA_SCAN_PAUSE_MS = 75
MEDIA_SCAN_MAX_STEPS = 80
MEDIA_SCAN_STABLE_ROUNDS = 3
MEDIA_SCAN_MAX_ELEMENTS = 2_000
MEDIA_SCAN_MAX_IMAGES = 200

_ARTICLE_MEDIA_SCAN_SCRIPT = r"""
async ({ maxSteps, pauseMs, stableRounds, maxElements, maxImages }) => {
  const root = document.querySelector("#js_content");
  if (!root) return [];

  const images = [];
  const seen = new Set();
  const add = (raw) => {
    if (typeof raw !== "string" || raw.length === 0 || raw.length > 4096) return;
    try {
      const parsed = new URL(raw, document.baseURI);
      if (parsed.protocol !== "https:" || parsed.username || parsed.password) return;
      const value = parsed.href;
      if (!seen.has(value) && images.length < maxImages) {
        seen.add(value);
        images.push(value);
      }
    } catch (_) {}
  };
  const addSrcset = (raw) => {
    if (typeof raw !== "string") return;
    for (const part of raw.split(",")) add(part.trim().split(/\s+/)[0]);
  };
  const addBackgrounds = (raw) => {
    if (typeof raw !== "string" || raw === "none") return;
    const pattern = /url\(\s*(["']?)(.*?)\1\s*\)/gi;
    for (const match of raw.matchAll(pattern)) add(match[2]);
  };
  const collect = () => {
    const elements = root.querySelectorAll("*");
    const count = Math.min(elements.length, maxElements);
    for (let index = 0; index < count && images.length < maxImages; index += 1) {
      const node = elements[index];
      const name = node.localName;
      if (name === "img") {
        add(
          node.getAttribute("data-src") ||
          node.getAttribute("data-original") ||
          node.getAttribute("data-lazy-src") ||
          node.currentSrc ||
          node.getAttribute("src")
        );
      } else if (name === "source") {
        add(node.getAttribute("data-src") || node.currentSrc || node.getAttribute("src"));
        addSrcset(node.getAttribute("data-srcset"));
        addSrcset(node.getAttribute("srcset"));
      } else if (name === "image") {
        add(node.getAttribute("href") || node.getAttribute("xlink:href"));
      } else if (name === "video") {
        add(node.getAttribute("poster"));
      }
      addBackgrounds(node.style && node.style.backgroundImage);
      addBackgrounds(window.getComputedStyle(node).backgroundImage);
    }
  };
  const wait = () => new Promise((resolve) => window.setTimeout(resolve, pauseMs));
  let stable = 0;
  let previousHeight = -1;
  let previousCount = -1;
  for (let step = 0; step < maxSteps; step += 1) {
    collect();
    const viewport = Math.max(window.innerHeight || 0, 1);
    const articleBottom = root.getBoundingClientRect().bottom + window.scrollY;
    const maximum = Math.max(0, document.documentElement.scrollHeight - viewport);
    const target = Math.min(maximum, Math.max(0, articleBottom - viewport));
    const atBottom = window.scrollY >= target - 2;
    const height = root.scrollHeight;
    if (atBottom && height === previousHeight && images.length === previousCount) stable += 1;
    else stable = 0;
    if (stable >= stableRounds || images.length >= maxImages) break;
    previousHeight = height;
    previousCount = images.length;
    window.scrollTo(0, Math.min(target, window.scrollY + Math.max(1, viewport * 0.8)));
    await wait();
  }
  collect();
  return images;
}
"""


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
            try:
                page.set_default_timeout(timeout_ms)
                page.goto(
                    normalized_url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                html = cast(str, page.content())
            except PlaywrightError as error:
                raise WxcliError(
                    ErrorCode.CHROME_ERROR,
                    "Chrome could not read the requested page.",
                ) from error

            kind = self._provider.classifier.classify(html)
            if kind is PageKind.VERIFICATION:
                raise VerificationRequiredError(
                    verification_stage="browser",
                    required_action="run_browser_login",
                )
            if kind is PageKind.NOT_FOUND:
                raise NotFoundError("The public article was not found.")
            if kind is PageKind.ERROR:
                raise WxcliError(
                    ErrorCode.PARSING_ERROR,
                    "The WeChat page is not a readable article.",
                )

            observed_images: list[str] = []
            remaining_ms = max(0, int((self._deadline - time.monotonic()) * 1000))
            scan_ms = min(MEDIA_SCAN_MAX_MS, max(0, remaining_ms - 1_000))
            if scan_ms >= MEDIA_SCAN_PAUSE_MS:
                max_steps = min(MEDIA_SCAN_MAX_STEPS, scan_ms // MEDIA_SCAN_PAUSE_MS)
                try:
                    result = page.evaluate(
                        _ARTICLE_MEDIA_SCAN_SCRIPT,
                        {
                            "maxSteps": max_steps,
                            "pauseMs": MEDIA_SCAN_PAUSE_MS,
                            "stableRounds": MEDIA_SCAN_STABLE_ROUNDS,
                            "maxElements": MEDIA_SCAN_MAX_ELEMENTS,
                            "maxImages": MEDIA_SCAN_MAX_IMAGES,
                        },
                    )
                    if isinstance(result, list):
                        observed_images = [
                            value for value in result if isinstance(value, str)
                        ]
                    html = cast(str, page.content())
                except PlaywrightError:
                    observed_images = []

            document = PublicArticleParser.parse(
                html,
                normalized_url,
                Provider.CHROME,
                observed_images=observed_images,
            )
            if self._provider.cache and not no_cache:
                self._provider.cache.put(normalized_url, document.article)
            self._provider.browser_profile.record_successful_read()
            return document
        finally:
            close = getattr(page, "close", None)
            if callable(close):
                close()


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
