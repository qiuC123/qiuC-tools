# Approved wxcli Windows V1 Design

## Purpose and scope

wxcli is a Windows x64 command-line tool for reading WeChat Official Account content and, under the approved V2 extension below, explicitly creating one new unpublished draft from Word. Its command groups are `article`, `account`, `auth`, `browser`, `cache`, and `doctor`; `--version` reports its version. The tool never publishes, mass-sends, deletes, edits existing content, bypasses verification, or exports cookies.

## Runtime and distribution

The supported development runtime is Python 3.12 on Windows. The implementation uses Typer, Pydantic, HTTPX, BeautifulSoup with lxml, markdownify, Playwright using visible installed Chrome, keyring, pytest, and PyInstaller. Releases use a Windows x64 PyInstaller `onedir` folder packed as ZIP; a single EXE is not required.

## Data and source boundary

`Article` is the content model. `PublishedMessage(article_id)` and `DraftMessage(media_id)` each contain `articles[]`, preserving multi-article ordering. Providers are `http`, `chrome`, `official`, and `local`; each is read-only and selected for its corresponding source boundary.

## Command contract

With `--json`, standard output contains exactly one UTF-8 JSON document. Logs, prompts, and progress messages use standard error. Exit codes are: 0 success, 1 general, 2 input, 3 validation, 4 not found, 5 network, 6 authentication or permission, 7 Chrome, 8 parsing, and 9 local configuration. Non-interactive execution never waits for a prompt. A verification requirement returns `VERIFICATION_REQUIRED`; only explicit `--browser` may open Chrome.

## URL, cache, and secrets

Only `https://mp.weixin.qq.com/s/<token>` and `https://mp.weixin.qq.com/s?__biz=...&mid=...` public URLs are accepted. Successful public results may be cached for one hour using a normalized URL shared by HTTP and Chrome; failures are never cached. AppID is ordinary configuration, while AppSecret and access tokens are stored in keyring. No secret, token, or cookie may be placed in command arguments, logs, JSON, Git, or cache files.

## Approved V2 draft-import extension

wxcli may map a local Word `.docx` and a separate cover image into WeChat-safe HTML, preserve paragraph and image order, compress copies to documented WeChat limits, and generate a local preview without network access. A separate explicit `--confirm` action may upload those prepared images and create one new unpublished draft. Providers remain read-only, and wxcli still does not publish, update existing drafts, delete, comment, mass-send, bypass verification, or expose credentials.
