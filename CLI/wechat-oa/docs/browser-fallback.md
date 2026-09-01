# wxcli Browser Fallback Design

**Status: Implemented in wxcli 0.5.0; packaged live acceptance remains explicitly authorized.**

This document records the implemented 0.5.0 Browser Fallback contract. The installed 0.4.0 release, if still selected through rollback, retains its older explicit `--browser` behavior.

## Goal and scope

Version 0.5.0 makes repeated public-WeChat reading require less supervision without turning wxcli into a general browser controller. A user may authorize HTTP-to-Chrome fallback once; later requests still try HTTP first and create a visible Browser Run only for strict Public URLs that return Verification Required.

Version 0.5.0 does not download Article images, decode QR payloads, perform OCR, build media evidence bundles, visit Article external links, search company websites or ATS systems, or operate Official Account administration pages. Those media-evidence capabilities move to 0.6.0.

## Domain model

- **Browser Session** is browser-owned WeChat session state retained in the single independent wxcli profile across Browser Runs. wxcli does not inspect, import, export, or display its underlying Cookies.
- **Browser Run** is one bounded visible-Chrome lifetime for one request or Hydration batch. It reuses the Browser Session and ends after the request.
- **Browser Fallback Policy** is a user-owned durable local choice that may allow a Browser Run only after HTTP returns Verification Required. A trusted Direct Discovery Request may grant fallback for its own invocation, but Candidate Batch input cannot grant it and no request changes the durable policy.
- **User Action Required** means a Browser Run still reached a scan, slider, confirmation, or equivalent human challenge and automatic reading cannot continue until the user explicitly refreshes the Browser Session.

## Release split

- **0.5.0**: durable Browser Fallback Policy, one-shot fallback, one Browser Run per batch, Browser Session reuse, bounded challenge handling, truthful browser status, and packaged/live verification.
- **0.6.0**: bounded image download, standard QR decoding, local OCR, derived media evidence, and optional Evidence Bundles.

## CLI contract

The following commands are implemented in 0.5.0 and do not exist after rollback to 0.4.0:

```powershell
wxcli browser policy set auto-fallback
wxcli browser policy set never
wxcli --json browser policy status
```

`set` is already an explicit local configuration action and does not add another interactive confirmation. An Agent may run it only after the user explicitly requests that policy change. `status` is read-only and returns no Cookie, account, profile-path, or browser-internal data.

Article-reading and Hydration commands gain two one-shot controls while preserving the existing `--browser` switch:

```text
--browser           Preserve 0.4.0 behavior and directly use visible Chrome.
--browser-fallback  For this invocation, try HTTP and permit Chrome only after Verification Required.
--no-browser        For this invocation, prohibit Chrome even when durable auto-fallback is enabled.
```

Supplying conflicting explicit controls is a CLI input error. When no browser-control option is supplied, a trusted Direct Discovery Request may grant fallback for that invocation; otherwise the durable Browser Fallback Policy applies. `--no-browser` overrides request authorization and durable `auto-fallback`. Candidate Batch input cannot authorize Chrome.

Direct Discovery Request JSON remains a trusted control interface and may contain the existing field:

```json
{
  "schema_version": "1",
  "query": "校园招聘",
  "hydrate": true,
  "allow_browser": true
}
```

This grant ends with that invocation. It does not write `browser-policy.json`. When `discovery search --input FILE|-` is used, `--no-browser` is the one browser option allowed alongside `--input` so an operator can locally prohibit Chrome; the other manual search controls remain mutually exclusive with request JSON.

## Policy state

The policy is non-secret, user-level state stored at:

```text
%LOCALAPPDATA%\wxcli\browser-policy.json
```

It uses a versioned schema and atomic replacement. The installed default is `never`. Installation, upgrade, program rollback, and Skill synchronization preserve this file.

A missing policy file means effective `never`. A corrupt file or unsupported schema also safely resolves to `never`: HTTP reading continues, Chrome does not start from durable policy, and command output or sanitized diagnostics reports `browser_policy_invalid`. `browser policy status` treats invalid persistent state as a nonzero configuration error. Explicit `--browser`, `--browser-fallback`, and `--no-browser` remain usable because they do not depend on trusting or rewriting the damaged file.

`wxcli browser clear` continues to delete only the independent Browser Session and browser status. It does not change the Browser Fallback Policy. A later automatic fallback with no usable session may return User Action Required. Disabling automatic fallback always requires the separate explicit `browser policy set never` action.

