"""Tests for public result schemas."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from wxcli.models import Article, DraftMessage, Provider, PublishedMessage


def example_article() -> Article:
    return Article(
        title="示例文章",
        content_markdown="# 内容",
        source_url="https://mp.weixin.qq.com/s/example",
        published_at=datetime(2026, 8, 26, tzinfo=UTC),
        images=["https://mmbiz.qpic.cn/example.jpg"],
        provider=Provider.HTTP,
    )


def test_message_models_keep_article_order() -> None:
    article = example_article()

    published = PublishedMessage(article_id="100", articles=[article])
    draft = DraftMessage(media_id="media-100", articles=[article])

    assert published.articles[0].title == "示例文章"
    assert draft.articles[0].provider is Provider.HTTP


def test_message_requires_at_least_one_article() -> None:
    with pytest.raises(ValidationError):
        PublishedMessage(article_id="100", articles=[])


def test_article_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Article(
            title="示例文章",
            content_markdown="内容",
            provider=Provider.LOCAL,
            unexpected="not allowed",
        )
