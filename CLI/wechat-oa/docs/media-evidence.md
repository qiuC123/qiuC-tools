# Media Evidence Design

**Status: Implementation in progress for WeChat OA 0.6.0. The versioned Media Evidence data models, standalone safe image downloader, original-byte Media Cache, and article-level acquisition orchestration are implemented; analysis orchestration, derived-result caching, QR, OCR, CLI integration, and Evidence Bundles are not yet implemented.**

This document freezes the optional image-download, QR-decoding, local-OCR, Media Evidence, and Evidence Bundle boundaries for wxcli 0.6.0. These commands are not part of the 0.5.0 implementation. Browser reliability remains the separate 0.5.0 scope described in [Browser Fallback Design](browser-fallback.md).

## Implemented foundations

The model contract lives in `src/wxcli/media/models.py`. It currently provides:

- Media Evidence schema v1 and the explicit media-enabled outer result schema v2;
- `MediaItemEvidence` with the stable `analyzed`, `skipped`, and `failed` statuses;
- separate QR payload and local OCR outcomes linked to the exact image-byte SHA-256;
- deterministic summary and `partial` values derived from item outcomes;
- hard resource-limit models that callers may lower but cannot raise above approved caps;
- a stable Media Evidence SHA-256 that includes result-affecting configuration and derived
  observations while excluding run timestamps and cache-hit status; and
- validation that Media Evidence links to the unchanged Article Evidence schema v1 through
  `content_sha256`.

The standalone downloader lives in `src/wxcli/media/downloader.py`. It currently provides:

- a dedicated credential-free HTTP client that ignores environment proxies and manually validates
  every redirect;
- an initial exact allowlist containing only the tested article-image CDN host
  `mmbiz.qpic.cn`;
- HTTPS, authority, DNS, and public IPv4/IPv6 checks before every request and retry;
- one bounded retry for DNS, transport, and timeout failures, plus one bounded HTTP 429 retry;
- enforcement of actual streamed bytes as well as declared `Content-Length`; and
- safe JPEG, PNG, static WebP, and first-frame GIF decoding with media-type, integrity, dimension,
  and pixel-limit checks.

The original-byte cache lives in `src/wxcli/media/cache.py`. It stores public image bytes by their
verified SHA-256 and keeps separate hashed URL references, so identical content is stored once.
Writes use sibling temporary files and atomic replacement. Reads recompute the byte hash, refresh
LRU access only after successful verification, and remove corrupt or expired entries individually.
Cleanup expires references first, removes orphan blobs, and then deterministically evicts the least
recently used references until the complete cache is within its configurable hard cap of 1 GB.

Article-level acquisition lives in `src/wxcli/media/orchestration.py`. It consumes only the existing
`Article.images` sequence, keeps source order, applies the configured per-image and per-Article
limits before each request, isolates per-item failures, and treats cache I/O as best-effort rather
than a reason to lose valid downloaded media. Results contain at most the configured image-count
limit and record a bounded `omitted_count` instead of creating an unbounded list of skip records.
Every admitted occurrence counts toward the Article byte budget, including a repeated URL served
from cache, so downstream decode and analysis memory remains bounded; the cache still avoids the
second network request.

The acquisition layer is an internal primitive and is not yet connected to Article Evidence,
Media Evidence analysis, or a CLI control. Therefore the existing 0.5.1 commands still perform no
media downloads. Derived QR/OCR result caching remains deferred until those analyzers exist.

## Goal and non-goals

Media Analysis turns image URLs already observed in a successfully read WeChat Article into bounded derived evidence. It supports recruitment posters and QR codes while preserving a strict distinction between WeChat source facts and machine-derived observations.

It does not search for extra images, visit Article external links, open QR destinations, operate Mini Programs, search company websites or ATS systems, classify recruitment batches or jobs, upload images to a cloud OCR service, or change any Official Account content.

## Activation and trust boundary

Media Analysis is off by default and starts only after Article Evidence exists. The approved local controls are:

```powershell
wxcli --json article evidence "WECHAT_URL" --analyze-media
wxcli --json discovery search "校园招聘" --hydrate --analyze-media
wxcli --json discovery hydrate --input candidates.json --analyze-media
```

Local capability inspection is read-only:

```powershell
wxcli --json media doctor
```