## Effective-mode resolution

The effective behavior is resolved locally before reading any candidate:

1. Conflicting explicit controls fail without opening Chrome.
2. `--no-browser` prohibits every Browser Run for the invocation.
3. Existing `--browser` directly selects Chrome for backward compatibility.
4. `--browser-fallback` permits one-shot HTTP-to-Chrome fallback.
5. A trusted Direct Discovery Request with `allow_browser: true` permits one-shot fallback for that request.
6. With no explicit or request grant, the durable policy selects `auto-fallback` or `never`.

Candidate Batch machine input may request Hydration but cannot request, infer, or persist browser authorization. Direct Discovery Request JSON is a separate caller-owned control plane: it may authorize only its own invocation and cannot change durable policy.

## Single-Article flow

With effective `never`:

```text
cache → HTTP → Article Evidence or existing structured failure
```

With effective `auto-fallback` or one-shot fallback:

```text
cache
→ HTTP
→ success: return without creating a Browser Run
→ Verification Required: create one Browser Run
→ success: return Chrome Article Evidence
→ human challenge: return User Action Required
```

Existing `--browser` continues to select Chrome directly so 0.4.0 scripts that intentionally require `provider: chrome` are not silently changed.

After a visible Chrome page has been classified as an Article, the provider performs a bounded,
read-only scan of `#js_content` before taking the final snapshot. It scrolls only the current page,
stops after at most 7 seconds, 80 steps, 2,000 observed elements, or 200 image URLs, and never clicks
or navigates an Article link. The scan observes lazy/runtime image sources, `picture`/`source`, SVG
`image`, video posters, and CSS background images. Only HTTPS runtime observations without embedded
credentials are accepted. A failed optional scan falls back to the already readable static Article
instead of discarding its core evidence.

Static HTTP and Chrome snapshots also extract those supported markup forms when they are already
present in `#js_content`. Runtime observations are merged in source order and deduplicated before
they become `Article.images`; this discovery step does not download or analyze an image.

## Batch flow

Hydration first runs its bounded parallel HTTP phase for every selected candidate. It then collects only candidates whose HTTP result is Verification Required.

When eligible candidates exist and fallback is authorized, wxcli creates at most one persistent Chrome context for the entire batch. Each candidate uses a fresh temporary tab inside that context; the tab closes after its Article is read or categorized. The context closes when the batch finishes, while the independent Browser Session remains on disk.

If any browser attempt reaches a human challenge, wxcli immediately ends the Browser Run because the shared Browser Session is not currently usable. It preserves earlier successes, records the challenged candidate as User Action Required, and marks browser-eligible candidates not yet visited as requiring a refreshed session. Ordinary not-found and parse failures do not stop the Browser Run.

The Browser Run remains serial and may visit only the already validated strict Public URLs. It does not reuse or control a user's normal Chrome process or tabs.

## Time and concurrency bounds

- Each Chrome Article read has a 30-second limit.
- One Discovery or Candidate Hydration command has a hard ten-minute total deadline.
- Its HTTP phase uses at most the first five minutes.
- The complete Browser Run has a five-minute limit.
- The Browser Run may use only the time remaining under the ten-minute total deadline, even when that is less than five minutes.
- Automatic fallback never waits for a person inside that five-minute budget.
- `browser login` remains the separate explicit workflow and allows up to five minutes for manual session initialization or refresh.
- `browser verify ARTICLE_URL` is the explicit Article-specific workflow. It keeps only that strict Public URL visible for up to five minutes, observes page classification without clicking, and returns Article Evidence when the user makes the Article readable.
- Successful Evidence remains available when later candidates time out.
- Timed-out and unattempted candidates receive safe per-candidate outcomes; batch output remains partial rather than discarding completed Evidence.

The existing single-profile lock remains authoritative. A Browser Run waits at most five seconds for the lock. If it remains occupied, a single-Article command returns `BROWSER_BUSY` with Chrome exit code 7; a batch records `BROWSER_BUSY` in eligible Hydration Attempts, preserves existing HTTP/Evidence results, and reports partial success. wxcli never terminates the process holding the lock.

If Chrome or its persistent context crashes, wxcli does not automatically restart it in 0.5.0. The current and remaining browser-eligible candidates receive safe browser-failure outcomes, completed Evidence is preserved, and a batch reports partial success. This prevents crash loops and repeated visible browser launches.

## Human-challenge contract

HTTP that requires an authorized browser remains `verification_status: verification_required`. After an authorized Browser Run also reaches a human challenge, the candidate becomes:

