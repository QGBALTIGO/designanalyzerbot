import os

import pytest

from app.config import Settings


def test_defaults(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith(("FREE_", "PRO_", "AGENCY_", "ANALYSIS_", "MAX_", "BOT_TOKEN", "ADMIN_IDS", "WORK_DIR", "DATABASE_PATH", "ANALYZER_MOCK")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "jobs"))
    settings = Settings.from_env()
    assert settings.bot_token is None
    assert settings.free_monthly_limit == 1
    assert settings.pro_monthly_limit == 10
    assert settings.plan_pages("agency") == 20
    assert settings.max_concurrent_analyses == 1
    assert settings.max_concurrent_premium == 1
    assert settings.pro_clone_pages == 3
    assert settings.agency_clone_pages == 12
    assert settings.openai_model == "gpt-5.6-luna"
    assert settings.ai_enabled is False


def test_env_parsing(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("BOT_TOKEN", "abc")
    monkeypatch.setenv("ADMIN_IDS", "1, 2,3")
    monkeypatch.setenv("ANALYZER_MOCK", "sim")
    monkeypatch.setenv("FREE_MONTHLY_LIMIT", "4")
    settings = Settings.from_env()
    assert settings.bot_token == "abc"
    assert settings.admin_ids == frozenset({1, 2, 3})
    assert settings.analyzer_mock is True
    assert settings.free_monthly_limit == 4
    assert settings.clone_pages("pro") == 3
    assert settings.clone_pages("agency") == 12


def test_bad_numeric_setting(monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_ANALYSES", "0")
    with pytest.raises(ValueError):
        Settings.from_env()