It reports supported image decoders, the packaged standard-QR analyzer, Windows OCR availability, and installed OCR languages. It does not contact a remote service or install, download, or enable a component.

A trusted Direct Discovery Request may set `analyze_media: true` for its own invocation. A Candidate Batch is untrusted candidate data and cannot enable Media Analysis, choose download limits, or choose a local output path. Search snippets and candidate metadata never become OCR or QR evidence.

Media-enabled Direct Discovery Requests use request schema v2, for example:

```json
{
  "schema_version": "2",
  "query": "校园招聘",
  "hydrate": true,
  "analyze_media": true,
  "ocr_language": "zh-Hans"
}
```

Candidate Batch input remains schema v1. A local `--analyze-media` control may request media processing for that batch, but no field inside the Candidate Batch may do so.

Media controls do not grant Chrome access. Browser authorization is still resolved independently under [ADR-0006](adr/0006-persist-browser-fallback-authorization-outside-candidate-input.md).

## Separate schema and hashes

Article Evidence schema v1, `content_sha256`, and `evidence_sha256` keep their 0.4.0 meanings. Media Analysis produces a separate Media Evidence schema v1 linked by the exact source `content_sha256`.

Commands that do not enable Media Analysis retain their existing outer schema-v1 result exactly. A media-enabled command returns outer result schema v2 containing the unchanged Article Evidence schema v1 and the separate Media Evidence schema v1. This makes the new contract explicit while leaving old requests and consumers untouched.

The Media Evidence document records:

- its own schema and extractor versions;
- the source `content_sha256`;
- analysis start and finish times;
- a stable Media Evidence hash that excludes run timestamps and cache-hit status;
- `partial` and summary counts;
- one Media Item Evidence outcome per Article image occurrence.

Each Media Item Evidence preserves the Article image index and source URL. Its stable top-level status is `analyzed`, `skipped`, or `failed`; a separate categorical reason explains outcomes such as `blocked_host`, `too_large`, `unsupported_format`, or `ocr_unavailable` without expanding the status enum. When download succeeds it may include byte SHA-256, media type, byte length, dimensions, QR Evidence, and OCR Evidence. A failed or skipped item never invents derived content.

OCR text never merges into `Article.content_markdown`, never changes the core Article hashes, and never becomes a publication or account-identity fact. QR content is also derived evidence rather than a verified destination.

## Download boundary

Only HTTPS URLs already present in `Article.images` and hosted on a maintained allowlist of tested WeChat media CDN hosts are eligible. Unknown or external image hosts remain visible as skipped source URLs but are not fetched.

Before the request and after every redirect, wxcli validates the scheme, normalized host, resolved IP addresses, and final destination. It rejects loopback, link-local, private, reserved, non-HTTPS, unapproved-host, and unsafe redirect targets. It never expands arbitrary short links.

The downloader uses a dedicated client and sends no Cookie, Browser Session data, Authorization header, search API key, Official Account credential, or caller-supplied header. It does not reuse a browser page or a user's normal Chrome session.

A network failure retries once. HTTP 429 may honor a bounded delay of at most ten seconds and retry once. HTTP 401 or 403, invalid content, unsupported format, exhausted resource limit, and any security rejection do not retry. wxcli never switches to another host, mirror, browser, or media source after a download failure.

After download, the declared media type, detected raster format, dimensions, and byte limits must agree with a safely decoded image. Version 0.6.0 supports JPEG, PNG, static WebP, and only the first frame of GIF. It rejects SVG, active/vector content, unsupported formats, malformed images, and decoded images larger than 40 million pixels. File extensions and remote `Content-Type` values are hints, never sufficient proof of a safe raster image.

## Resource limits

Default hard limits are:

| Scope | Limit |
| --- | ---: |
| One image | 10 MB and 20 seconds |
| One Article | 50 images and 100 MB |
| One batch | 200 images and 400 MB |
| Command without Media Analysis | 10 minutes |
| Media-enabled command | 20 minutes total |
| Hydration inside a media-enabled command | First 10 minutes at most |
| Media phase | Remaining time, at most 10 minutes |

Limits apply to downloaded bytes, not only declared `Content-Length`. Once a byte or time limit is exhausted, completed Article and Media Evidence remains available and selected but unattempted items receive bounded-limit outcomes. An Article with more image occurrences than its configured image-count limit keeps only the bounded prefix and records the number omitted. Media ordering follows Article/candidate order and then image index so the partial result is deterministic.

