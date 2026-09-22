
from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from openai import AsyncOpenAI
from playwright.async_api import async_playwright

from .analyzer import ProgressUpdate
from .config import Settings
from .rebuild_common import (
    collect_css,
    content_brief,
    deterministic_redesign,
    sanitize_generated_html,
)
from .security import validate_public_url
from .webclone import WebsiteCapture


ProgressCallback = Callable[[ProgressUpdate], Awaitable[None]]


@dataclass(frozen=True)
class InspiredArtifacts:
    source_url: str
    output_dir: Path
    html: Path
    preview: Path | None
    used_ai: bool
    summary: str


class InspiredRebuilder:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.capture = WebsiteCapture(settings)
        self.settings.premium_dir.mkdir(parents=True, exist_ok=True)

    async def build(
        self,
        build_id: str,
        raw_url: str,
        *,
        mode: str = "modernize",
        progress: ProgressCallback | None = None,
    ) -> InspiredArtifacts:
        if mode not in {"modernize", "inspire"}:
            raise ValueError(f"modo inválido: {mode}")

        started = time.monotonic()
        validated = await validate_public_url(raw_url)

        async def cap_progress(update: ProgressUpdate) -> None:
            await self._emit(
                progress,
                ProgressUpdate(
                    min(52, 2 + int(update.percent * 0.50)),
                    update.stage,
                    int(time.monotonic() - started),
                    None,
                    update.detail,
                ),
            )

        capture = await self.capture.capture(
            f"{build_id}-inspire",
            validated.url,
            "clone",
            progress=cap_progress,
        )
        index = capture.output_dir / "index.html"
        if not index.exists():
            raise RuntimeError("clone base não gerou index.html")

        source_html = index.read_text(encoding="utf-8")
        css = collect_css(capture.output_dir)
        brief = content_brief(source_html, css, validated.url)

        await self._emit(
            progress,
            ProgressUpdate(58, "Planejando nova composição", int(time.monotonic() - started), 35),
        )
        used_ai = False
        rebuilt_html: str | None = None
        if self.settings.openai_api_key:
            try:
                rebuilt_html = await _ai_rebuild(self.settings, brief, mode)
                rebuilt_html = sanitize_generated_html(rebuilt_html)
                used_ai = True
            except Exception:
                rebuilt_html = None

        if not rebuilt_html:
            rebuilt_html = deterministic_redesign(brief, mode)

        out = self.settings.premium_dir / f"inspired-{build_id}"
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)

        html_path = out / "index.html"
        html_path.write_text(rebuilt_html, encoding="utf-8")
        (out / "brief.json").write_text(
            json.dumps(brief, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        await self._emit(
            progress,
            ProgressUpdate(83, "Renderizando nova versão", int(time.monotonic() - started), 15),
        )
        preview = out / "preview.png"
        try:
            await _render_local(html_path, preview)
        except Exception:
            preview = None

        (out / "README.md").write_text(
            "# Design Analyzer Inspired Rebuild\n\n"
            f"Origem: {validated.url}\n"
            f"Modo: {mode}\n"
            f"IA utilizada: {'sim' if used_ai else 'não; fallback local'}\n\n"
            "A saída é uma composição nova baseada em conteúdo e sinais visuais da referência.\n",
            encoding="utf-8",
        )

        await self._emit(
            progress,
            ProgressUpdate(100, "Nova versão concluída", int(time.monotonic() - started), 0),
        )
        return InspiredArtifacts(
            source_url=validated.url,
            output_dir=out,
            html=html_path,
            preview=preview,
            used_ai=used_ai,
            summary=(
                f"✨ {'Modernização' if mode == 'modernize' else 'Inspiração'} concluída\n"
                f"🤖 IA: {'GPT via API' if used_ai else 'gerador local'}\n"
                "📄 Saída: HTML responsivo editável"
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


async def _ai_rebuild(settings: Settings, brief: dict, mode: str) -> str:
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    prompt = (
        "Crie uma única página HTML responsiva e profissional em pt-BR, sem JavaScript, "
        "sem formulários, sem login e sem checkout. Preserve o conteúdo factual fornecido, "
        "mas produza uma composição nova e editável. "
        f"Modo: {mode}. Retorne somente HTML completo com CSS dentro de style.\n\n"
        "Brief JSON:\n"
        + json.dumps(brief, ensure_ascii=False)[:30000]
    )
    response = await client.responses.create(
        model=settings.openai_model,
        input=prompt,
        store=False,
        reasoning={"effort": "low"},
        max_output_tokens=12000,
    )
    text = response.output_text.strip()
    fence = chr(96) * 3
    if text.startswith(fence + "html"):
        text = text[len(fence + "html"):].lstrip()
    elif text.startswith(fence):
        text = text[len(fence):].lstrip()
    if text.endswith(fence):
        text = text[:-len(fence)].rstrip()
    if "<html" not in text.lower():
        raise RuntimeError("modelo não retornou HTML completo")
    return text


async def _render_local(html_path: Path, screenshot: Path) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        await page.goto(
            html_path.resolve().as_uri(),
            wait_until="load",
            timeout=30_000,
        )
        await page.screenshot(path=str(screenshot), full_page=True)
        await browser.close()
