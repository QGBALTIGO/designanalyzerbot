import asyncio

import pytest

from app.analyzer import DesignAnalyzer
from app.config import Settings
from app.manager import AnalysisManager
from app.storage import Storage


@pytest.mark.asyncio
async def test_manager_processes_job_and_notifies(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYZER_MOCK", "1")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "jobs"))
    settings = Settings.from_env()
    store = Storage(settings.database_path)
    user = store.upsert_user(1, None, None)
    job = store.create_job_with_quota(telegram_user_id=1, chat_id=99, url="https://example.com", pages=2, limit=5)
    events = []

    async def notify(job, artifacts, error):
        events.append((job.status, bool(artifacts), error))

    manager = AnalysisManager(store, DesignAnalyzer(settings), 1, notify)
    await manager.start()
    manager.enqueue(job.id)
    await asyncio.wait_for(manager.queue.join(), timeout=2)
    await manager.stop()
    assert store.get_job(job.id).status == "completed"
    assert events == [("completed", True, None)]


@pytest.mark.asyncio
async def test_manager_does_not_duplicate_recovered_job(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYZER_MOCK", "1")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "jobs"))
    settings = Settings.from_env()
    store = Storage(settings.database_path)
    store.upsert_user(1, None, None)
    job = store.create_job_with_quota(telegram_user_id=1, chat_id=99, url="https://example.com", pages=2, limit=5)
    calls = 0

    async def notify(job, artifacts, error):
        nonlocal calls
        calls += 1

    manager = AnalysisManager(store, DesignAnalyzer(settings), 1, notify)
    await manager.start()  # recovered pending job gets queued here
    manager.enqueue(job.id)  # duplicate enqueue attempt must be ignored
    await asyncio.wait_for(manager.queue.join(), timeout=2)
    await manager.stop()
    assert calls == 1
