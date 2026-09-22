from pathlib import Path

import pytest

from app.storage import QuotaExceeded, Storage


def make_store(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "db.sqlite3")


def test_upsert_user_preserves_plan(tmp_path):
    store = make_store(tmp_path)
    user = store.upsert_user(10, "alice", "Alice")
    assert user.plan == "free"
    store.set_plan(10, "pro")
    store.upsert_user(10, "alice2", "Alice B")
    user2 = store.get_user(10)
    assert user2.plan == "pro"
    assert user2.username == "alice2"


def test_invalid_plan_rejected(tmp_path):
    store = make_store(tmp_path)
    store.upsert_user(1, None, None)
    with pytest.raises(ValueError):
        store.set_plan(1, "vip")


def test_quota_is_atomic_and_failed_job_releases_quota(tmp_path):
    store = make_store(tmp_path)
    store.upsert_user(1, None, None)
    first = store.create_job_with_quota(telegram_user_id=1, chat_id=1, url="https://example.com", pages=2, limit=1)
    assert store.usage_this_month(1) == 1
    with pytest.raises(QuotaExceeded):
        store.create_job_with_quota(telegram_user_id=1, chat_id=1, url="https://example.org", pages=2, limit=1)
    store.mark_failed(first.id, "boom")
    assert store.usage_this_month(1) == 0
    second = store.create_job_with_quota(telegram_user_id=1, chat_id=1, url="https://example.org", pages=2, limit=1)
    assert second.status == "queued"


def test_cancel_only_own_queued_job(tmp_path):
    store = make_store(tmp_path)
    store.upsert_user(1, None, None)
    store.upsert_user(2, None, None)
    job = store.create_job_with_quota(telegram_user_id=1, chat_id=1, url="https://example.com", pages=2, limit=5)
    assert not store.cancel_queued(job.id, 2)
    assert store.cancel_queued(job.id, 1)
    assert store.get_job(job.id).status == "cancelled"
    assert not store.cancel_queued(job.id, 1)


def test_pending_jobs_requeues_interrupted_running(tmp_path):
    store = make_store(tmp_path)
    store.upsert_user(1, None, None)
    job = store.create_job_with_quota(telegram_user_id=1, chat_id=1, url="https://example.com", pages=2, limit=5)
    store.mark_running(job.id)
    assert store.get_job(job.id).status == "running"
    pending = store.pending_jobs()
    assert [j.id for j in pending] == [job.id]
    assert store.get_job(job.id).status == "queued"
