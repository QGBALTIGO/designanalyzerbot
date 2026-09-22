import zipfile

import pytest

from app.analyzer import DesignAnalyzer
from app.config import Settings


def make_settings(tmp_path, monkeypatch, max_result_mb=45):
    monkeypatch.setenv("ANALYZER_MOCK", "1")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("MAX_RESULT_MB", str(max_result_mb))
    return Settings.from_env()


@pytest.mark.asyncio
async def test_mock_analysis_generates_expected_artifacts(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    result = await DesignAnalyzer(settings).analyze(7, "example.com", 2, "free")
    assert result.output_dir.name == "job-7"
    assert result.pdf and result.pdf.exists()
    assert result.bundle and result.bundle.exists()
    assert "Análise concluída" in result.summary
    with zipfile.ZipFile(result.bundle) as zf:
        names = set(zf.namelist())
    assert "tokens.json" in names
    assert "variables.css" in names
    assert "raw.json" in names
    assert "design-system.pdf" in names


@pytest.mark.asyncio
async def test_rerun_replaces_old_job_directory(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    analyzer = DesignAnalyzer(settings)
    first = await analyzer.analyze(1, "example.com", 2, "free")
    stale = first.output_dir / "stale.txt"
    stale.write_text("old")
    second = await analyzer.analyze(1, "example.org", 2, "pro")
    assert not stale.exists()
    assert second.pdf is not None
