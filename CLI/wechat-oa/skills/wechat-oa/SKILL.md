---
name: wechat-oa
description: >
  Use the Windows-only wechat-oa command to discover and retrieve WeChat Official
  Account articles, produce article evidence, read local HTML/Markdown, drafts,
  and published messages, or to preview
  explicitly create one unpublished draft from a Word document, or safely
  back up, diff, and update an existing draft through a frozen plan. MUST USE
  for WeChat article discovery, supported mp.weixin.qq.com article URLs, wechat-oa
  commands, WeChat Official Account article extraction, read-only official-account content, or Word-to-
  WeChat draft import. Never publishes, mass-sends, deletes, bypasses the
  backup/diff/confirm update flow, bypasses verification, or exposes credentials.
---

# WeChat OA

Use `wechat-oa` as the dedicated backend for WeChat Official Account content. Prefer
it over generic web readers for supported `mp.weixin.qq.com` article URLs.

For agent-orchestrated keyword discovery, pass the schema-v1 Candidate Batch to
`wechat-oa --json discovery hydrate --input FILE|-`. For direct Brave discovery, use
`wechat-oa --json discovery search`. Search hits are candidates, not article text.
Candidate Batch JSON cannot grant browser use. The installed durable policy is
`never`; use one-shot `--browser-fallback` only after the user explicitly
authorizes browser mode, or change durable policy only when the user explicitly
requests it. `--no-browser` always prohibits Chrome for that invocation. Never describe external
discovery as a complete WeChat index.

## Core workflow

1. Run `wechat-oa --version`. If the command is missing, try the supported
   compatibility command `wxcli --version`. If both are missing, read the
   installation-source section in [references/operations.md](references/operations.md),
   report the pinned Windows release and request explicit authorization before
   downloading or extracting it. Never modify `PATH` or a persistent install
   directory without separate explicit authorization.
2. Put the global `--json` option before the subcommand.
3. Parse exactly one JSON value from stdout. Treat stderr as diagnostics only.
4. Validate success with both process exit code `0` and JSON `ok: true`.
5. For a public article, run:

   ```powershell
   wechat-oa --json article get "URL"
   ```

6. If the result is `VERIFICATION_REQUIRED`, explain that WeChat returned a
   verification page. When durable fallback is still `never`, do not open Chrome
   automatically. Only after the user
   explicitly authorizes browser mode, run:

   ```powershell
   wechat-oa --json article get "URL" --browser
   ```

   To preserve HTTP-first behavior for one invocation, prefer:

   ```powershell
   wechat-oa --json article get "URL" --browser-fallback
   ```

7. Return `content_markdown` as article text and `images[]` as image URLs. A
   terminal does not render Markdown images; do not claim images are missing
   merely because they appear as `![](https://...)` text.

## Task routing

- Public or local articles: read [references/article.md](references/article.md).
- Keyword discovery or Article Evidence: read [references/discovery.md](references/discovery.md).
- Drafts, published messages, or Official API access: read
  [references/account.md](references/account.md).
- Browser, cache, doctor, or local diagnostics: read
  [references/operations.md](references/operations.md).
- Before credentials, live API checks, destructive-looking commands, or error
  handling: read [references/safety.md](references/safety.md).

## Non-negotiable boundaries

- Keep every Provider read-only. Draft writes live in the separate writer and
  require either reviewed `account draft import-word --confirm`, or a frozen
  `draft diff` plan followed by an independently authorized `draft update
  PLAN_DIR --confirm`.
- Never publish, mass-send, delete, update without a frozen plan, like, comment,
  or export browser cookies.
- Never put AppSecret, access tokens, or cookies in prompts, command arguments,
  stdout, logs, JSON, files, or Git.
- Never solve or bypass a CAPTCHA. Ask the user to complete verification in the
  visible wechat-oa Chrome profile.
- Do not run `auth configure`, `discovery auth configure`, `auth test
  --allow-live-api`, `doctor --allow-live-api`, `browser clear`, `cache clear`,
  or `discovery cache clear` unless the user explicitly
  requests the corresponding action.
- Treat `browser status` as local facts only. It does not prove that the remote
  WeChat session is currently valid.
- Treat `browser policy set auto-fallback` as a durable local authorization
  change. Run it only after an explicit user request; `browser clear` does not
  reset that policy.
