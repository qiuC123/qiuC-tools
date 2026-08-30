# WeChat Article Discovery Design

**Status: Direct Discovery and Agent-orchestrated Candidate Ingestion are implemented since wxcli 0.4.0. Browser reliability is implemented in 0.5.0; media evidence for 0.6.0 remains an approved design, not implemented.**

This document records the wxcli 0.4.0 discovery foundation and its backward-compatible 0.5.0 browser reliability extension. The implementation preserves existing commands, JSON envelopes, exit codes, cache behavior, and read-only safety boundaries. Live Brave, WeChat, and Chrome smoke tests still require separate explicit authorization.

## Product boundary

wxcli discovers and reads public WeChat Articles, extracts source identity and evidence, and hands observed external links to callers. It does not promise a complete or real-time WeChat index, decide which company or recruitment batch an Article represents, search or read company websites or ATS systems, classify jobs, or merge WeChat evidence with other sources. Those decisions belong to callers such as `official-campus-radar`.

A search hit is only an Article Candidate. Search titles, snippets, account hints, and dates never become Article fields. Only a successful Hydration through an existing Content Provider produces Article Evidence; unsuccessful reads produce a Hydration Attempt.

## Discovery modes

Direct Discovery is the implemented standalone path:

```text
wxcli → Brave Discovery Provider → Article Candidate → Hydration → Article Evidence
```

Agent-Orchestrated Discovery is the approved primary integration for callers that treat Codex CLI as a runtime dependency:

```text
Codex Search Orchestrator → Agent Reach / Exa → Candidate Batch → wxcli → Article Evidence
```

The Search Orchestrator owns query expansion, multi-query strategy, and External Discovery Provider continuation. wxcli remains the trust boundary for WeChat URLs and source evidence. It never imports Agent Reach installation details, MCP configuration, or Exa credentials. See [ADR-0005](adr/0005-use-agent-orchestrated-discovery-as-primary-integration.md).

## CLI

Human-oriented discovery:

```powershell
wxcli --json discovery search "2027 校园招聘" `
  --company "Example Company" `
  --account "Example Recruiting" `
  --published-after 2026-09-01 `
  --published-before 2027-12-31 `
  --limit 50
```

Versioned machine input accepts either a file or standard input. It is mutually exclusive with the positional query and the corresponding search options:

```powershell
wxcli --json discovery search --input request.json
Get-Content request.json | wxcli --json discovery search --input -
```

Optional Hydration is explicit. Search without `--hydrate` never visits candidate WeChat pages. Browser fallback is separately and explicitly authorized, is valid only with `--hydrate`, runs serially, and remains subject to the batch deadline:

```powershell
wxcli --json discovery search "2027 校园招聘" --hydrate
wxcli --json discovery search "2027 校园招聘" --hydrate --browser
```

Single-article evidence uses a separate command so the existing `article get` contract remains unchanged:

```powershell
wxcli --json article evidence "https://mp.weixin.qq.com/s/TOKEN"
```

Discovery credentials and cache management are separate from Official Account credentials and the existing public-article cache:

```powershell
wxcli discovery auth configure --provider brave
wxcli --json discovery auth status --provider brave
wxcli --json discovery cache clear
```

Credential configuration is interactive and never accepts a secret in an argument, JSON input, or standard input. Status reports only whether the credential exists. Cache clearing requires an explicit command and does not broaden the meaning of the existing `wxcli cache clear` command.

### Agent-orchestrated Candidate Hydration

The machine-only entry point accepts one Candidate Batch from a file or standard input:

```powershell
wxcli --json discovery hydrate --input candidates.json
Get-Content candidates.json | wxcli --json discovery hydrate --input -
```

`discovery hydrate` does not call Brave, Exa, Agent Reach, or Codex. It validates the supplied batch, deduplicates strict WeChat Public URLs, records candidate history, selects bounded Hydration attempts, and produces wxcli-owned evidence. Direct `discovery search` remains available and continues to use Brave when a standalone search path is wanted.

Candidate Batch policy may be overridden locally. Browser permission is deliberately a CLI-only decision and cannot be delegated through input JSON:

```powershell
wxcli --json discovery hydrate --input candidates.json `
  --priority-hydrate 10 --max-hydrate 20

# Only after the user explicitly authorizes visible Chrome fallback:
wxcli --json discovery hydrate --input candidates.json --browser
```

## Candidate Batch contract

The Candidate Batch input schema is version 1:

