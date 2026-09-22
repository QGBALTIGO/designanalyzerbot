from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from app.config import Settings
from app.webclone import WebsiteCapture


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        os.environ["DATABASE_PATH"] = str(root / "db.sqlite3")
        os.environ["WORK_DIR"] = str(root / "jobs")
        os.environ["MAX_CAPTURE_ASSETS"] = "40"
        os.environ["MAX_CAPTURE_MB"] = "20"
        os.environ["CAPTURE_TIMEOUT_SECONDS"] = "120"

        settings = Settings.from_env()
        result = await WebsiteCapture(settings).capture(
            "smoke",
            "https://example.com/",
            "clone",
        )

        index = result.output_dir / "index.html"
        assert index.exists()
        assert result.bundle and result.bundle.exists()
        assert result.manifest.exists()
        assert result.original_screenshot and result.original_screenshot.exists()
        assert result.clone_screenshot and result.clone_screenshot.exists()

        html = index.read_text(encoding="utf-8").lower()
        assert "<script" not in html
        assert "noindex,nofollow" in html
        print(
            "CAPTURE_SMOKE_OK",
            result.asset_count,
            result.bundle.name,
            f"similarity={result.similarity}",
        )


if __name__ == "__main__":
    asyncio.run(main())
