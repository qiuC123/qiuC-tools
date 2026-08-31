"""Public, read-only data models returned by wxcli providers."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class Provider(StrEnum):
    """Approved read-only ways to retrieve data."""

    HTTP = "http"
    CHROME = "chrome"
    OFFICIAL = "official"
    LOCAL = "local"


class Article(BaseModel):
    """A single readable article."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(default=0, ge=0)
    title: str
    content_markdown: str
    source_url: HttpUrl | None = None
    author: str | None = None
    published_at: datetime | None = None
    images: list[str] = Field(default_factory=list)
    provider: Provider


class PublishedMessage(BaseModel):
    """A published Official Account message, possibly with multiple articles."""

    model_config = ConfigDict(extra="forbid")

    article_id: str
    articles: list[Article] = Field(min_length=1)


class DraftMessage(BaseModel):
    """An unpublished Official Account message, possibly with multiple articles."""

    model_config = ConfigDict(extra="forbid")

    media_id: str
    articles: list[Article] = Field(min_length=1)
