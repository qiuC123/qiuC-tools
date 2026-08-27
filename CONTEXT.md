# wxcli Content Access

This context names the information wxcli reads from public WeChat pages, local files, and explicitly authorized Official Account APIs, plus narrowly scoped creation and safe replacement of unpublished drafts. It excludes publishing and account administration.

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

**Provider**:
A read-only source adapter that obtains an Article or Message in one of four approved ways: public HTTP, visible Chrome, Official Account API, or a local file.
_Avoid_: scraper, backend

**Public URL**:
A WeChat article URL in one of the two explicitly supported `https://mp.weixin.qq.com/s/...` forms.
_Avoid_: WeChat link, share link

**Verification Required**:
A source result meaning WeChat requires human browser verification before content can be read. It is not permission to bypass verification.
_Avoid_: captcha failure, anti-bot workaround

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
