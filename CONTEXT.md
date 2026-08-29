# wxcli Content Access

This context names the information wxcli discovers and reads from public WeChat pages, local files, and explicitly authorized Official Account APIs, plus narrowly scoped creation and safe replacement of unpublished drafts. It excludes publishing, account administration, company recruitment modeling, and non-WeChat website retrieval.

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

## Discovery and evidence

**Discovery Provider**:
A read-only adapter used by wxcli during Direct Discovery to find possible Public URLs without reading or validating their article content.
_Avoid_: Content Provider, WeChat index, scraper

**Search Orchestrator**:
An external agent that chooses search strategies and submits possible Public URLs to wxcli without claiming that they are readable or trustworthy Articles.
_Avoid_: Discovery Provider, Content Provider, evidence generator

**External Discovery Provider**:
A search service used by a Search Orchestrator outside wxcli to find possible Public URLs.
_Avoid_: Discovery Provider, Content Provider, WeChat index

**Direct Discovery**:
Discovery initiated by wxcli through a Discovery Provider.
_Avoid_: Agent-Orchestrated Discovery, Hydration

**Agent-Orchestrated Discovery**:
Discovery initiated by a Search Orchestrator through one or more External Discovery Providers, then handed to wxcli as a Candidate Batch.
_Avoid_: Direct Discovery, Article Evidence

**Discovery Query**:
A bounded request for possible WeChat Articles using search terms, optional account expectations, and an optional publication window.
_Avoid_: crawl, watchlist, recruitment batch

**Article Candidate**:
A strictly validated Public URL accepted through Direct Discovery or Candidate Ingestion but not yet proven to contain a readable Article.
_Avoid_: Article, search result article, verified result

**Candidate Batch**:
A bounded collection of untrusted possible Public URLs and search hints submitted by a Search Orchestrator for wxcli validation and optional Hydration.
_Avoid_: Article Evidence, search response, recruitment batch

**Candidate Ingestion**:
The boundary where wxcli validates, normalizes, deduplicates, and records a Candidate Batch without trusting its search hints as WeChat source facts.
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
An external URL observed in Article Evidence and passed to a caller without wxcli visiting, classifying as a recruitment channel, or operating the destination.
_Avoid_: website crawl, application channel, verified job link

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