```json
{
  "schema_version": "1",
  "discovery_request": {
    "query": "2027 校园招聘",
    "companies": ["Example Company"],
    "expected_accounts": [
      {
        "display_names": ["Example Recruiting"]
      }
    ]
  },
  "source": {
    "orchestrator": "codex",
    "providers": ["exa"]
  },
  "candidates": [
    {
      "url": "https://mp.weixin.qq.com/s/TOKEN",
      "title_hint": "Example Company 2027 campus hiring",
      "snippet": "Untrusted search-provider summary",
      "backend_date_hint": "2026-08-20",
      "search_provenance": {
        "provider": "exa",
        "rank": 1,
        "result_id": "safe-result-id"
      }
    }
  ],
  "hydration": {
    "priority_count": 10,
    "maximum_attempts": 20
  }
}
```

All objects reject unknown fields. Candidate URLs, strings, collection sizes, and the total document size are bounded before work begins: at most 100 candidates and 2 MiB of UTF-8 JSON per invocation. Provider names are lowercase extensible identifiers rather than a fixed Brave-or-Exa enum. Search provenance is untrusted metadata and never becomes Account Identity Evidence.

The input cannot supply `discovered_at`, `last_seen_at`, `published_at`, Article Evidence, Hydration Attempts, identity-verification status, evidence hashes, API keys, Cookies, authorization headers, private business tags, or caller-internal trust scores. wxcli stamps observation times, creates stable identities, and derives every evidence field itself. Exa credentials remain entirely outside wxcli.

The result is a separate `CandidateIngestionResult`; it does not change the Direct Discovery result:

```json
{
  "schema_version": "1",
  "discovery_mode": "agent_orchestrated",
  "orchestrator": "codex",
  "provenance_trust": "orchestrator_reported",
  "summary": {
    "received": 20,
    "accepted": 15,
    "duplicates_removed": 3,
    "invalid_removed": 2,
    "hydration_attempted": 12,
    "verified": 10,
    "partial": true
  },
  "candidates": []
}
```

Individual Hydration failures and invalid individual URLs remain per-candidate outcomes with overall success and `partial: true`; invalid URLs also appear in `rejections[]` with their input index and safe error category. An invalid batch, unreadable input, unavailable local evidence service, or failure that prevents the batch from being accepted uses the existing nonzero error-envelope contract. Candidate Ingestion records accepted identities in discovery history, but search pagination and query continuation remain the Search Orchestrator's responsibility; a Direct Discovery cursor is never reused as an Agent continuation token.

## Request contract

The request schema is version 1:

```json
{
  "schema_version": "1",
  "query": "2027 校园招聘",
  "companies": ["Example Company"],
  "expected_accounts": [
    {
      "biz_id": "opaque-public-account-id",
      "display_names": ["Example Recruiting"]
    }
  ],
  "published_after": "2026-09-01",
  "published_before": "2027-12-31",
  "limit": 50,
  "cursor": null,
  "checkpoint": null,
  "new_only": false,
  "hydrate": true,
  "priority_hydrate": 10,
  "max_hydrate": 20,
  "require_account_match": false,
  "require_published_date": false,
  "allow_browser": false
}
```

`query` is required. `companies`, account display names, and the publication window are search hints, not company ownership or publication proof. `biz_id` is the strongest caller-supplied account identity. Search text rejects control characters, each company/account-name hint is limited to 200 characters, and the combined outbound query is limited to 2,000 characters. Credentials, internal company IDs, trust scores, private business tags, and internal notes are forbidden in the request.

`limit` defaults to 50. `priority_hydrate` defaults to 10 and `max_hydrate` defaults to 20; both apply only when Hydration is enabled, and `priority_hydrate` cannot exceed `max_hydrate`. Strict URL validation and deduplication happen before either count is applied.

## Result contract

The existing one-document JSON envelope remains unchanged. Discovery data uses schema version 1 and contains request metadata, the actual Discovery Provider, an opaque `next_cursor`, a `checkpoint`, a summary, and ordered candidates:

```json
{
  "ok": true,
  "data": {
    "schema_version": "1",
    "search_provider": "brave",
    "next_cursor": null,
    "checkpoint": "opaque-checkpoint",
    "summary": {
      "received": 12,
      "accepted": 8,
      "duplicates_removed": 3,
      "hydration_attempted": 8,
      "verified": 6,
      "partial": true
    },
    "candidates": []
  }
}
```