At most four images download concurrently and at most two local image analyses run concurrently. Concurrency never changes evidence order: results are emitted by Article/candidate order and then Article image index.

## QR Evidence

Standard QR decoding runs through a packaged local analyzer against successfully decoded image bytes. Possible WeChat Mini Program codes may be identified as unsupported or unknown but are not promised to decode.

Each decoded payload is an inert string with its source image, payload hash, stable location ordering, and a conservative type such as URL, text, contact, or unknown. One image may produce at most 20 payloads and each payload is limited to 4 KB. Excess items or bytes receive explicit bounded-limit outcomes. wxcli never opens the payload, follows redirects, launches Chrome, invokes another application, executes commands, or promotes it to an External Link Handoff without an explicit future contract.

The complete payload appears only in explicitly requested machine JSON or an Evidence Bundle. Human-oriented terminal output shows a sanitized, length-bounded summary so control characters or very long payloads cannot manipulate the terminal.

## OCR Evidence

OCR runs locally through a replaceable provider and records engine name, engine version, requested/detected language where available, confidence where supported, source image identity, and extracted text. The first 0.6.0 provider uses Windows local OCR and already installed language data. The provider receives only the bounded local image bytes.

The deterministic default OCR language is Simplified Chinese, `zh-Hans`. A caller may change it through `--ocr-language` or a trusted Direct Discovery Request schema v2. Candidate Batch data cannot choose it. If the requested Windows language capability is absent, the result is `ocr_unavailable`; wxcli does not silently substitute the operating-system display language or another OCR engine.

wxcli performs only deterministic line-ending normalization, Unicode normalization, and terminal-control-character removal. It never spell-corrects, rewrites, translates, infers company names, guesses dates, or otherwise presents modified OCR as engine output. Normalized OCR text is limited to 50,000 characters per image and 1,000,000 characters per batch; a bounded result records `truncated: true`.

If no supported local OCR engine or language data is available, the image records `ocr_unavailable`. QR decoding, Article Evidence, and other images continue. wxcli never silently switches to a remote OCR API and never uploads the image for analysis.

## Partial-result semantics

Core Article Evidence is the prerequisite and remains successful when a media item fails. Media Evidence sets `partial: true` when an eligible item could not complete and records each outcome separately, including download failure, blocked destination, size or time limit, unsupported or malformed image, QR not found, QR decode failure, OCR unavailable, and OCR failure.

`qr_not_found` is an ordinary completed observation, not an error. One failed image does not stop unrelated images unless an invocation-wide time or byte limit is exhausted. A batch keeps successful Article Evidence and successful Media Evidence even when later work is partial.

## Deduplication and Media Cache

Downloaded bytes are keyed by SHA-256. Identical bytes are decoded, QR-scanned, and OCR-processed once, while Media Item Evidence preserves every Article occurrence and source URL that referred to those bytes.

Derived cache keys also include analyzer identity and version, OCR language, normalization version, and relevant analysis configuration. An engine, language, or configuration change re-runs analysis. The unchanged original image bytes may still be reused safely by SHA-256.

The dedicated Media Cache retains public image bytes and derived results for seven days and is capped at 1 GB. Eviction is deterministic and never removes an explicit Evidence Bundle. Cache records contain no Cookie, authorization header, API key, browser state, raw HTTP request, or caller-owned secret.

Every cache read recomputes and verifies the expected byte SHA-256 before decoding or analysis. A mismatched or unreadable entry is removed individually and may be downloaded again under the normal policy. Cleanup removes expired entries first and then evicts the least recently used entries until the cache is within 1 GB. An invalid cache entry never becomes Media Evidence.

The approved clear command is:

```powershell
wechat-oa --json media cache clear
```

It does not clear Article Cache, discovery history/checkpoints, Browser Session state, Browser Fallback Policy, credentials, or Evidence Bundles.

## Evidence Bundles

An Evidence Bundle is optional and requires Media Analysis. A caller may request it through an explicit CLI output directory. A trusted Direct Discovery Request may also specify the output directory because that document is a caller-owned control plane; Candidate Batch data may not specify any filesystem path.

