# WeChat Official Account Content Access

This context names the information WeChat OA discovers and reads from public WeChat pages, local files, and explicitly authorized Official Account APIs, plus narrowly scoped creation and safe replacement of unpublished drafts. It excludes publishing, account administration, company recruitment modeling, and non-WeChat website retrieval.

## Product

**WeChat OA**:
The product dedicated to discovering, reading, and safely preparing content for WeChat Official Accounts.
_Avoid_: wxcli, WeChat client, WeChat automation

## Content

**Article**:
A single readable piece of WeChat content, including its title, body, publication information, and referenced images.
_Avoid_: post, page, message

**Published Message**:
An Official Account publication identified by `article_id` that contains one or more Articles.
_Avoid_: published article, news item

**Draft Message**:
An unpublished Official Account draft identified by `media_id` that contains one or more Articles.
_Avoid_: draft article, material

**Article Index**:
The zero-based position of an Article inside a Published Message or Draft Message with multiple articles.
_Avoid_: item number, child id

## Sources

**Content Provider**:
A read-only source adapter that obtains an Article or Message through public HTTP, visible Chrome, the Official Account API, or a local file.
_Avoid_: Provider, scraper, discovery backend

**Public URL**:
A WeChat article URL in one of the two explicitly supported `https://mp.weixin.qq.com/s/...` forms.
_Avoid_: WeChat link, share link

**Verification Required**:
A source result meaning WeChat requires human browser verification before content can be read. It is not permission to bypass verification.
_Avoid_: captcha failure, anti-bot workaround

**Browser Session**:
Browser-owned WeChat session state retained in one independent WeChat OA profile across Browser Runs without exposing its underlying Cookies.
_Avoid_: Cookie configuration, imported login, browser token

**Browser Run**:
One bounded visible-Chrome lifetime used to read one or more strict Public URLs while reusing the Browser Session, ending when that request or batch finishes.
_Avoid_: Browser Session, permanent browser, background service

**Browser Fallback Policy**:
A user-owned durable local choice controlling whether Verification Required may start a Browser Run after HTTP reading fails. A trusted Discovery Query may grant fallback for its own invocation, but no request changes the durable policy and no Candidate Batch may grant browser use.
_Avoid_: Candidate permission, automatic Cookie import, browser flag

**User Action Required**:
A browser-read outcome meaning the retained Browser Session cannot satisfy WeChat verification and a person must initialize or refresh it before automatic reading can continue.
_Avoid_: automatic CAPTCHA handling, indefinite wait, parse failure

## Discovery and evidence

**Discovery Provider**:
A read-only adapter used by WeChat OA during Direct Discovery to find possible Public URLs without reading or validating their article content.
_Avoid_: Content Provider, WeChat index, scraper

**Search Orchestrator**:
An external agent that chooses search strategies and submits possible Public URLs to WeChat OA without claiming that they are readable or trustworthy Articles.
_Avoid_: Discovery Provider, Content Provider, evidence generator

**External Discovery Provider**:
A search service used by a Search Orchestrator outside WeChat OA to find possible Public URLs.
_Avoid_: Discovery Provider, Content Provider, WeChat index

**Direct Discovery**:
Discovery initiated by WeChat OA through a Discovery Provider.
_Avoid_: Agent-Orchestrated Discovery, Hydration

**Agent-Orchestrated Discovery**:
Discovery initiated by a Search Orchestrator through one or more External Discovery Providers, then handed to WeChat OA as a Candidate Batch.
_Avoid_: Direct Discovery, Article Evidence

**Discovery Query**:
A bounded, caller-owned control request for possible WeChat Articles using search terms, optional account expectations, and an optional publication window. It may select per-invocation behavior but never changes user-level policy.
_Avoid_: crawl, watchlist, recruitment batch

**Article Candidate**:
A strictly validated Public URL accepted through Direct Discovery or Candidate Ingestion but not yet proven to contain a readable Article.
_Avoid_: Article, search result article, verified result

**Candidate Batch**:
A bounded collection of untrusted possible Public URLs and search hints submitted by a Search Orchestrator for WeChat OA validation and optional Hydration.
_Avoid_: Article Evidence, search response, recruitment batch

**Candidate Ingestion**:
The boundary where WeChat OA validates, normalizes, deduplicates, and records a Candidate Batch without trusting its search hints as WeChat source facts.
_Avoid_: Hydration, import Article, evidence creation