Version 1 may gain optional fields but cannot remove fields or change their meaning. A breaking contract requires a new schema major version. Callers ignore unknown optional fields within a known major version and reject an unknown major version.

Each Article Candidate contains:

- `fetch_url`: the strict Public URL used for reading.
- `article_identity`: a stable deduplication identity separate from the fetchable URL.
- `title_hint`, `account_hint`, `snippet`, and `backend_date_hint`: unverified search metadata.
- `discovered_at` and `last_seen_at`: wxcli discovery times.
- `search_provenance`: provider name, rank, and sanitized provider result identifier.
- `match_reasons` and categorical `confidence` (`low`, `medium`, or `high`); numeric ranking scores are internal and unstable.
- `hydration_decision`: `priority`, `selected`, or `candidate_only`, plus explicit decision reasons.
- `verification_status`: `not_attempted`, `verified`, `verification_required`, `not_found`, `parse_failed`, or `network_failed`.
- Exactly one of optional `evidence` or `hydration_attempt`; neither exists before Hydration.

An Article Evidence contains the existing Article, Account Identity Evidence, observed external links, image metadata, `last_verified_at`, `content_sha256`, and `evidence_sha256`. `published_at` comes only from the WeChat source and remains null when the source provides no reliable publication time. `backend_date_hint` never substitutes for it. `content_sha256` excludes discovery rank and observation timestamps; `evidence_sha256` additionally reflects extractor versions and derived evidence.

A Hydration Attempt contains the attempted Content Provider, attempt time, verification status, and the existing safe structured error category. It never copies search snippets into Article content.

An empty search is a successful result. If search succeeds but individual Hydrations fail, the command exits successfully with `summary.partial: true` and per-candidate failure states. A total Discovery Provider, authentication, or request failure uses the existing nonzero exit-code and error-envelope contract.

## Discovery, ranking, and Hydration

The implemented Direct Discovery Provider is Brave Web Search, queried with a mandatory `site:mp.weixin.qq.com/s` restriction. It is an optional deterministic path rather than a required dependency for Agent-Orchestrated Discovery. In the approved agent-first integration, Codex and Agent Reach may call Exa outside wxcli and submit only a Candidate Batch. Both paths record real search provenance and neither is presented as official WeChat search.

Only direct HTTPS `mp.weixin.qq.com/s` results that satisfy the existing strict Public URL contract are accepted. wxcli does not follow arbitrary search wrappers or untrusted redirects. The fetchable URL is preserved, while query-form results use `__biz`, `mid`, and `idx` where available to create a stable identity that ignores tracking parameters.

Candidates are deduplicated and ranked from sanitized search metadata before Hydration. When Hydration is enabled, up to the first 10 ranked candidates are priority attempts. Additional candidates may be selected for expected-account, company, keyword, recency, uncertainty, or source-diversity reasons, but no more than 20 candidates are attempted per invocation. These are generic relevance decisions, never recruitment-batch classifications.

HTTP Content Providers run before any browser fallback. HTTP Hydration uses at most three concurrent requests, a 30-second per-article timeout, one retry for a network error, and a five-minute batch deadline. Verification pages, not-found pages, and parse failures are not retried. In 0.4.0, Chrome opens only when the local CLI invocation includes `--browser` or a trusted Direct Discovery Request contains `allow_browser: true`; Candidate Batch JSON cannot authorize it. Chrome runs serially in the visible dedicated profile, never bypasses verification, and never exports cookies.

## Implemented 0.5.0 browser contract

Version 0.5.0 is limited to browser reliability and does not include image download, QR decoding, OCR, or Evidence Bundles. It introduces a user-level Browser Fallback Policy with these fixed semantics:

