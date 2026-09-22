from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from app.analyzer import DesignAnalyzer
from app.config import Settings
from app.storage import Storage


async def run() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        os.environ["ANALYZER_MOCK"] = "1"
        os.environ["DATABASE_PATH"] = str(root / "db.sqlite3")
        os.environ["WORK_DIR"] = str(root / "jobs")
        settings = Settings.from_env()
        storage = Storage(settings.database_path)
        user = storage.upsert_user(123, "smoke", "Smoke")
        job = storage.create_job_with_quota(
            telegram_user_id=user.telegram_user_id,
            chat_id=123,
            url="https://example.com/",
            pages=settings.plan_pages(user.plan),
            limit=settings.plan_limit(user.plan),
        )
        storage.mark_running(job.id)
        artifacts = await DesignAnalyzer(settings).analyze(job.id, job.url, job.pages, job.plan)
        storage.mark_completed(job.id, str(artifacts.output_dir))
        assert artifacts.pdf and artifacts.pdf.exists()
        assert artifacts.bundle and artifacts.bundle.exists()
        assert storage.get_job(job.id).status == "completed"
        print("SMOKE_OK", job.id, artifacts.bundle.name)


if __name__ == "__main__":
    asyncio.run(run())
