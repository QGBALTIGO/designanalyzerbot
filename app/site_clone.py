from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import time
import zipfile
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit
from xml.sax.saxutils import escape as xml_escape

from bs4 import BeautifulSoup
from markdownify import markdownify as to_markdown
from playwright.async_api import async_playwright

from .analyzer import ProgressUpdate
from .config import Settings
from .security import validate_public_url
from .webclone import CaptureArtifacts, WebsiteCapture


ProgressCallback = Callable[[ProgressUpdate], Awaitable[None]]
_CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)


@dataclass(frozen=True)
class MultiPageArtifacts:
    source_url: str
    output_dir: Path
    bundle: Path | None
    page_count: int
    asset_count: int
    total_bytes: int
    average_similarity: float | None
    sitemap: Path
    page_map: Path
    summary: str


@dataclass(frozen=True)
class SingleFileArtifacts:
    source_url: str
    output_dir: Path
    html: Path
    screenshot: Path | None
    size_bytes: int
    summary: str


class MultiPageCloner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.capture = WebsiteCapture(settings)
        self.settings.premium_dir.mkdir(parents=True, exist_ok=True)

    async def _emit(self, callback: ProgressCallback | None, update: ProgressUpdate) -> None:
        if callback is not None:
            try:
                await callback(update)
            except Exception:
                pass

    async def clone(
        self,
        clone_id: str,
        raw_url: str,
        *,
        max_pages: int,
        progress: ProgressCallback | None = None,
    ) -> MultiPageArtifacts:
        started = time.monotonic()
        validated = await validate_public_url(raw_url)
        root_url = validated.url
        max_pages = max(1, min(30, int(max_pages)))

        out = self.settings.premium_dir / f"site-clone-{clone_id}"
        temp = out / "_captures"
        if out.exists():
            shutil.rmtree(out)
        temp.mkdir(parents=True, exist_ok=True)

        await self._emit(progress, ProgressUpdate(3, "Descobrindo páginas internas", 0, None))
        urls = await self._discover_pages(root_url, max_pages, progress, started)

        await self._emit(
            progress,
            ProgressUpdate(15, "Iniciando clone multipágina", int(time.monotonic() - started), None, f"{len(urls)} páginas"),
        )

        page_entries: list[dict] = []
        total_assets = 0
        total_bytes = 0
        similarities: list[float] = []

        for index, page_url in enumerate(urls, start=1):
            base_percent = 15 + int(66 * ((index - 1) / max(1, len(urls))))
            await self._emit(
                progress,
                ProgressUpdate(
                    base_percent,
                    f"Clonando página {index}/{len(urls)}",
                    int(time.monotonic() - started),
                    None,
                    urlsplit(page_url).path or "/",
                ),
            )

            async def page_progress(update: ProgressUpdate) -> None:
                # Compress each single-page progress range into this page's share.
                slice_size = 66 / max(1, len(urls))
                mapped = min(80, int(15 + (index - 1) * slice_size + (update.percent / 100) * slice_size))
                await self._emit(
                    progress,
                    ProgressUpdate(mapped, f"Página {index}/{len(urls)} · {update.stage}", int(time.monotonic() - started), None, update.detail),
                )

            capture = await self.capture.capture(
                f"{clone_id}-p{index}",
                page_url,
                "clone",
                progress=page_progress,
            )
            rel_dir = Path(".") if index == 1 else Path("pages") / f"p{index}"
            target_dir = out / rel_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            _copy_capture(capture.output_dir, target_dir)

            html_path = target_dir / "index.html"
            if not html_path.exists():
                raise RuntimeError(f"clone da página {page_url} não gerou index.html")

            markdown_dir = out / "markdown"
            markdown_dir.mkdir(exist_ok=True)
            markdown_path = markdown_dir / f"page-{index}.md"
            markdown_path.write_text(_page_markdown(page_url, html_path), encoding="utf-8")

            total_assets += capture.asset_count
            total_bytes += capture.total_bytes
            if capture.similarity is not None:
                similarities.append(float(capture.similarity))

            page_entries.append(
                {
                    "index": index,
                    "url": page_url,
                    "relative_dir": rel_dir.as_posix(),
                    "html": (rel_dir / "index.html").as_posix() if str(rel_dir) != "." else "index.html",
                    "markdown": markdown_path.relative_to(out).as_posix(),
                    "asset_count": capture.asset_count,
                    "bytes": capture.total_bytes,
                    "similarity": capture.similarity,
                    "technologies": list(capture.technologies),
                }
            )

        await self._emit(progress, ProgressUpdate(83, "Reescrevendo links internos", int(time.monotonic() - started), 20))
        self._rewrite_internal_links(out, page_entries)

        sitemap = out / "sitemap.xml"
        sitemap.write_text(_sitemap_xml([entry["url"] for entry in page_entries]), encoding="utf-8")
        page_map = out / "site-map.json"
        page_map.write_text(
            json.dumps(
                {
                    "root": root_url,
                    "pages": page_entries,
                    "asset_count": total_assets,
                    "total_bytes": total_bytes,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        (out / "README.txt").write_text(
            "Design Analyzer Bot - clone multipágina offline\n"
            f"Origem: {root_url}\n"
            f"Páginas: {len(page_entries)}\n\n"
            "Abra index.html. Scripts, formulários, iframes, login e checkout ficam desativados.\n",
            encoding="utf-8",
        )

        shutil.rmtree(temp, ignore_errors=True)
        await self._emit(progress, ProgressUpdate(92, "Compactando site", int(time.monotonic() - started), 10))
        bundle = _zip_dir(out, "multipage-clone.zip", self.settings.max_result_mb)

        avg = round(sum(similarities) / len(similarities), 2) if similarities else None
        summary = (
            "🕷 Clone multipágina concluído\n"
            f"📄 Páginas: {len(page_entries)}\n"
            f"📦 Assets: {total_assets}\n"
            f"💾 Assets baixados: {_human_bytes(total_bytes)}\n"
            f"🎯 Similaridade média: {f'{avg:.1f}%' if avg is not None else '—'}"
        )
        await self._emit(progress, ProgressUpdate(100, "Clone multipágina concluído", int(time.monotonic() - started), 0))
        return MultiPageArtifacts(
            source_url=root_url,
            output_dir=out,
            bundle=bundle,
            page_count=len(page_entries),
            asset_count=total_assets,
            total_bytes=total_bytes,
            average_similarity=avg,
            sitemap=sitemap,
            page_map=page_map,
            summary=summary,
        )

    async def _discover_pages(
        self,
        root_url: str,
        max_pages: int,
        progress: ProgressCallback | None,
        started: float,
    ) -> list[str]:
        root_host = (urlsplit(root_url).hostname or "").lower()
        queue: deque[str] = deque([root_url])
        seen: set[str] = set()
        ordered: list[str] = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1280, "height": 800}, service_workers="block")

            async def guard_route(route) -> None:
                request_url = route.request.url
                scheme = urlsplit(request_url).scheme.lower()
                if scheme in {"data", "blob", "about"}:
                    await route.continue_()
                    return
                if scheme not in {"http", "https"}:
                    await route.abort()
                    return
                try:
                    await validate_public_url(request_url)
                except Exception:
                    await route.abort()
                    return
                await route.continue_()

            await context.route("**/*", guard_route)
            page = await context.new_page()
            while queue and len(ordered) < max_pages:
                current = queue.popleft()
                current, _ = urldefrag(current)
                if current in seen:
                    continue
                seen.add(current)
                try:
                    await validate_public_url(current)
                    await page.goto(current, wait_until="domcontentloaded", timeout=45_000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5_000)
                    except Exception:
                        pass
                    final_url, _ = urldefrag(page.url)
                    if (urlsplit(final_url).hostname or "").lower() != root_host:
                        continue
                    ordered.append(final_url)
                    links = await page.eval_on_selector_all(
                        "a[href]",
                        "(els) => els.map(a => a.href)",
                    )
                    for link in links:
                        clean, _ = urldefrag(str(link))
                        parsed = urlsplit(clean)
                        if (
                            parsed.scheme in {"http", "https"}
                            and (parsed.hostname or "").lower() == root_host
                            and clean not in seen
                            and _page_candidate(parsed.path)
                        ):
                            queue.append(clean)
                except Exception:
                    if current == root_url and not ordered:
                        raise
                    continue

                await self._emit(
                    progress,
                    ProgressUpdate(
                        min(13, 3 + len(ordered)),
                        "Descobrindo páginas internas",
                        int(time.monotonic() - started),
                        None,
                        f"{len(ordered)}/{max_pages} encontradas",
                    ),
                )
            await context.close()
            await browser.close()

        return ordered or [root_url]

    def _rewrite_internal_links(self, root: Path, entries: list[dict]) -> None:
        normalized = {urldefrag(item["url"])[0]: item for item in entries}
        for entry in entries:
            html_path = root / entry["html"]
            soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
            current_dir = html_path.parent
            for anchor in soup.find_all("a", href=True):
                href = str(anchor.get("href") or "")
                absolute, fragment = urldefrag(urljoin(entry["url"], href))
                target = normalized.get(absolute)
                if target is None:
                    continue
                target_path = root / target["html"]
                rel = Path(os.path.relpath(target_path, start=current_dir)).as_posix()
                anchor["href"] = rel + (f"#{fragment}" if fragment else "")
            html_path.write_text("<!doctype html>\n" + str(soup), encoding="utf-8")


class SingleFileExporter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.capture = WebsiteCapture(settings)

    async def export(
        self,
        export_id: str,
        raw_url: str,
        *,
        progress: ProgressCallback | None = None,
    ) -> SingleFileArtifacts:
        started = time.monotonic()
        validated = await validate_public_url(raw_url)
        await self._emit(progress, ProgressUpdate(3, "Capturando página e assets", 0, None))

        async def capture_progress(update: ProgressUpdate) -> None:
            mapped = min(70, 3 + int(update.percent * 0.67))
            await self._emit(
                progress,
                ProgressUpdate(mapped, update.stage, int(time.monotonic() - started), None, update.detail),
            )

        capture = await self.capture.capture(export_id, validated.url, "clone", progress=capture_progress)
        out = capture.output_dir
        index_path = out / "index.html"
        if not index_path.exists():
            raise RuntimeError("clone não gerou index.html")

        await self._emit(progress, ProgressUpdate(75, "Embutindo imagens, fontes e CSS", int(time.monotonic() - started), 20))
        standalone = _make_single_file(index_path, out)
        result_path = out / "single-file.html"
        result_path.write_text(standalone, encoding="utf-8")

        max_bytes = self.settings.max_result_mb * 1024 * 1024
        if result_path.stat().st_size > max_bytes:
            raise RuntimeError(
                f"HTML único ficou com {_human_bytes(result_path.stat().st_size)}, acima do limite de {self.settings.max_result_mb} MB"
            )

        await self._emit(progress, ProgressUpdate(100, "HTML único concluído", int(time.monotonic() - started), 0))
        return SingleFileArtifacts(
            source_url=validated.url,
            output_dir=out,
            html=result_path,
            screenshot=capture.clone_screenshot,
            size_bytes=result_path.stat().st_size,
            summary=(
                "📄 HTML único concluído\n"
                f"📦 Arquivo: {_human_bytes(result_path.stat().st_size)}\n"
                f"🖼 Assets originais incorporados: {capture.asset_count}"
            ),
        )

    async def _emit(self, callback: ProgressCallback | None, update: ProgressUpdate) -> None:
        if callback is not None:
            try:
                await callback(update)
            except Exception:
                pass


def _copy_capture(source: Path, target: Path) -> None:
    for path in source.rglob("*"):
        rel = path.relative_to(source)
        if rel.name in {"offline-clone.zip", "website-assets.zip"}:
            continue
        dest = target / rel
        if path.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)


