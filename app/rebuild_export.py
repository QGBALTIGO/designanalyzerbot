
from __future__ import annotations

import json
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from .analyzer import ProgressUpdate
from .config import Settings
from .rebuild_common import (
    build_next_export,
    build_react_export,
    build_tailwind_export,
    collect_css,
    extract_colors,
    extract_fonts,
    zip_dir,
)
from .security import validate_public_url
from .webclone import WebsiteCapture


ProgressCallback = Callable[[ProgressUpdate], Awaitable[None]]


@dataclass(frozen=True)
class RebuildArtifacts:
    source_url: str
    output_dir: Path
    bundle: Path | None
    preview: Path | None
    formats: tuple[str, ...]
    colors: tuple[str, ...]
    fonts: tuple[str, ...]
    summary: str


class RebuildExporter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.capture = WebsiteCapture(settings)
        self.settings.premium_dir.mkdir(parents=True, exist_ok=True)

    async def export(
        self,
        export_id: str,
        raw_url: str,
        *,
        progress: ProgressCallback | None = None,
    ) -> RebuildArtifacts:
        started = time.monotonic()
        validated = await validate_public_url(raw_url)

        async def cap_progress(update: ProgressUpdate) -> None:
            await self._emit(
                progress,
                ProgressUpdate(
                    min(55, 2 + int(update.percent * 0.53)),
                    update.stage,
                    int(time.monotonic() - started),
                    None,
                    update.detail,
                ),
            )

        capture = await self.capture.capture(
            f"{export_id}-rebuild",
            validated.url,
            "clone",
            progress=cap_progress,
        )
        source_dir = capture.output_dir
        index_path = source_dir / "index.html"
        if not index_path.exists():
            raise RuntimeError("clone base não gerou index.html")

        out = self.settings.premium_dir / f"rebuild-{export_id}"
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)

        await self._emit(
            progress,
            ProgressUpdate(60, "Extraindo tokens e estrutura", int(time.monotonic() - started), 40),
        )
        html_text = index_path.read_text(encoding="utf-8")
        css_text = collect_css(source_dir)
        colors = tuple(extract_colors(css_text))
        fonts = tuple(extract_fonts(css_text))
        (out / "tokens.json").write_text(
            json.dumps(
                {"colors": list(colors), "fonts": list(fonts), "source": validated.url},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        await self._emit(
            progress,
            ProgressUpdate(67, "Gerando HTML/CSS limpo", int(time.monotonic() - started), 30),
        )
        html_css = out / "html-css"
        shutil.copytree(source_dir, html_css, dirs_exist_ok=True)
        for nested_zip in html_css.rglob("*.zip"):
            nested_zip.unlink(missing_ok=True)

        await self._emit(
            progress,
            ProgressUpdate(74, "Gerando React/Vite", int(time.monotonic() - started), 25),
        )
        build_react_export(out / "react-vite", source_dir, html_text, css_text)

        await self._emit(
            progress,
            ProgressUpdate(81, "Gerando Next.js", int(time.monotonic() - started), 20),
        )
        build_next_export(out / "nextjs", source_dir, html_text, css_text)

        await self._emit(
            progress,
            ProgressUpdate(87, "Gerando scaffold Tailwind", int(time.monotonic() - started), 15),
        )
        build_tailwind_export(out / "tailwind", source_dir, html_text, css_text, colors, fonts)

        report = {
            "source_url": validated.url,
            "formats": ["html-css", "react-vite", "nextjs", "tailwind"],
            "colors": list(colors),
            "fonts": list(fonts),
            "asset_count": capture.asset_count,
            "similarity_base_clone": capture.similarity,
        }
        (out / "rebuild.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (out / "README.md").write_text(
            "# Design Analyzer Rebuild\n\n"
            f"Origem: {validated.url}\n\n"
            "html-css: snapshot offline editável\n"
            "react-vite: projeto React com assets\n"
            "nextjs: projeto Next.js App Router com assets\n"
            "tailwind: scaffold Tailwind para migração gradual\n\n"
            "Scripts e formulários do site original permanecem desativados.\n",
            encoding="utf-8",
        )

        preview = capture.clone_screenshot
        if preview and preview.exists():
            shutil.copy2(preview, out / "preview.png")
            preview = out / "preview.png"

        await self._emit(
            progress,
            ProgressUpdate(94, "Compactando exports", int(time.monotonic() - started), 10),
        )
        bundle = zip_dir(out, "rebuild-export.zip", self.settings.max_result_mb)
        await self._emit(
            progress,
            ProgressUpdate(100, "Reconstrução concluída", int(time.monotonic() - started), 0),
        )

        return RebuildArtifacts(
            source_url=validated.url,
            output_dir=out,
            bundle=bundle,
            preview=preview,
            formats=("HTML/CSS", "React/Vite", "Next.js", "Tailwind"),
            colors=colors,
            fonts=fonts,
            summary=(
                "🧱 Reconstrução concluída\n"
                "📦 HTML/CSS + React/Vite + Next.js + Tailwind\n"
                f"🎨 Cores extraídas: {len(colors)}\n"
                f"🔤 Fontes detectadas: {len(fonts)}"
            ),
        )

    async def _emit(
        self,
        callback: ProgressCallback | None,
        update: ProgressUpdate,
    ) -> None:
        if callback is None:
            return
        try:
            await callback(update)
        except Exception:
            pass
