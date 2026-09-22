from pathlib import Path

import pytest
from PIL import Image

pytest.importorskip("telegram")

from app.bot import _make_telegram_preview, create_application
from app.config import Settings


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        bot_token="123456:ABCDEF_fake_token_for_tests",
        database_path=tmp_path / "bot.db",
        work_dir=tmp_path / "jobs",
        designsys_bin="designsys",
        analysis_timeout_seconds=60,
        max_concurrent_analyses=1,
        max_result_mb=45,
        capture_timeout_seconds=300,
        max_concurrent_captures=1,
        max_capture_assets=300,
        max_capture_mb=45,
        free_monthly_limit=1,
        pro_monthly_limit=10,
        agency_monthly_limit=100,
        free_pages=2,
        pro_pages=8,
        agency_pages=20,
        admin_ids=frozenset({123}),
        analyzer_mock=True,
        log_level="INFO",
    )


def test_create_application_registers_lifecycle_and_handlers(tmp_path: Path) -> None:
    app = create_application(make_settings(tmp_path))
    assert app.post_init is not None
    assert app.post_shutdown is not None
    assert app.bot_data["manager"].workers == 1
    assert sum(len(group) for group in app.handlers.values()) >= 10


def test_make_telegram_preview_limits_tall_screenshot(tmp_path: Path) -> None:
    source = tmp_path / "full.png"
    Image.new("RGB", (1440, 12000), (120, 130, 140)).save(source)
    preview = _make_telegram_preview(source)
    assert preview.exists()
    with Image.open(preview) as image:
        assert image.width <= 1280
        assert image.height <= 1800
        assert image.width >= 10
        assert image.height >= 10