By default the bundle contains the original bytes of every successfully analyzed image under the existing resource limits. wxcli does not resize, recompress, re-encode, or otherwise modify those bytes. Generated filenames use only deterministic image indexes and SHA-256 values. `--bundle-metadata-only` omits binary image artifacts while retaining manifests, hashes, and per-image outcomes; it causes no additional download.

The destination must not exist and must not resolve through a Windows symbolic link, junction, mount point, or other reparse point. Parent and staging paths are resolved and guarded before each destructive cleanup. wxcli writes a sibling staging directory, verifies the manifest and hashes, and atomically renames it to the requested destination. It never offers `--force`, overwrites, or merges into an existing file or directory. Failure removes only the guarded staging directory and leaves any pre-existing paths untouched.

Destination and capability preflight runs before any live Article or image request. Because a Bundle is an explicitly requested deliverable, a later staging, disk, manifest, hash-verification, or final-rename failure returns a nonzero command error rather than claiming partial bundle success.

On Ctrl+C or equivalent cancellation, wxcli schedules no new downloads or analyses, closes active files and local analyzers, rolls back incomplete cache transactions, and removes only its guarded incomplete bundle staging directory. It preserves valid cache entries and any previously completed final Evidence Bundle.

The bundle contains versioned Article Evidence and Media Evidence JSON, Article Markdown, external-link and image manifests, hashes, and explicitly requested image artifacts. It omits raw search-provider responses, API credentials, Cookies, request headers, Browser Session data, and full dynamic WeChat HTML. Creating a bundle does not open it or any contained link.

## Test and acceptance contract

Ordinary tests remain offline. They use synthetic image fixtures, fake DNS and HTTP transports, fake QR/OCR providers, temporary caches and destinations, and fake clocks. They cover:

- default no-download behavior and Candidate Batch inability to enable media work;
- schema-v1 non-media compatibility, schema-v2 media request/result selection, Article Evidence schema/hash compatibility, and separate Media Evidence linking;
- HTTPS/host/IP/redirect validation, misleading content lengths, oversized and decompression-heavy images;
- JPEG, PNG, WebP, first-frame GIF behavior, SVG rejection, format sniffing, and the 40-million-pixel limit;
- byte, count, time, batch, and cache limits;
- four-download/two-analysis concurrency with deterministic output ordering;
- duplicate bytes across different URLs and preserved occurrence mappings;
- multiple QR payload ordering and count/size bounds, inert data treatment, terminal sanitization, and no destination visit;
- Windows OCR capability reporting, unavailable/failure behavior, normalization and text bounds, analyzer-version cache invalidation, and proof that no cloud fallback runs;
- explicit `zh-Hans` default and absence of locale-based language switching;
- bounded network and 429 retry behavior with no retry or source switch for terminal failures;
- SHA-256 cache verification, corrupt-entry removal, expiry-first cleanup, and least-recently-used eviction;
- per-image partial outcomes and deterministic ordering;
- guarded atomic bundle creation, existing/reparse-destination refusal, metadata-only mode, original-byte preservation, generated safe names, and safe cleanup;
- requested-bundle nonzero failure behavior and cancellation cleanup without deleting completed artifacts;
- absence of Cookies, authorization data, raw search responses, and dynamic HTML from output, cache, fixtures, and bundles.

The standard-QR analyzer is required in the packaged EXE and its absence fails the build. Windows OCR is an optional operating-system capability and its absence does not prevent wxcli startup. Offline packaged smoke covers `media doctor`, a standard-QR fixture, `ocr_unavailable`, schema selection, and guarded atomic Bundle creation.

Packaged live tests require explicit authorization for real WeChat reads and a separate explicit Media Analysis control. They never open QR destinations. Fixtures must be owned or clearly licensed for the test. Real Article text, images, and QR payloads stay in a Git-ignored temporary acceptance directory; retained reports contain only statistics and sanitized summaries. Acceptance uses a frozen, rights-cleared image benchmark and requires at least 95% successful standard-QR decoding with zero incorrect payloads on that benchmark, Chinese OCR character error rate no greater than 10% on readable recruitment posters with the supported local engine installed, no fetch outside the approved WeChat media hosts, and no regression in core Article Evidence hashes.

See [ADR-0007](adr/0007-keep-media-analysis-separate-from-core-article-evidence.md).
