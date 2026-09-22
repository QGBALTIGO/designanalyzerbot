from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _host(url: str) -> str:
    return (urlsplit(url).hostname or url).lower()


@dataclass(frozen=True)
class Snapshot:
    id: int
    telegram_user_id: int
    url: str
    host: str
    mode: str
    output_dir: str
    screenshot_path: str | None
    manifest_path: str | None
    html_path: str | None
    pdf_path: str | None
    warc_path: str | None
    markdown_path: str | None
    content_hash: str | None
    asset_count: int
    total_bytes: int
    technologies: tuple[str, ...]
    metadata: dict[str, Any]
    created_at: str


class PremiumStorage:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS premium_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    host TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    output_dir TEXT NOT NULL,
                    screenshot_path TEXT,
                    manifest_path TEXT,
                    html_path TEXT,
                    pdf_path TEXT,
                    warc_path TEXT,
                    markdown_path TEXT,
                    content_hash TEXT,
                    asset_count INTEGER NOT NULL DEFAULT 0,
                    total_bytes INTEGER NOT NULL DEFAULT 0,
                    technologies_json TEXT NOT NULL DEFAULT '[]',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_premium_snapshots_user_host_created
                    ON premium_snapshots(telegram_user_id, host, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_premium_snapshots_user_mode_created
                    ON premium_snapshots(telegram_user_id, mode, created_at DESC);
                """
            )

    def record_snapshot(
        self,
        *,
        telegram_user_id: int,
        url: str,
        mode: str,
        output_dir: Path,
        screenshot_path: Path | None = None,
        manifest_path: Path | None = None,
        html_path: Path | None = None,
        pdf_path: Path | None = None,
        warc_path: Path | None = None,
        markdown_path: Path | None = None,
        content_hash: str | None = None,
        asset_count: int = 0,
        total_bytes: int = 0,
        technologies: tuple[str, ...] | list[str] = (),
        metadata: dict[str, Any] | None = None,
    ) -> Snapshot:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO premium_snapshots(
                    telegram_user_id, url, host, mode, output_dir,
                    screenshot_path, manifest_path, html_path, pdf_path,
                    warc_path, markdown_path, content_hash,
                    asset_count, total_bytes, technologies_json, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    telegram_user_id,
                    url,
                    _host(url),
                    mode,
                    str(output_dir),
                    str(screenshot_path) if screenshot_path else None,
                    str(manifest_path) if manifest_path else None,
                    str(html_path) if html_path else None,
                    str(pdf_path) if pdf_path else None,
                    str(warc_path) if warc_path else None,
                    str(markdown_path) if markdown_path else None,
                    content_hash,
                    int(asset_count),
                    int(total_bytes),
                    json.dumps(list(technologies), ensure_ascii=False),
                    json.dumps(metadata or {}, ensure_ascii=False),
                    _utcnow(),
                ),
            )
            row_id = int(cur.lastrowid)
        return self.get_snapshot(telegram_user_id, row_id)

    def get_snapshot(self, telegram_user_id: int, snapshot_id: int) -> Snapshot:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM premium_snapshots WHERE id=? AND telegram_user_id=?",
                (snapshot_id, telegram_user_id),
            ).fetchone()
        if row is None:
            raise KeyError(snapshot_id)
        return self._row(row)

    def list_snapshots(
        self,
        telegram_user_id: int,
        *,
        url: str | None = None,
        mode: str | None = None,
        limit: int = 10,
    ) -> list[Snapshot]:
        clauses = ["telegram_user_id=?"]
        params: list[Any] = [telegram_user_id]
        if url:
            clauses.append("host=?")
            params.append(_host(url))
        if mode:
            clauses.append("mode=?")
            params.append(mode)
        params.append(max(1, min(100, int(limit))))
        sql = (
            "SELECT * FROM premium_snapshots WHERE "
            + " AND ".join(clauses)
            + " ORDER BY id DESC LIMIT ?"
        )
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row(row) for row in rows]

    def previous_snapshot(
        self,
        telegram_user_id: int,
        url: str,
        *,
        before_id: int | None = None,
    ) -> Snapshot | None:
        params: list[Any] = [telegram_user_id, _host(url)]
        extra = ""
        if before_id is not None:
            extra = " AND id < ?"
            params.append(before_id)
        with self._conn() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM premium_snapshots
                WHERE telegram_user_id=? AND host=?{extra}
                ORDER BY id DESC LIMIT 1
                """,
                params,
            ).fetchone()
        return self._row(row) if row is not None else None

    def _row(self, row: sqlite3.Row) -> Snapshot:
        try:
            technologies = tuple(json.loads(row["technologies_json"] or "[]"))
        except Exception:
            technologies = ()
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except Exception:
            metadata = {}
        return Snapshot(
            id=int(row["id"]),
            telegram_user_id=int(row["telegram_user_id"]),
            url=row["url"],
            host=row["host"],
            mode=row["mode"],
            output_dir=row["output_dir"],
            screenshot_path=row["screenshot_path"],
            manifest_path=row["manifest_path"],
            html_path=row["html_path"],
            pdf_path=row["pdf_path"],
            warc_path=row["warc_path"],
            markdown_path=row["markdown_path"],
            content_hash=row["content_hash"],
            asset_count=int(row["asset_count"] or 0),
            total_bytes=int(row["total_bytes"] or 0),
            technologies=technologies,
            metadata=metadata,
            created_at=row["created_at"],
        )
