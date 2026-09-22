
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path

from app.config import Settings
from app.premium_audit import FullSiteAudit
from app.rebuild_export import RebuildExporter
from app.redesign import InspiredRebuilder
from app.site_clone import SingleFileExporter
from app.tech_fingerprint import TechnologyDetector


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        os.environ["DATABASE_PATH"] = str(root / "db.sqlite3")
        os.environ["WORK_DIR"] = str(root / "jobs")
        os.environ["PREMIUM_DIR"] = str(root / "premium")
        os.environ["MAX_CAPTURE_ASSETS"] = "50"
        os.environ["MAX_CAPTURE_MB"] = "20"
        os.environ["CAPTURE_TIMEOUT_SECONDS"] = "150"
        os.environ["AUDIT_TIMEOUT_SECONDS"] = "240"
        os.environ.pop("OPENAI_API_KEY", None)

        settings = Settings.from_env()

        version = subprocess.run(
            [settings.lighthouse_bin, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        assert version

        detector = TechnologyDetector(settings.tech_fingerprints_path)
        assert detector.available()

        audit = await FullSiteAudit(settings).run(
            "smoke",
            "https://example.com/",
        )
        assert audit.report_json.exists()
        assert audit.report_html.exists()
        assert audit.screenshot.exists()
        assert audit.pdf.exists()
        assert audit.markdown.exists()
        assert audit.warc.exists()
        audit_json = json.loads(audit.report_json.read_text(encoding="utf-8"))
        assert "scores" in audit_json
        assert "security" in audit_json
        assert "accessibility" in audit_json

        single = await SingleFileExporter(settings).export(
            "smoke",
            "https://example.com/",
        )
        assert single.html.exists()
        assert "<html" in single.html.read_text(encoding="utf-8").lower()

        rebuild = await RebuildExporter(settings).export(
            "smoke",
            "https://example.com/",
        )
        assert (rebuild.output_dir / "html-css" / "index.html").exists()
        assert (rebuild.output_dir / "react-vite" / "src" / "App.jsx").exists()
        assert (rebuild.output_dir / "nextjs" / "app" / "page.jsx").exists()
        assert (rebuild.output_dir / "tailwind" / "tailwind.config.js").exists()

        inspired = await InspiredRebuilder(settings).build(
            "smoke",
            "https://example.com/",
            mode="modernize",
        )
        assert inspired.html.exists()
        assert inspired.used_ai is False
        assert "<script" not in inspired.html.read_text(encoding="utf-8").lower()

        print(
            "PREMIUM_SMOKE_OK",
            f"lighthouse={version}",
            f"seo={audit.score_seo}",
            f"a11y={audit.score_accessibility}",
            f"security={audit.score_security}",
            f"performance={audit.score_performance}",
        )


if __name__ == "__main__":
    asyncio.run(main())