def _page_candidate(path: str) -> bool:
    lower = (path or "/").lower()
    blocked = (
        ".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".ico",
        ".pdf", ".zip", ".rar", ".7z", ".mp4", ".mp3", ".woff", ".woff2",
        ".css", ".js", ".json", ".xml",
    )
    return not lower.endswith(blocked)


def _page_markdown(url: str, html_path: Path) -> str:
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    for tag in soup(["style", "noscript"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else url
    body = str(soup.body or soup)
    return f"# {title}\n\n> Origem: {url}\n\n" + to_markdown(body, heading_style="ATX").strip() + "\n"


def _sitemap_xml(urls: list[str]) -> str:
    body = "\n".join(f"  <url><loc>{xml_escape(url)}</loc></url>" for url in urls)
    return '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + body + "\n</urlset>\n"


def _make_single_file(index_path: Path, root: Path) -> str:
    soup = BeautifulSoup(index_path.read_text(encoding="utf-8"), "html.parser")

    def data_uri(value: str | None) -> str | None:
        if not value or value.startswith(("data:", "http://", "https://", "#", "mailto:", "tel:")):
            return None
        target = (index_path.parent / value).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            return None
        if not target.is_file():
            return None
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        encoded = base64.b64encode(target.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    def inline_css(css_text: str, css_dir: Path) -> str:
        def repl(match: re.Match) -> str:
            quote = match.group(1) or ""
            raw = match.group(2).strip()
            if raw.startswith(("data:", "http://", "https://", "#")):
                return match.group(0)
            target = (css_dir / raw).resolve()
            try:
                target.relative_to(root.resolve())
            except ValueError:
                return match.group(0)
            if not target.is_file():
                return match.group(0)
            mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            encoded = base64.b64encode(target.read_bytes()).decode("ascii")
            return f"url({quote}data:{mime};base64,{encoded}{quote})"

        return _CSS_URL_RE.sub(repl, css_text)

    for link in list(soup.find_all("link", href=True)):
        rels = {str(x).lower() for x in (link.get("rel") or [])}
        href = str(link.get("href") or "")
        if "stylesheet" in rels and not href.startswith(("http://", "https://")):
            css_path = (index_path.parent / href).resolve()
            try:
                css_path.relative_to(root.resolve())
            except ValueError:
                continue
            if css_path.is_file():
                style = soup.new_tag("style")
                style["data-source"] = href
                style.string = inline_css(css_path.read_text(encoding="utf-8", errors="replace"), css_path.parent)
                link.replace_with(style)
        elif rels & {"icon", "shortcut", "apple-touch-icon", "mask-icon"}:
            uri = data_uri(href)
            if uri:
                link["href"] = uri

    for tag in soup.find_all(True):
        for attr in ("src", "poster"):
            if tag.has_attr(attr):
                uri = data_uri(str(tag.get(attr)))
                if uri:
                    tag[attr] = uri
        if tag.has_attr("srcset"):
            parts = []
            for piece in str(tag["srcset"]).split(","):
                bits = piece.strip().split()
                if bits:
                    uri = data_uri(bits[0])
                    bits[0] = uri or bits[0]
                    parts.append(" ".join(bits))
            tag["srcset"] = ", ".join(parts)
        if tag.has_attr("style"):
            tag["style"] = inline_css(str(tag["style"]), index_path.parent)

    for style in soup.find_all("style"):
        if style.string:
            style.string.replace_with(inline_css(style.string, index_path.parent))

    return "<!doctype html>\n" + str(soup)


def _zip_dir(root: Path, name: str, max_mb: int) -> Path | None:
    bundle = root / name
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_file() and path != bundle:
                zf.write(path, path.relative_to(root))
    if bundle.stat().st_size > max_mb * 1024 * 1024:
        bundle.unlink(missing_ok=True)
        return None
    return bundle


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024
    return f"{size} B"
