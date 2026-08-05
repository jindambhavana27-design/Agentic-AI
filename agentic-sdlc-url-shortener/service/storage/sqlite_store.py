"""SQLite-backed :class:`LinkStore`.

Design notes
------------
* Connections are thread-local. The HTTP server is threaded and SQLite
  connection objects are not safe to share across threads.
* WAL mode plus a busy timeout lets readers proceed during writes, which
  matters because the redirect path is read-dominated.
* Click analytics are stored pre-aggregated (per code per day, and per
  referrer) rather than as raw rows. Raw event storage grows without bound and
  the only queries we serve are aggregates anyway.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import time
from typing import List, Optional, Tuple

from ..errors import ConflictError, StorageError
from ..models import ClickEvent, Link, LinkStats
from .base import LinkStore

SCHEMA_VERSION = 2

_MIGRATIONS = [
    # v1 -- links table
    """
    CREATE TABLE IF NOT EXISTS links (
        code            TEXT PRIMARY KEY,
        target_url      TEXT NOT NULL,
        created_at      REAL NOT NULL,
        expires_at      REAL,
        deleted_at      REAL,
        created_by      TEXT,
        idempotency_key TEXT,
        custom_alias    INTEGER NOT NULL DEFAULT 0,
        metadata        TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_links_created ON links (created_at DESC, code DESC);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_links_idem
        ON links (created_by, idempotency_key)
        WHERE idempotency_key IS NOT NULL;
    """,
    # v2 -- pre-aggregated click analytics
    """
    CREATE TABLE IF NOT EXISTS click_daily (
        code     TEXT NOT NULL,
        day      TEXT NOT NULL,
        clicks   INTEGER NOT NULL DEFAULT 0,
        first_at REAL,
        last_at  REAL,
        PRIMARY KEY (code, day)
    );
    CREATE TABLE IF NOT EXISTS click_referrer (
        code     TEXT NOT NULL,
        referrer TEXT NOT NULL,
        clicks   INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (code, referrer)
    );
    """,
]


def _day_key(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


class SQLiteLinkStore(LinkStore):
    def __init__(self, path: str, busy_timeout_ms: int = 5000) -> None:
        self.path = path
        self._busy_timeout_ms = busy_timeout_ms
        self._local = threading.local()
        self._migrate_lock = threading.Lock()
        directory = os.path.dirname(os.path.abspath(path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        self._migrate()

    # -- connection management ------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self._busy_timeout_ms / 1000.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        # PRAGMA values cannot be bound as parameters, so the statement is built
        # from an int we own. Never interpolate caller-supplied data here.
        busy_timeout_sql = "PRAGMA busy_timeout=%d" % int(self._busy_timeout_ms)
        conn.execute(busy_timeout_sql)
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def _migrate(self) -> None:
        with self._migrate_lock:
            conn = self.conn
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            for version in range(current, len(_MIGRATIONS)):
                conn.executescript(_MIGRATIONS[version])
                # As above: PRAGMA takes no parameters. The value is a loop index.
                set_version_sql = "PRAGMA user_version = %d" % (version + 1)
                conn.execute(set_version_sql)
                conn.commit()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- links ----------------------------------------------------------------

    def create(self, link: Link) -> Link:
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO links (code, target_url, created_at, expires_at,
                                       deleted_at, created_by, idempotency_key,
                                       custom_alias, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        link.code,
                        link.target_url,
                        link.created_at,
                        link.expires_at,
                        link.deleted_at,
                        link.created_by,
                        link.idempotency_key,
                        1 if link.custom_alias else 0,
                        json.dumps(link.metadata, separators=(",", ":")),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("code or idempotency key already exists", {"code": link.code}) from exc
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            raise StorageError("failed to persist link: %s" % exc) from exc
        return link

    def get(self, code: str) -> Optional[Link]:
        row = self.conn.execute("SELECT * FROM links WHERE code = ?", (code,)).fetchone()
        return _row_to_link(row) if row else None

    def find_by_idempotency_key(self, key: str, owner: Optional[str]) -> Optional[Link]:
        row = self.conn.execute(
            "SELECT * FROM links WHERE idempotency_key = ? AND created_by IS ?",
            (key, owner),
        ).fetchone()
        return _row_to_link(row) if row else None

    def soft_delete(self, code: str, at: float) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE links SET deleted_at = ? WHERE code = ? AND deleted_at IS NULL",
                (at, code),
            )
        return cur.rowcount > 0

    # Two static statements rather than one built by concatenation: nothing in
    # the SQL text ever originates from a caller.
    _LIST_FIRST_PAGE = (
        "SELECT * FROM links WHERE deleted_at IS NULL "
        "ORDER BY created_at DESC, code DESC LIMIT ?"
    )
    _LIST_NEXT_PAGE = (
        "SELECT * FROM links WHERE deleted_at IS NULL AND (created_at, code) < (?, ?) "
        "ORDER BY created_at DESC, code DESC LIMIT ?"
    )

    def list(self, limit: int, cursor: Optional[str]) -> Tuple[List[Link], Optional[str]]:
        if cursor:
            created_at, code = _decode_cursor(cursor)
            rows = self.conn.execute(
                self._LIST_NEXT_PAGE, (created_at, code, limit + 1)
            ).fetchall()
        else:
            rows = self.conn.execute(self._LIST_FIRST_PAGE, (limit + 1,)).fetchall()

        has_more = len(rows) > limit
        rows = rows[:limit]
        links = [_row_to_link(r) for r in rows]
        next_cursor = _encode_cursor(links[-1].created_at, links[-1].code) if has_more and links else None
        return links, next_cursor

    # -- analytics ------------------------------------------------------------

    def record_click(self, event: ClickEvent) -> None:
        day = _day_key(event.timestamp)
        referrer = _normalise_referrer(event.referrer)
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO click_daily (code, day, clicks, first_at, last_at)
                    VALUES (?, ?, 1, ?, ?)
                    ON CONFLICT(code, day) DO UPDATE SET
                        clicks  = clicks + 1,
                        last_at = excluded.last_at
                    """,
                    (event.code, day, event.timestamp, event.timestamp),
                )
                self.conn.execute(
                    """
                    INSERT INTO click_referrer (code, referrer, clicks)
                    VALUES (?, ?, 1)
                    ON CONFLICT(code, referrer) DO UPDATE SET clicks = clicks + 1
                    """,
                    (event.code, referrer),
                )
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            raise StorageError("failed to record click: %s" % exc) from exc

    def stats(self, code: str, days: int) -> LinkStats:
        cutoff_day = _day_key(time.time() - days * 86400)
        rows = self.conn.execute(
            """
            SELECT day, clicks, first_at, last_at FROM click_daily
            WHERE code = ? AND day >= ?
            ORDER BY day ASC
            """,
            (code, cutoff_day),
        ).fetchall()
        clicks_by_day = {r["day"]: r["clicks"] for r in rows}
        total = sum(clicks_by_day.values())
        first_at = min((r["first_at"] for r in rows if r["first_at"] is not None), default=None)
        last_at = max((r["last_at"] for r in rows if r["last_at"] is not None), default=None)

        ref_rows = self.conn.execute(
            "SELECT referrer, clicks FROM click_referrer WHERE code = ? ORDER BY clicks DESC LIMIT 10",
            (code,),
        ).fetchall()

        return LinkStats(
            code=code,
            total_clicks=total,
            unique_days=len(clicks_by_day),
            first_click_at=first_at,
            last_click_at=last_at,
            clicks_by_day=clicks_by_day,
            top_referrers={r["referrer"]: r["clicks"] for r in ref_rows},
        )

    def health(self) -> bool:
        try:
            self.conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:  # pragma: no cover - defensive
            return False


# -- helpers ------------------------------------------------------------------


def _row_to_link(row: sqlite3.Row) -> Link:
    return Link(
        code=row["code"],
        target_url=row["target_url"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        deleted_at=row["deleted_at"],
        created_by=row["created_by"],
        idempotency_key=row["idempotency_key"],
        custom_alias=bool(row["custom_alias"]),
        metadata=json.loads(row["metadata"] or "{}"),
    )


def _normalise_referrer(referrer: Optional[str]) -> str:
    if not referrer:
        return "direct"
    # Store only the origin: full referrer URLs carry user-identifying paths and
    # query strings we have no reason to retain.
    from urllib.parse import urlsplit

    parts = urlsplit(referrer)
    if parts.scheme and parts.hostname:
        return "%s://%s" % (parts.scheme, parts.hostname)
    return "unknown"


def _encode_cursor(created_at: float, code: str) -> str:
    raw = "%r|%s" % (created_at, code)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> Tuple[float, str]:
    from ..errors import ValidationError

    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + padding).decode("utf-8")
        created_at, code = raw.split("|", 1)
        return float(created_at), code
    except Exception as exc:
        raise ValidationError("cursor is malformed", {"cursor": cursor}) from exc
