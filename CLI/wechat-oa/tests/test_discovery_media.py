from datetime import UTC, datetime

import pytest
from pydantic import HttpUrl

from wxcli.discovery.media import (
    CandidateMediaEvidence,
    DiscoveryMediaAnalysisResult,
    DiscoveryMediaAnalyzer,
    DiscoveryMediaBatchLimits,
)
from wxcli.discovery.models import (
    ArticleCandidate,
    CandidateConfidence,
    DiscoveryResult,
    DiscoverySummary,
    SearchProvenance,
    VerificationStatus,
)
from wxcli.evidence import ExpectedAccount, build_article_evidence
from wxcli.media import (
    MediaItemEvidence,
    MediaItemReason,
    MediaItemStatus,
    build_media_evidence,
)
from wxcli.models import Provider
from wxcli.public_article import PublicArticleParser

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def article_evidence(token: str):
    url = f"https://mp.weixin.qq.com/s/{token}"
    document = PublicArticleParser.parse(
        f"""
        <h1 id="activity-name">{token}</h1>
        <a id="js_name">Acme Jobs</a>
        <div id="js_content"><p>Apply now</p></div>
        <script>var __biz='BIZ';</script>
        """,
        url,
        Provider.HTTP,
    )
    return build_article_evidence(
        document,
        [ExpectedAccount(biz_id="BIZ", display_names=["Acme Jobs"])],
        now=NOW,
    )


def candidate(token: str, *, verified: bool = True) -> ArticleCandidate:
    evidence = article_evidence(token) if verified else None
    return ArticleCandidate(
        fetch_url=HttpUrl(f"https://mp.weixin.qq.com/s/{token}"),
        article_identity=f"token:{token}",
        discovered_at=NOW,
        last_seen_at=NOW,
        search_provenance=SearchProvenance(
            provider="brave",
            rank=1,
            result_id=token,
        ),
        confidence=CandidateConfidence.HIGH if verified else CandidateConfidence.LOW,
        verification_status=(
            VerificationStatus.VERIFIED
            if verified
            else VerificationStatus.NOT_ATTEMPTED
        ),
        evidence=evidence,
    )


def discovery_result(*candidates: ArticleCandidate) -> DiscoveryResult:
    verified = sum(item.evidence is not None for item in candidates)
    return DiscoveryResult(
        search_provider="brave",
        checkpoint="checkpoint",
        summary=DiscoverySummary(
            received=len(candidates),
            accepted=len(candidates),
            duplicates_removed=0,
            hydration_attempted=verified,
            verified=verified,
            partial=False,
        ),
        candidates=list(candidates),
    )


def media_evidence(source_hash: str, *, item_count: int = 1):
    return build_media_evidence(
        source_content_sha256=source_hash,
        items=[
            MediaItemEvidence(
                index=index,
                source_url=f"https://mmbiz.qpic.cn/example/{index}",
                status=MediaItemStatus.SKIPPED,
                reason=MediaItemReason.RESOURCE_LIMIT,
            )
            for index in range(item_count)
        ],
        analysis_started_at=NOW,
        analysis_finished_at=NOW,
    )


def test_discovery_media_wrapper_preserves_core_result_and_links_verified_candidates() -> None:
    verified = candidate("T1")
    not_attempted = candidate("T2", verified=False)
    core = discovery_result(verified, not_attempted)
    observed = []

    def analyze(evidence, budget):
        observed.append((evidence.content_sha256, budget))
        return media_evidence(evidence.content_sha256)

    result = DiscoveryMediaAnalyzer(analyze).analyze(core)

    assert result.schema_version == "2"
    assert result.discovery_result is core
    assert [item.article_identity for item in result.media] == ["token:T1"]
    assert result.media[0].candidate_index == 0
    assert result.summary.eligible_articles == 1
    assert result.summary.analyzed_articles == 1
    assert result.summary.image_items == 1
    assert result.summary.partial is True
    assert observed[0][1].max_images == 50


def test_discovery_batch_image_limit_omits_later_articles_deterministically() -> None:
    core = discovery_result(candidate("T1"), candidate("T2"))
    calls = []

    def analyze(evidence, budget):
        calls.append((evidence, budget))
        return media_evidence(evidence.content_sha256)

    result = DiscoveryMediaAnalyzer(
        analyze,
        limits=DiscoveryMediaBatchLimits(max_images=1),
    ).analyze(core)

    assert len(calls) == 1
    assert calls[0][1].max_images == 1
    assert result.summary.analyzed_articles == 1
    assert result.summary.omitted_articles == 1
    assert result.summary.partial is True


def test_discovery_media_result_rejects_wrong_candidate_link() -> None:
    verified = candidate("T1")
    assert verified.evidence is not None
    core = discovery_result(verified)
    linked = CandidateMediaEvidence(
        candidate_index=0,
        article_identity="token:WRONG",
        media_evidence=media_evidence(verified.evidence.content_sha256),
    )

    with pytest.raises(ValueError, match="wrong article identity"):
        DiscoveryMediaAnalysisResult(
            discovery_result=core,
            limits=DiscoveryMediaBatchLimits(),
            summary={
                "eligible_articles": 1,
                "analyzed_articles": 1,
                "omitted_articles": 0,
                "image_items": 1,
                "omitted_images": 0,
                "downloaded_bytes": 0,
                "ocr_characters": 0,
                "partial": True,
            },
            media=(linked,),
        )