```json
{
  "verification_status": "verification_required",
  "verification_stage": "browser",
  "required_action": "run_browser_login"
}
```

A failed single-Article command preserves the existing `VERIFICATION_REQUIRED` error family and exit code 6, adding optional safe fields that identify the browser stage and required action. Schema-v1 consumers continue to see the existing verification status; no new required enum value is introduced. Batch commands record the outcome in a Hydration Attempt, stop the unusable Browser Run, and preserve all HTTP and completed Evidence results.

wxcli never solves a CAPTCHA, clicks through a human confirmation, waits indefinitely, or changes `browser policy` as a side effect. Generic session initialization remains available through `wxcli browser login`; when the exact challenged Article URL is known, a separately authorized `wxcli --json browser verify "URL"` keeps that Article visible and returns its evidence after manual verification. Calling agents must not use computer-use, window activation, or generic browser automation to take over this window.

## Result transparency

Discovery and Candidate Ingestion results may add this optional schema-v1 field:

```json
{
  "browser_fallback": {
    "effective_mode": "auto-fallback",
    "policy_source": "request_json",
    "eligible": 15,
    "attempted": 15,
    "verified": 12,
    "user_action_required": 3,
    "started_at": "2026-08-30T00:00:00Z",
    "finished_at": "2026-08-30T00:03:00Z"
  }
}
```

The summary reports observable wxcli behavior only. It never emits Cookies, a profile path, a login identity, tab contents, browser target IDs, or raw verification-page data. Existing schema-v1 fields keep their meaning and callers may ignore the optional summary.

## Truthful browser status and migration

In 0.4.0, `browser login` may write `last_verified_at` merely because its visible window completed; that value does not prove a WeChat Article was successfully read. Version 0.5.0 corrects the model:

- `browser login` no longer claims remote verification success.
- Exit code 0 from `browser login` means only that the explicit visible-window lifecycle completed normally; it does not prove that any fixed remote Article or current WeChat challenge was successfully read.
- Only a successful real Chrome Article read writes `last_successful_read_at`.
- `browser status` continues to state that local profile existence is not proof of a currently valid remote session.
- Existing `last_verified_at` data migrates as legacy observation data and is never reinterpreted as a successful read.
- The local browser-state schema is versioned and migrated atomically.

## Safety invariants

- Only strict `https://mp.weixin.qq.com/s/...` Public URLs are eligible.
- A trusted Direct Discovery Request may grant browser use only for its own invocation. A Candidate Batch, Search Orchestrator result, calling-project data, page script, redirect, or Article body cannot grant browser authorization, and no request may modify durable policy.
- Automatic fallback never visits External Link Handoffs, company sites, ATS pages, QR payloads, Official Account administration pages, or arbitrary WeChat pages.
- The Browser Session belongs to the independent wxcli profile; no normal-browser Cookie is imported or exported.
- Chrome remains visible and no verification is bypassed.
- Every Browser Run is bounded, locked, attributable in safe JSON, and closed at completion.

## Test and acceptance contract

Ordinary tests remain offline and use fake HTTP/Chrome providers, temporary policy/state files, a fake profile lock, and a fake clock. They cover policy schemas and atomic writes, option conflicts and precedence, migration, missing and corrupt state with safe `never` fallback, HTTP success, Verification Required, User Action Required without a new status enum, direct `--browser` compatibility, one-shot fallback, local prohibition, lock contention, ten-minute total and phase budgets, first-candidate human challenge, Chrome crash without restart, batch partial success, output redaction, and one-context/fresh-tab behavior.

Packaged and explicitly authorized live acceptance must prove:

- An HTTP-success batch creates zero Browser Runs.
- Durable `never` and explicit `--no-browser` create zero Browser Runs.
- A verification-only batch creates at most one Browser Run and one persistent context.
- Browser Session state is reusable across invocations without Cookie import or export.
- A human challenge immediately ends the Browser Run as User Action Required without indefinite waiting; completed Evidence remains available and unvisited eligible candidates report that session refresh is required.
- A Chrome crash starts no replacement Browser Run in 0.5.0.
- Invalid durable policy state never starts Chrome implicitly, while explicit invocation controls continue to work.
- HTTP and browser work together never exceed the ten-minute command deadline.
- Provider and browser-policy provenance are visible in output.
- No wxcli Chrome process remains after the request.
- Existing 0.4.0 `--browser` commands and JSON consumers remain compatible.