- The installed default is `never`. A user may explicitly enable durable automatic fallback once and may revoke it later.
- A trusted Direct Discovery Request may grant fallback for its own invocation through `allow_browser: true`, but it cannot modify the durable policy. Candidate Batches, Search Orchestrators, Article content, and calling-project data cannot grant browser use. A local per-invocation prohibition always overrides every grant.
- Automatic fallback applies only to strict Public URLs after the HTTP Content Provider returns Verification Required. It cannot visit External Link Handoffs, company sites, ATS pages, Official Account administration pages, or QR payloads.
- Existing `--browser` behavior remains backward compatible. The separate `--browser-fallback` control and trusted Direct Discovery Request authorization support callers that do not want durable authorization.
- A batch gathers HTTP verification outcomes first, then creates at most one visible Browser Run and serially reads the eligible candidates through the one retained Browser Session. The Browser Run ends with the batch while the independent profile persists.
- If the retained Browser Session still reaches a scan, slider, confirmation, or other human challenge, unattended execution preserves `verification_status: verification_required`, adds `verification_stage: browser` and `required_action: run_browser_login`, and immediately ends the Browser Run. It does not wait indefinitely, solve the challenge, or export Cookies. Completed Evidence is preserved and unvisited browser-eligible candidates require session refresh.
- The whole command has a ten-minute hard deadline: HTTP may use at most five minutes and the single Browser Run may use the remaining time, never more than five minutes. Chrome crashes are reported without automatic restart in 0.5.0.
- Missing durable policy means `never`; corrupt or unsupported policy safely degrades to `never` with a visible configuration diagnostic. Explicit per-invocation browser controls continue to work without trusting the damaged file.
- `browser login` success means only that its visible window completed normally. Only a successful real Chrome Article read records `last_successful_read_at`.
- One user-level wxcli profile serves all strict public WeChat Articles. It is not split by company or Official Account; Official Account API credentials remain separate in keyring.

See [ADR-0006](adr/0006-persist-browser-fallback-authorization-outside-candidate-input.md).
The complete implemented command, state, error, migration, and acceptance contract is in [Browser Fallback Design](browser-fallback.md).

## Identity and external links

Account Identity Evidence records observed display names and stable identifiers, then compares them with caller-supplied expected accounts. Identity status is one of `observed`, `allowlist_matched`, `name_only_matched`, `mismatch`, `unknown`, or `repost_suspected`. Exact `biz_id` agreement is stronger than a display-name match; a name alone can never produce the highest confidence or a claim that the account belongs to a company.

Page metadata is read only from the strict source URL, account-profile elements outside the article body, and trusted page-level script elements outside the article body. Text or links embedded in the article body cannot supply `biz_id` or `published_at`; this prevents ordinary article content from impersonating page metadata.

Reposts remain candidates by default with reduced confidence and explicit mismatch or repost evidence. `require_account_match` may exclude them after verification. `require_published_date` may exclude evidence whose WeChat `published_at` is null; without that option its date match is `unknown`.

External URLs observed in article markup are recorded with their raw value, normalized value when safe, source location, and type hint. wxcli does not visit them, resolve unknown short links, operate application forms, or decide that a link is an official recruitment channel. The caller owns all company-site and ATS retrieval.

## State, credentials, and privacy

- Search responses are cached for 15 minutes. Candidate history is retained for 180 days for `new_only`, `discovered_at`, and checkpoint behavior. The existing successful Article cache remains one hour and is unchanged.
- `next_cursor` continues the current result page; `checkpoint` repeats the same normalized query while identifying candidates not previously seen for it. Callers still enforce permanent idempotency by `article_identity` or normalized source URL.
- Brave credentials live in Windows Credential Manager under a discovery-specific keyring identity. Secrets never enter command arguments, JSON, stdin, stdout, stderr, logs, caches, fixtures, Git, or evidence bundles.
- A search request necessarily sends its minimum query terms to the configured Discovery Provider. wxcli sends no caller-internal IDs, complete business databases, trust scores, private tags, credentials, or internal notes.
- wxcli adds no remote telemetry. JSON results use stdout, sanitized diagnostics use stderr, and metrics remain local except for data required to execute the configured search.
- Search requests time out after 30 seconds. Network failures retry once. HTTP 429 honors a bounded `Retry-After`; authentication and permission failures do not retry or fall back to another provider.

## Releases

### 0.4.0 (implemented)

Article discovery, strict candidate handling, ranking, bounded Hydration, Account Identity Evidence, content fingerprints, and external-link extraction. It does not download images for derived analysis, decode QR codes, or perform OCR.

### 0.5.0 (implemented)

Durable, user-authorized HTTP-to-Chrome fallback; a local per-invocation prohibition; one visible Browser Run per Hydration batch; retained Browser Session reuse; and a bounded User Action Required outcome for challenges that still need a person. Existing 0.4.0 browser behavior remains compatible, Candidate input cannot authorize Chrome, and no Cookie import or export is added.

### 0.6.0

Standard QR decoding and local-only OCR through replaceable providers. QR payloads are recorded but never opened; possible WeChat Mini Program codes may be identified but are not promised to decode. OCR text remains derived evidence with image origin, confidence, and engine version and never merges into the Article body.

