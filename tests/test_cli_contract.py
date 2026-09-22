import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.analyzer import DesignAnalyzer
from app.config import Settings


class FakeProcess:
    def __init__(self, output_dir):
        self.pid = 43210
        self.returncode = 0
        self.output_dir = output_dir

    async def communicate(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "raw.json").write_text("{}")
        return b"ok", b""


@pytest.mark.asyncio
async def test_designsys_command_free_uses_no_assets(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYZER_MOCK", "0")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "jobs"))
    settings = Settings.from_env()
    analyzer = DesignAnalyzer(settings)
    out = settings.work_dir / "job-1"
    captured = []

    async def fake_exec(*args, **kwargs):
        captured.extend(args)
        return FakeProcess(out)

    with patch("app.analyzer.validate_public_url", new=AsyncMock()) as validate:
        validate.return_value.url = "https://example.com/"
        with patch("app.analyzer.asyncio.create_subprocess_exec", side_effect=fake_exec):
            await analyzer.analyze(1, "https://example.com", 2, "free")
    assert captured[:3] == ["designsys", "url", "https://example.com/"]
    assert "--pages" in captured and "2" in captured
    assert "--no-assets" in captured
    assert "--exhaustive" not in captured


@pytest.mark.asyncio
async def test_designsys_command_pro_uses_exhaustive(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYZER_MOCK", "0")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "jobs"))
    settings = Settings.from_env()
    analyzer = DesignAnalyzer(settings)
    out = settings.work_dir / "job-2"
    captured = []

    async def fake_exec(*args, **kwargs):
        captured.extend(args)
        return FakeProcess(out)

    with patch("app.analyzer.validate_public_url", new=AsyncMock()) as validate:
        validate.return_value.url = "https://example.com/"
        with patch("app.analyzer.asyncio.create_subprocess_exec", side_effect=fake_exec):
            await analyzer.analyze(2, "https://example.com", 8, "pro")
    assert "--exhaustive" in captured
    assert "--no-assets" not in captured
