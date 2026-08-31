"""Local SQLite state for short search caching and incremental candidate history."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wxcli.discovery.models import SearchPage
from wxcli.errors import ErrorCode, WxcliError


class DiscoveryStore:
    """Persist sanitized discovery state; never stores credentials or request headers."""

    def __init__(
        self,
        path: Path,
        *,
        cache_ttl: timedelta = timedelta(minutes=15),
        history_ttl: timedelta = timedelta(days=180),
    ) -> None:
        self.path = path
        self.cache_ttl = cache_ttl
        self.history_ttl = history_ttl

    def get_page(
        self, provider: str, fingerprint: str, offset: int, now: datetime
    ) -> SearchPage | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT response_json, expires_at FROM search_cache "
                "WHERE provider = ? AND query_fingerprint = ? AND page_offset = ?",
                (provider, fingerprint, offset),
            ).fetchone()
            if row is None:
                return None
            if datetime.fromisoformat(str(row[1])) <= now.astimezone(UTC):
                with connection:
                    connection.execute(
                        "DELETE FROM search_cache WHERE provider = ? "
                        "AND query_fingerprint = ? AND page_offset = ?",
                        (provider, fingerprint, offset),
                    )
                return None
            try:
                return SearchPage.model_validate_json(str(row[0]))
            except ValueError:
                return None

    def put_page(
        self,
        provider: str,
        fingerprint: str,
        offset: int,
        page: SearchPage,
        now: datetime,
    ) -> None:
        expires_at = now.astimezone(UTC) + self.cache_ttl
        with self._connect() as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO search_cache "
                "(provider, query_fingerprint, page_offset, response_json, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    provider,
                    fingerprint,
                    offset,
                    page.model_dump_json(),
                    expires_at.isoformat(),
                ),
            )

    def observe_candidate(
        self,
        fingerprint: str,
        identity: str,
        fetch_url: str,
        now: datetime,
    ) -> tuple[datetime, datetime, bool]:
        seen_at = now.astimezone(UTC)
        with self._connect() as connection, connection:
            row = connection.execute(
                "SELECT first_seen_at FROM candidate_history "
                "WHERE query_fingerprint = ? AND article_identity = ?",
                (fingerprint, identity),
            ).fetchone()
            is_new = row is None
            first_seen = seen_at if row is None else datetime.fromisoformat(str(row[0]))
            connection.execute(
                "INSERT INTO candidate_history "
                "(query_fingerprint, article_identity, fetch_url, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(query_fingerprint, article_identity) DO UPDATE SET "
                "fetch_url = excluded.fetch_url, last_seen_at = excluded.last_seen_at",
                (
                    fingerprint,
                    identity,
                    fetch_url,
                    first_seen.isoformat(),
                    seen_at.isoformat(),
                ),
            )
        return first_seen, seen_at, is_new

    def prune(self, now: datetime) -> int:
        cutoff = now.astimezone(UTC) - self.history_ttl
        with self._connect() as connection, connection:
            first = connection.execute(
                "DELETE FROM search_cache WHERE expires_at <= ?", (now.astimezone(UTC).isoformat(),)
            ).rowcount
            second = connection.execute(
                "DELETE FROM candidate_history WHERE last_seen_at < ?", (cutoff.isoformat(),)
            ).rowcount
            third = connection.execute(
                "DELETE FROM discovery_checkpoint WHERE observed_before < ?", (cutoff.isoformat(),)
            ).rowcount
        return max(first, 0) + max(second, 0) + max(third, 0)

    def put_checkpoint(
        self, provider: str, fingerprint: str, observed_before: datetime
    ) -> None:
        with self._connect() as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO discovery_checkpoint "
                "(provider, query_fingerprint, observed_before) VALUES (?, ?, ?)",
                (provider, fingerprint, observed_before.astimezone(UTC).isoformat()),
            )

    def clear(self) -> int:
        with self._connect() as connection, connection:
            first = connection.execute("DELETE FROM search_cache").rowcount
            second = connection.execute("DELETE FROM candidate_history").rowcount
            third = connection.execute("DELETE FROM discovery_checkpoint").rowcount
        return max(first, 0) + max(second, 0) + max(third, 0)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=5.0)
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS search_cache ("
                "provider TEXT NOT NULL, query_fingerprint TEXT NOT NULL, "
                "page_offset INTEGER NOT NULL, response_json TEXT NOT NULL, expires_at TEXT NOT NULL, "
                "PRIMARY KEY (provider, query_fingerprint, page_offset))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS candidate_history ("
                "query_fingerprint TEXT NOT NULL, article_identity TEXT NOT NULL, "
                "fetch_url TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, "
                "PRIMARY KEY (query_fingerprint, article_identity))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS discovery_checkpoint ("
                "provider TEXT NOT NULL, query_fingerprint TEXT NOT NULL, "
                "observed_before TEXT NOT NULL, "
                "PRIMARY KEY (provider, query_fingerprint))"
            )
            connection.execute("PRAGMA user_version = 1")
            yield connection
        except (OSError, sqlite3.Error) as error:
            raise WxcliError(
                ErrorCode.LOCAL_CONFIGURATION_ERROR,
                "The discovery state database is unavailable.",
            ) from error
        finally:
            if "connection" in locals():
                connection.close()
