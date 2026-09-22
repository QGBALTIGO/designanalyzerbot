from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .security import UnsafeUrl, normalize_url, validate_public_url


@dataclass(frozen=True)
class AnalysisArtifacts:
    output_dir: Path
    pdf: Path | None
    bundle: Path | None
    summary: str


class AnalysisError(RuntimeError):
    pass


class DesignAnalyzer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.work_dir.mkdir(parents=True, exist_ok=True)

    async def analyze(self, job_id: int, raw_url: str, pages: int, plan: str) -> AnalysisArtifacts:
        if self.settings.analyzer_mock:
            url = normalize_url(raw_url)
        else:
            validated = await validate_public_url(raw_url)
            url = validated.url
            # Resolve uma segunda vez imediatamente antes de entregar o endereço ao Chromium.
            await validate_public_url(url)

        output_dir = self.settings.work_dir / f"job-{job_id}"
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if self.settings.analyzer_mock:
            self._create_mock_result(output_dir, url, pages, plan)
        else:
            await self._run_designsys(output_dir, url, pages, plan)

        pdf = output_dir / "design-system.pdf"
        if not pdf.exists():
            pdf = None
        bundle = self._create_bundle(output_dir)
        summary = self._build_summary(output_dir, url)
        return AnalysisArtifacts(output_dir, pdf, bundle, summary)

    async def _run_designsys(self, output_dir: Path, url: str, pages: int, plan: str) -> None:
        args = [
            self.settings.designsys_bin,
            "url",
            url,
            "--pages",
            str(pages),
            "--out",
            str(output_dir),
        ]
        if plan == "free":
            args.append("--no-assets")
        else:
            args.append("--exhaustive")

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise AnalysisError("O executável 'designsys' não está instalado no servidor.") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.settings.analysis_timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            raise AnalysisError("A análise excedeu o tempo máximo permitido.") from exc

        if proc.returncode != 0:
            err = (stderr or stdout).decode("utf-8", errors="replace").strip()
            tail = err[-1600:] if err else f"designsys terminou com código {proc.returncode}"
            raise AnalysisError(tail)
        if not (output_dir / "raw.json").exists():
            raise AnalysisError("O designsys terminou sem gerar raw.json; análise incompleta.")

    def _create_bundle(self, output_dir: Path) -> Path | None:
        preferred = [
            "design-system.pdf",
            "DESIGN-SYSTEM.md",
            "tokens.json",
            "variables.css",
            "tailwind.config.js",
            "components.json",
            "style-guide.html",
            "tokens.studio.json",
            "README.md",
            "raw.json",
        ]
        existing = [output_dir / name for name in preferred if (output_dir / name).is_file()]
        if not existing:
            return None
        bundle = output_dir / "design-analysis.zip"
        with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for path in existing:
                zf.write(path, path.name)
        max_bytes = self.settings.max_result_mb * 1024 * 1024
        if bundle.stat().st_size > max_bytes:
            bundle.unlink(missing_ok=True)
            return None
        return bundle

    def _build_summary(self, output_dir: Path, url: str) -> str:
        raw_path = output_dir / "raw.json"
        try:
            data = json.loads(raw_path.read_text(encoding="utf-8"))
        except Exception:
            return f"✅ Análise concluída\n🌐 {url}"
        name = data.get("name") or data.get("target") or url
        pages = data.get("pages") or []
        colors = data.get("colors") or {}
        typography = data.get("typography") or {}
        components = data.get("components") or []
        families = typography.get("families") if isinstance(typography, dict) else []
        role_count = len((colors.get("roles") or {})) if isinstance(colors, dict) else 0
        return (
            "✅ Análise concluída\n"
            f"🌐 {name}\n"
            f"📄 Páginas lidas: {len(pages)}\n"
            f"🎨 Cores semânticas: {role_count}\n"
            f"🔤 Famílias tipográficas: {len(families or [])}\n"
            f"🧩 Componentes detectados: {len(components)}"
        )

    def _create_mock_result(self, output_dir: Path, url: str, pages: int, plan: str) -> None:
        payload = {
            "name": "mock.example",
            "target": url,
            "mode": "url",
            "pages": [{"url": url, "elements": 42, "rules": 120} for _ in range(min(pages + 1, 3))],
            "colors": {"roles": {"primary": {"value": "#7c6cff"}}},
            "typography": {"families": [{"family": "Inter", "count": 10}]},
            "components": [{"name": "button"}, {"name": "card"}],
        }
        (output_dir / "raw.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        (output_dir / "tokens.json").write_text('{"color": {}}\n', encoding="utf-8")
        (output_dir / "variables.css").write_text(":root { --color-primary: #7c6cff; }\n", encoding="utf-8")
        (output_dir / "tailwind.config.js").write_text("module.exports = { theme: { extend: {} } };\n", encoding="utf-8")
        (output_dir / "components.json").write_text('{"components": []}\n', encoding="utf-8")
        (output_dir / "tokens.studio.json").write_text("{}\n", encoding="utf-8")
        (output_dir / "DESIGN-SYSTEM.md").write_text(f"# Mock design system\n\n{url}\n", encoding="utf-8")
        (output_dir / "README.md").write_text(f"# Mock analysis\n\nPlan: {plan}\n", encoding="utf-8")
        (output_dir / "style-guide.html").write_text("<html><body>mock</body></html>\n", encoding="utf-8")
        (output_dir / "design-system.pdf").write_bytes(b"%PDF-1.4\n% mock test artifact\n")
