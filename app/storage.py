from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


PLANS = {"free", "pro", "agency"}
ACTIVE_STATUSES = {"queued", "running", "completed"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def month_start_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()


@dataclass(frozen=True)
class User:
    telegram_user_id: int
    username: str | None
    first_name: str | None
    plan: str


@dataclass(frozen=True)
class Job:
    id: int
    telegram_user_id: int
    chat_id: int
    url: str
    plan: str
    status: str
    pages: int
    created_at: str
    started_at: str | None
    finished_at: str | None
    output_dir: str | None
    error: str | None


class QuotaExceeded(RuntimeError):
    def __init__(self, used: int, limit: int, plan: str) -> None:
        super().__init__(f"quota exceeded: {used}/{limit} ({plan})")
        self.used = used
        self.limit = limit
        self.plan = plan


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    plan TEXT NOT NULL DEFAULT 'free' CHECK(plan IN ('free','pro','agency')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id INTEGER NOT NULL REFERENCES users(telegram_user_id),
                    chat_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    plan TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled')),
                    pages INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    output_dir TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_user_created ON jobs(telegram_user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
                """
            )

    def upsert_user(self, telegram_user_id: int, username: str | None, first_name: str | None) -> User:
        now = utcnow()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users(telegram_user_id, username, first_name, plan, created_at, updated_at)
                VALUES (?, ?, ?, 'free', ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    updated_at=excluded.updated_at
                """,
                (telegram_user_id, username, first_name, now, now),
            )
        return self.get_user(telegram_user_id)

    def get_user(self, telegram_user_id: int) -> User:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE telegram_user_id=?", (telegram_user_id,)).fetchone()
        if row is None:
            raise KeyError(telegram_user_id)
        return User(row["telegram_user_id"], row["username"], row["first_name"], row["plan"])

    def set_plan(self, telegram_user_id: int, plan: str) -> None:
        if plan not in PLANS:
            raise ValueError(f"invalid plan: {plan}")
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE users SET plan=?, updated_at=? WHERE telegram_user_id=?",
                (plan, utcnow(), telegram_user_id),
            )
            if cur.rowcount != 1:
                raise KeyError(telegram_user_id)

    def usage_this_month(self, telegram_user_id: int) -> int:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM jobs
                   WHERE telegram_user_id=? AND created_at>=?
                   AND status IN ('queued','running','completed')""",
                (telegram_user_id, month_start_iso()),
            ).fetchone()
        return int(row["n"])

    def create_job_with_quota(self, *, telegram_user_id: int, chat_id: int, url: str, pages: int, limit: int) -> Job:
        now = utcnow()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                user = conn.execute("SELECT plan FROM users WHERE telegram_user_id=?", (telegram_user_id,)).fetchone()
                if user is None:
                    raise KeyError(telegram_user_id)
                used = conn.execute(
                    """SELECT COUNT(*) AS n FROM jobs
                       WHERE telegram_user_id=? AND created_at>=?
                       AND status IN ('queued','running','completed')""",
                    (telegram_user_id, month_start_iso()),
                ).fetchone()["n"]
                if limit >= 0 and used >= limit:
                    raise QuotaExceeded(int(used), limit, user["plan"])
                cur = conn.execute(
                    """INSERT INTO jobs(telegram_user_id, chat_id, url, plan, status, pages, created_at)
                       VALUES (?, ?, ?, ?, 'queued', ?, ?)""",
                    (telegram_user_id, chat_id, url, user["plan"], pages, now),
                )
                job_id = int(cur.lastrowid)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.get_job(job_id)

    def get_job(self, job_id: int) -> Job:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return Job(**dict(row))

    def mark_running(self, job_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET status='running', started_at=?, error=NULL WHERE id=? AND status='queued'",
                (utcnow(), job_id),
            )

    def mark_completed(self, job_id: int, output_dir: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET status='completed', finished_at=?, output_dir=?, error=NULL WHERE id=?",
                (utcnow(), output_dir, job_id),
            )

    def mark_failed(self, job_id: int, error: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET status='failed', finished_at=?, error=? WHERE id=?",
                (utcnow(), error[:2000], job_id),
            )

    def cancel_queued(self, job_id: int, telegram_user_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status='cancelled', finished_at=? WHERE id=? AND telegram_user_id=? AND status='queued'",
                (utcnow(), job_id, telegram_user_id),
            )
        return cur.rowcount == 1

    def recent_jobs(self, telegram_user_id: int, limit: int = 5) -> list[Job]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE telegram_user_id=? ORDER BY id DESC LIMIT ?",
                (telegram_user_id, limit),
            ).fetchall()
        return [Job(**dict(row)) for row in rows]

    def pending_jobs(self) -> list[Job]:
        with self._conn() as conn:
            conn.execute("UPDATE jobs SET status='queued', started_at=NULL WHERE status='running'")
            rows = conn.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id").fetchall()
        return [Job(**dict(row)) for row in rows]
