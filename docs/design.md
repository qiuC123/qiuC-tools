# Approved wxcli Windows V1 Design

## Purpose and scope

wxcli is a Windows x64 command-line tool for read-only access to WeChat Official Account content. Its command groups are `article`, `account`, `auth`, `browser`, `cache`, and `doctor`; `--version` reports its version. The tool never publishes, deletes, edits, bypasses verification, or exports cookies.

## Runtime and distribution

The supported development runtime is Python 3.12 on Windows. The implementation uses Typer, Pydantic, HTTPX, BeautifulSoup with lxml, markdownify, Playwright using visible installed Chrome, keyring, pytest, and PyInstaller. Releases use a Windows x64 PyInstaller `onedir` folder packed as ZIP; a single EXE is not required.

## Data and source boundary

`Article` is the content model. `PublishedMessage(article_id)` and `DraftMessage(media_id)` each contain `articles[]`, preserving multi-article ordering. Providers are `http`, `chrome`, `official`, and `local`; each is read-only and selected for its corresponding source boundary.

## Command contract

With `--json`, standard output contains exactly one UTF-8 JSON document. Logs, prompts, and progress messages use standard error. Exit codes are: 0 success, 1 general, 2 input, 3 validation, 4 not found, 5 network, 6 authentication or permission, 7 Chrome, 8 parsing, and 9 local configuration. Non-interactive execution never waits for a prompt. A verification requirement returns `VERIFICATION_REQUIRED`; only explicit `--browser` may open Chrome.

## URL, cache, and secrets

Only `https://mp.weixin.qq.com/s/<token>` and `https://mp.weixin.qq.com/s?__biz=...&mid=...` public URLs are accepted. Successful public results may be cached for one hour using a normalized URL shared by HTTP and Chrome; failures are never cached. AppID is ordinary configuration, while AppSecret and access tokens are stored in keyring. No secret, token, or cookie may be placed in command arguments, logs, JSON, Git, or cache files.
