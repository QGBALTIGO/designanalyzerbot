from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    value = int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "sim"}


def _ids(name: str) -> frozenset[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return frozenset()
    return frozenset(int(part.strip()) for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    bot_token: str | None
    database_path: Path
    work_dir: Path
    premium_dir: Path
    designsys_bin: str
    lighthouse_bin: str
    tech_fingerprints_path: Path
    analysis_timeout_seconds: int
    audit_timeout_seconds: int
    max_concurrent_analyses: int
    max_concurrent_premium: int
    max_result_mb: int
    capture_timeout_seconds: int
    max_concurrent_captures: int
    max_capture_assets: int
    max_capture_mb: int
    free_monthly_limit: int
    pro_monthly_limit: int
    agency_monthly_limit: int
    free_pages: int
    pro_pages: int
    agency_pages: int
    pro_clone_pages: int
    agency_clone_pages: int
    admin_ids: frozenset[int]
    analyzer_mock: bool
    openai_api_key: str | None
    openai_model: str
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        work_dir = Path(os.getenv("WORK_DIR", "data/jobs"))
        return cls(
            bot_token=os.getenv("BOT_TOKEN") or None,
            database_path=Path(os.getenv("DATABASE_PATH", "data/designanalyzer.db")),
            work_dir=work_dir,
            premium_dir=Path(os.getenv("PREMIUM_DIR", str(work_dir.parent / "premium"))),
            designsys_bin=os.getenv("DESIGNSYS_BIN", "designsys"),
            lighthouse_bin=os.getenv("LIGHTHOUSE_BIN", "lighthouse"),
            tech_fingerprints_path=Path(
                os.getenv("TECH_FINGERPRINTS_PATH", "/opt/webanalyze/technologies.json")
            ),
            analysis_timeout_seconds=_int("ANALYSIS_TIMEOUT_SECONDS", 900, 30),
            audit_timeout_seconds=_int("AUDIT_TIMEOUT_SECONDS", 420, 60),
            max_concurrent_analyses=_int("MAX_CONCURRENT_ANALYSES", 1, 1),
            max_concurrent_premium=_int("MAX_CONCURRENT_PREMIUM", 1, 1),
            max_result_mb=_int("MAX_RESULT_MB", 45, 1),
            capture_timeout_seconds=_int("CAPTURE_TIMEOUT_SECONDS", 300, 30),
            max_concurrent_captures=_int("MAX_CONCURRENT_CAPTURES", 1, 1),
            max_capture_assets=_int("MAX_CAPTURE_ASSETS", 300, 10),
            max_capture_mb=_int("MAX_CAPTURE_MB", 45, 5),
            free_monthly_limit=_int("FREE_MONTHLY_LIMIT", 1, 0),
            pro_monthly_limit=_int("PRO_MONTHLY_LIMIT", 10, 0),
            agency_monthly_limit=_int("AGENCY_MONTHLY_LIMIT", 100, 0),
            free_pages=_int("FREE_PAGES", 2, 1),
            pro_pages=_int("PRO_PAGES", 8, 1),
            agency_pages=_int("AGENCY_PAGES", 20, 1),
            pro_clone_pages=_int("PRO_CLONE_PAGES", 3, 1),
            agency_clone_pages=_int("AGENCY_CLONE_PAGES", 12, 1),
            admin_ids=_ids("ADMIN_IDS"),
            analyzer_mock=_bool("ANALYZER_MOCK", False),
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.6-luna"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )

    def plan_limit(self, plan: str) -> int:
        return {
            "free": self.free_monthly_limit,
            "pro": self.pro_monthly_limit,
            "agency": self.agency_monthly_limit,
        }.get(plan, self.free_monthly_limit)

    def plan_pages(self, plan: str) -> int:
        return {
            "free": self.free_pages,
            "pro": self.pro_pages,
            "agency": self.agency_pages,
        }.get(plan, self.free_pages)

    def clone_pages(self, plan: str) -> int:
        return {
            "free": 0,
            "pro": self.pro_clone_pages,
            "agency": self.agency_clone_pages,
        }.get(plan, 0)

    @property
    def ai_enabled(self) -> bool:
        return bool(self.openai_api_key)