**Hydration**:
An explicitly requested attempt to turn an Article Candidate into Article Evidence by reading the Public URL through a Content Provider.
_Avoid_: crawl, verification bypass, enrichment

**Hydration Attempt**:
A record of an unsuccessful or incomplete Hydration, including its safe outcome category and attempt time without pretending that Article Evidence exists.
_Avoid_: Article Evidence, empty Article, discarded error

**Article Evidence**:
A successfully read Article together with its source identity, verification time, extracted links, and stable evidence fingerprints.
_Avoid_: Article Candidate, search snippet, recruitment record

**Account Identity Evidence**:
Observed Official Account identifiers and the result of comparing them with caller-supplied expected identities; it is evidence, not a decision that an account belongs to a company.
_Avoid_: company identity, official company flag, account ownership

**Search Cursor**:
An opaque continuation value for reading the next page of one Discovery Query.
_Avoid_: Discovery Checkpoint, page number

**Discovery Checkpoint**:
A continuation value for repeating the same Discovery Query while identifying candidates not previously seen for that query.
_Avoid_: Search Cursor, permanent business state

**External Link Handoff**:
An external URL observed in Article Evidence and passed to a caller without WeChat OA visiting, classifying as a recruitment channel, or operating the destination.
_Avoid_: website crawl, application channel, verified job link

**Media Analysis**:
An explicitly requested, bounded process that downloads eligible Article image bytes from approved WeChat media hosts and derives QR and OCR observations without changing the Article body or Article Evidence.
_Avoid_: automatic image crawl, Article parsing, external-link visit

**Media Evidence**:
A separately versioned set of derived image, QR, and OCR observations linked to one Article Evidence by its `content_sha256`; it may be partial and does not alter the core evidence fingerprint.
_Avoid_: Article Evidence v2, verified Article text, recruitment classification

**Media Item Evidence**:
The download and analysis outcome for one image occurrence in an Article, preserving its source index and URL even when identical image bytes are analyzed only once.
_Avoid_: image file, Article image URL, OCR document

**QR Evidence**:
An inert decoded payload and type observation from an eligible image. WeChat OA records it but never opens, executes, follows, or treats it as browser authorization.
_Avoid_: application link, verified destination, QR action

**OCR Evidence**:
Locally derived text with image origin, engine identity, language, and confidence metadata. It never merges into the Article body or becomes a WeChat source fact.
_Avoid_: Article text, cloud OCR result, corrected source

**Media Analyzer**:
A replaceable local QR or OCR implementation whose identity, version, language, and configuration participate in derived-result cache validity and provenance.
_Avoid_: cloud enrichment, Content Provider, image downloader

**Media Capability Report**:
A read-only local report of available image decoders, QR analyzer, OCR analyzer, and installed OCR languages without downloading engines or contacting a remote service.
_Avoid_: installation command, health proof, remote capability discovery

**Evidence Bundle**:
An explicitly requested, atomically created local directory containing versioned evidence documents, manifests, hashes, Markdown, and selected image artifacts without raw search responses or full dynamic WeChat HTML.
_Avoid_: cache, website export, automatic download folder

**Media Cache**:
A bounded, expiring, content-addressed local cache of public image bytes and derived results that stores no Cookie, authorization header, or Evidence Bundle.
_Avoid_: Browser Session, Article Cache, permanent archive

## Draft preparation and change

**Draft Import**:
A local, reversible conversion of one Word document and one cover image into a previewable draft package. It does not contact WeChat.
_Avoid_: publish, Word upload

**Draft Creation**:
An explicitly confirmed operation that uploads a prepared package and creates one new unpublished Draft Message. It never publishes or deletes content.
_Avoid_: edit公众号, publish article, sync

**Upload Checkpoint**:
A non-secret, local record keyed by the exact prepared package. It maps image hashes to uploaded URLs or media IDs so an interrupted upload can resume and identical images are uploaded only once.
_Avoid_: Cookie cache, credential file, upload log

**Draft Snapshot**:
An exact local backup of a Draft Message as returned by the Official Account API, together with a stable fingerprint used to detect later remote changes.
_Avoid_: cache, copy, export

**Draft Difference**:
A read-only comparison between one prepared Article and one indexed Article in a Draft Snapshot. It explains what would change without uploading or updating anything.
_Avoid_: patch, update result

**Draft Update Plan**:
A local package containing the prepared Article, Draft Snapshot, Draft Difference, and expected remote fingerprint. Applying it requires explicit confirmation and is refused if the remote draft has changed since planning.
_Avoid_: live edit, autosave, sync
