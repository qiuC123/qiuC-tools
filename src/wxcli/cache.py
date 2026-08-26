"""Small, successful-result-only disk cache for normalized public URLs."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wxcli.models import Article


class ArticleCache:
    """A one-hour JSON cache keyed by a normalized public URL."""

    def __init__(self, directory: Path, ttl: timedelta = timedelta(hours=1)) -> None:
        self.directory = directory
        self.ttl = ttl

    def get(self, url: str) -> Article | None:
        path = self._path(url)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            expires_at = datetime.fromisoformat(payload["expires_at"])
            if expires_at <= datetime.now(UTC):
                path.unlink(missing_ok=True)
                return None
            return Article.model_validate(payload["article"])
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def put(self, url: str, article: Article) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "expires_at": (datetime.now(UTC) + self.ttl).isoformat(),
            "article": article.model_dump(mode="json"),
        }
        self._path(url).write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )

    def clear(self) -> int:
        if not self.directory.exists():
            return 0
        count = 0
        for path in self.directory.glob("*.json"):
            path.unlink()
            count += 1
        return count

    def _path(self, url: str) -> Path:
        return self.directory / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}.json"