Image processing is limited to HTTPS images referenced by the Article, blocks local/private network destinations and unsafe redirects, and defaults to at most 50 images, 10 MB per image, 100 MB total, and 20 seconds per image. Temporary files are deleted after extraction unless the user explicitly requests an evidence bundle. An explicit bundle contains versioned evidence JSON, Markdown, link and image manifests, hashes, and requested images; it omits raw search API responses and full dynamic WeChat HTML.

Article Evidence schema v1 and its hashes remain unchanged. Media Analysis is explicit and produces separately versioned Media Evidence linked by `content_sha256`; Candidate Batch data cannot enable it or choose a filesystem path. Batch limits, local-only processing, Media Cache, partial-result, and atomic Evidence Bundle rules are frozen in [Media Evidence Design](media-evidence.md) and [ADR-0007](adr/0007-keep-media-analysis-separate-from-core-article-evidence.md).

Non-media request and result documents remain schema v1. Media-enabled Direct Discovery Requests and outer results use schema v2, while embedded Article Evidence remains schema v1 and Media Evidence begins with its own schema v1. Candidate Batch input remains schema v1 and can be media-enabled only through a local CLI control.

## Test and acceptance contract

Ordinary unit, integration, command-boundary, and Windows packaging tests never call a real search service or WeChat. They use sanitized search fixtures, fake Discovery and Content Providers, mocked article results, temporary state, and fake credential storage. Coverage includes invalid and duplicate URLs, provider failure and rate limiting, bad or mismatched cursors, empty results, verification pages, missing articles, missing publication dates, reposts, account mismatch, partial Hydration, JSON pipelines, stdout/stderr separation, exit codes, and secret redaction.

Live smoke tests require separate explicit authorization for search, real WeChat HTTP reads, and visible Chrome. They are never mandatory CI jobs. A frozen live benchmark reports its companies, observation window, known relevant Articles, missing publication dates, verification pages, and categorized failures.

Candidate Ingestion tests use sanitized batches and fake Content Providers; ordinary tests never invoke Codex, Agent Reach, Exa, WeChat, or Chrome. They cover oversized documents, unknown fields, credentials in input, malformed and duplicate URLs, caller-supplied evidence claims, prompt-injection text treated only as inert strings, bounded Hydration, and safe partial results. A separate manually authorized agent-first smoke exercises Codex CLI → Agent Reach/Exa → Candidate Batch → wxcli, while visible Chrome remains a distinct authorization.

The direct Brave manual entry point is `scripts/live-discovery-smoke.ps1`. It refuses to run without `-AllowLiveSearch`; source Hydration additionally requires `-AllowLiveWeChat`, and visible Chrome additionally requires `-AllowBrowser`, which maps to one-shot `--browser-fallback`.

The agent-first manual entry point is `scripts/live-agent-discovery-smoke.ps1`. It independently requires `-AllowLiveAgentSearch` before invoking ephemeral, read-only `codex exec`, and `-AllowLiveWeChat` before handing its schema-constrained Candidate Batch to wxcli. Visible Chrome additionally requires `-AllowBrowser`, which maps to one-shot `--browser-fallback`. `-CodexPath` and `-WxcliPath` select the exact executables; the 0.5.0 script refuses older wxcli versions. Ordinary tests and the packaged offline smoke never invoke either live script.

Initial acceptance targets are:

- At least 80% recall against the controlled known-Article benchmark; this is not whole-WeChat coverage.
- No more than 10% false positives after strict identity filtering.
- No more than 5% complete Discovery Provider request failures.
- With daily execution, discovery delay `P50 <= 24 hours` and `P90 <= 72 hours` where a reliable `published_at` exists.
- Discovery implementation line coverage at least 95%, branch coverage at least 90%, and no reduction in repository-wide coverage.
- For 0.5.0, an HTTP-success batch starts zero Browser Runs; a verification-only batch creates at most one Browser Run; disabled, invalid-policy, or locally prohibited fallback never starts Chrome; a retained session is reused across invocations; a remaining human challenge terminates the Browser Run without changing the existing verification-status enum; Chrome crashes do not restart automatically; total execution stays within ten minutes; and no wxcli Chrome process remains after the batch.
- For 0.6.0, at least 95% standard-QR decoding success on the representative benchmark with zero incorrect payloads, and Chinese OCR character error rate no greater than 10% on readable recruitment-poster fixtures.

Verification-required, not-found, network, and parse outcomes are reported separately rather than collapsed into one failure rate.
