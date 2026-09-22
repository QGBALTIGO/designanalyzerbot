from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
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
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from PIL import Image, ImageChops, ImageStat
from playwright.async_api import async_playwright

from .analyzer import ProgressUpdate
from .config import Settings
from .security import UnsafeUrl, validate_public_url


ProgressCallback = Callable[[ProgressUpdate], Awaitable[None]]

_CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
_CSS_IMPORT_RE = re.compile(r"@import\s+(['\"])(.*?)\1", re.IGNORECASE)
_SAFE_SCHEMES = {"http", "https"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".avif", ".ico", ".bmp"}
_FONT_EXTS = {".woff", ".woff2", ".ttf", ".otf", ".eot"}
_STYLE_EXTS = {".css"}


@dataclass(frozen=True)
class CaptureArtifacts:
    mode: str
    source_url: str
    output_dir: Path
    bundle: Path | None
    manifest: Path
    original_screenshot: Path | None
    clone_screenshot: Path | None
    asset_count: int
    total_bytes: int
    similarity: float | None
    technologies: tuple[str, ...]
    summary: str


@dataclass(frozen=True)
class _Asset:
    url: str
    local_path: str
    content_type: str
    size: int
    category: str


class CaptureError(RuntimeError):
    pass


class WebsiteCapture:
    """Capture assets and build an inert, offline visual snapshot of a public page."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.work_dir.mkdir(parents=True, exist_ok=True)

    async def _emit(self, callback: ProgressCallback | None, update: ProgressUpdate) -> None:
        if callback is not None:
            await callback(update)

    async def capture(
        self,
        capture_id: str,
        raw_url: str,
        mode: str,
        *,
        progress: ProgressCallback | None = None,
    ) -> CaptureArtifacts:
        if mode not in {"assets", "clone"}:
            raise ValueError(f"unsupported capture mode: {mode}")

        started = time.monotonic()
        await self._emit(progress, ProgressUpdate(3, "Validando endereço", 0, None))
        validated = await validate_public_url(raw_url)
        url = validated.url
        await validate_public_url(url)

        output_dir = self.settings.work_dir / f"capture-{capture_id}-{mode}"
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        await self._emit(progress, ProgressUpdate(8, "Abrindo navegador", int(time.monotonic() - started), None))
        snapshot = await asyncio.wait_for(
            self._browser_snapshot(url, output_dir, mode, progress, started),
            timeout=self.settings.capture_timeout_seconds,
        )

        html_text: str = snapshot["html"]
        title: str = snapshot["title"]
        headers: dict[str, str] = snapshot["headers"]
        resource_entries: list[dict[str, str]] = snapshot["resources"]
        original_screenshot = snapshot["screenshot"]

        technologies = tuple(_detect_technologies(html_text, headers))
        await self._emit(
            progress,
            ProgressUpdate(
                34,
                "Mapeando imagens, fontes e estilos",
                int(time.monotonic() - started),
                None,
                f"{len(resource_entries)} recursos vistos pelo navegador",
            ),
        )

        candidates = _collect_candidates(url, html_text, resource_entries)
        assets, url_map, css_sources, skipped = await self._download_assets(
            candidates,
            output_dir,
            progress=progress,
            started=started,
        )
        await self._rewrite_css_files(css_sources, url_map, output_dir)

        index_path: Path | None = None
        clone_screenshot: Path | None = None
        similarity: float | None = None
        if mode == "clone":
            await self._emit(
                progress,
                ProgressUpdate(80, "Montando clone offline seguro", int(time.monotonic() - started), 30),
            )
            inert_html = _rewrite_html_offline(url, html_text, url_map)
            index_path = output_dir / "index.html"
            index_path.write_text(inert_html, encoding="utf-8")

            await self._emit(
                progress,
                ProgressUpdate(87, "Renderizando clone local", int(time.monotonic() - started), 20),
            )
            clone_screenshot = output_dir / "clone-preview.png"
            try:
                await self._render_local(index_path, clone_screenshot)
                if original_screenshot and original_screenshot.exists() and clone_screenshot.exists():
                    similarity = _visual_similarity(original_screenshot, clone_screenshot)
            except Exception:
                clone_screenshot = None
                similarity = None

        manifest_path = output_dir / "manifest.json"
        manifest = {
            "source_url": url,
            "title": title,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
            "asset_count": len(assets),
            "total_bytes": sum(a.size for a in assets),
            "skipped_assets": skipped,
            "technologies": list(technologies),
            "similarity_percent": similarity,
            "safety": {
                "scripts_removed": mode == "clone",
                "forms_disabled": mode == "clone",
                "iframes_removed": mode == "clone",
                "javascript_urls_disabled": mode == "clone",
                "private_network_targets_blocked": True,
            },
            "assets": [
                {
                    "url": a.url,
                    "local_path": a.local_path,
                    "content_type": a.content_type,
                    "bytes": a.size,
                    "category": a.category,
                }
                for a in assets
            ],
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        readme = output_dir / "README.txt"
        readme.write_text(
            "Design Analyzer Bot - captura offline\n"
            f"Origem: {url}\n"
            f"Modo: {mode}\n\n"
            "Este pacote é uma reprodução visual para estudo/design. "
            "Scripts, formulários, iframes e fluxos de autenticação foram desativados no clone offline.\n",
            encoding="utf-8",
        )

        await self._emit(progress, ProgressUpdate(94, "Compactando arquivos", int(time.monotonic() - started), 10))
        bundle = self._zip_output(output_dir, mode)

        total_bytes = sum(a.size for a in assets)
        summary_lines = [
            "✅ Captura concluída",
            f"🌐 {url}",
            f"📦 Assets salvos: {len(assets)}",
            f"💾 Tamanho dos assets: {_human_bytes(total_bytes)}",
        ]
        if technologies:
            summary_lines.append(f"🧠 Tecnologias: {', '.join(technologies[:8])}")
        if similarity is not None:
            summary_lines.append(f"🎯 Similaridade visual aproximada: {similarity:.1f}%")
        if skipped:
            summary_lines.append(f"⚠️ Recursos ignorados pelo limite/segurança: {skipped}")
        if bundle is None:
            summary_lines.append("⚠️ O pacote final excedeu o limite de envio configurado.")

        await self._emit(progress, ProgressUpdate(100, "Concluída", int(time.monotonic() - started), 0))
        return CaptureArtifacts(
            mode=mode,
            source_url=url,
            output_dir=output_dir,
            bundle=bundle,
            manifest=manifest_path,
            original_screenshot=original_screenshot,
            clone_screenshot=clone_screenshot,
            asset_count=len(assets),
            total_bytes=total_bytes,
            similarity=similarity,
            technologies=technologies,
            summary="\n".join(summary_lines),
        )

    async def _browser_snapshot(
        self,
        url: str,
        output_dir: Path,
        mode: str,
        progress: ProgressCallback | None,
        started: float,
    ) -> dict:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1440, "height": 1000},
                java_script_enabled=True,
            )
            page = await context.new_page()

            await self._emit(
                progress,
                ProgressUpdate(12, "Carregando página", int(time.monotonic() - started), None),
            )
            response = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception:
                pass

            # Aciona lazy-loading sem percorrer páginas internas.
            try:
                height = await page.evaluate("Math.min(document.body.scrollHeight, 12000)")
                for y in range(0, int(height), 900):
                    await page.evaluate("(y) => window.scrollTo(0, y)", y)
                    await page.wait_for_timeout(120)
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(400)
            except Exception:
                pass

            await self._emit(
                progress,
                ProgressUpdate(24, "Capturando DOM e recursos", int(time.monotonic() - started), None),
            )

            resources = await page.evaluate(
                """() => {
                    const out = new Map();
                    for (const e of performance.getEntriesByType('resource')) {
                        if (e && e.name) out.set(e.name, e.initiatorType || '');
                    }
                    const add = (u, t) => { if (u) out.set(u, t); };
                    document.querySelectorAll('img').forEach(x => add(x.currentSrc || x.src, 'img'));
                    document.querySelectorAll('link[rel~="stylesheet"]').forEach(x => add(x.href, 'css'));
                    document.querySelectorAll('link[rel~="icon"]').forEach(x => add(x.href, 'icon'));
                    document.querySelectorAll('source').forEach(x => {
                        if (x.src) add(x.src, 'source');
                        if (x.srcset) x.srcset.split(',').forEach(p => add(p.trim().split(/\s+/)[0], 'img'));
                    });
                    return [...out.entries()].map(([url, type]) => ({url, type}));
                }"""
            )
            html_text = await page.content()
            title = await page.title()
            headers: dict[str, str] = {}
            if response is not None:
                try:
                    headers = await response.all_headers()
                except Exception:
                    headers = {}

            screenshot: Path | None = None
            if mode == "clone":
                screenshot = output_dir / "original.png"
                await page.screenshot(path=str(screenshot), full_page=True)

            await context.close()
            await browser.close()

        return {
            "html": html_text,
            "title": title,
            "headers": headers,
            "resources": resources,
            "screenshot": screenshot,
        }

    async def _download_assets(
        self,
        candidates: list[tuple[str, str]],
        output_dir: Path,
        *,
        progress: ProgressCallback | None,
        started: float,
    ) -> tuple[list[_Asset], dict[str, str], dict[str, str], int]:
        max_assets = self.settings.max_capture_assets
        max_total = self.settings.max_capture_mb * 1024 * 1024
        max_single = min(12 * 1024 * 1024, max_total)
        queue: deque[tuple[str, str]] = deque(candidates)
        seen: set[str] = set()
        assets: list[_Asset] = []
        url_map: dict[str, str] = {}
        css_sources: dict[str, str] = {}
        total = 0
        skipped = 0

        timeout = httpx.Timeout(20.0, connect=12.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "DesignAnalyzerBot/1.0"},
            follow_redirects=False,
        ) as client:
            while queue and len(assets) < max_assets and total < max_total:
                asset_url, hint = queue.popleft()
                if asset_url in seen:
                    continue
                seen.add(asset_url)

                try:
                    final_url, response = await _safe_get(client, asset_url)
                except Exception:
                    skipped += 1
                    continue

                content_type = response.headers.get("content-type", "").split(";")[0].lower().strip()
                category = _category_for(final_url, content_type, hint)
                if category is None:
                    continue

                body = response.content
                if not body or len(body) > max_single or total + len(body) > max_total:
                    skipped += 1
                    continue

                rel_path = _asset_path(final_url, category, content_type)
                target = output_dir / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)

                asset = _Asset(final_url, rel_path, content_type, len(body), category)
                assets.append(asset)
                url_map[asset_url] = rel_path
                url_map[final_url] = rel_path
                total += len(body)

                if category == "styles":
                    try:
                        css_text = body.decode(response.encoding or "utf-8", errors="replace")
                    except Exception:
                        css_text = body.decode("utf-8", errors="replace")
                    css_sources[final_url] = rel_path
                    for nested in _css_dependencies(final_url, css_text):
                        if nested not in seen:
                            queue.append((nested, "css-dependency"))

                if len(assets) == 1 or len(assets) % 10 == 0:
                    percent = min(76, 38 + int(38 * (len(assets) / max(1, min(max_assets, len(candidates) + 30)))))
                    await self._emit(
                        progress,
                        ProgressUpdate(
                            percent,
                            "Baixando assets",
                            int(time.monotonic() - started),
                            None,
                            f"{len(assets)} salvos · {_human_bytes(total)}",
                        ),
                    )

        skipped += max(0, len(queue))
        return assets, url_map, css_sources, skipped

    async def _rewrite_css_files(
        self,
        css_sources: dict[str, str],
        url_map: dict[str, str],
        output_dir: Path,
    ) -> None:
        for source_url, rel_path in css_sources.items():
            target = output_dir / rel_path
            try:
                text = target.read_text(encoding="utf-8")
            except Exception:
                continue
            rewritten = _rewrite_css(source_url, rel_path, text, url_map)
            target.write_text(rewritten, encoding="utf-8")

    async def _render_local(self, index_path: Path, screenshot_path: Path) -> None:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1440, "height": 1000}, java_script_enabled=False)
            page = await context.new_page()
            await page.goto(index_path.resolve().as_uri(), wait_until="load", timeout=30_000)
            await page.screenshot(path=str(screenshot_path), full_page=True)
            await context.close()
            await browser.close()

    def _zip_output(self, output_dir: Path, mode: str) -> Path | None:
        bundle = output_dir / ("offline-clone.zip" if mode == "clone" else "website-assets.zip")
        with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for path in sorted(output_dir.rglob("*")):
                if not path.is_file() or path == bundle:
                    continue
                zf.write(path, path.relative_to(output_dir))
        if bundle.stat().st_size > self.settings.max_result_mb * 1024 * 1024:
            bundle.unlink(missing_ok=True)
            return None
        return bundle


async def _safe_get(client: httpx.AsyncClient, url: str, max_redirects: int = 5) -> tuple[str, httpx.Response]:
    current = url
    for _ in range(max_redirects + 1):
        validated = await validate_public_url(current)
        current = validated.url
        response = await client.get(current)
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("location")
            if not location:
                raise CaptureError("redirect sem destino")
            current = urljoin(current, location)
            continue
        if response.status_code != 200:
            raise CaptureError(f"asset HTTP {response.status_code}")
        return current, response
    raise CaptureError("redirecionamentos demais")


def _collect_candidates(base_url: str, html_text: str, resource_entries: list[dict[str, str]]) -> list[tuple[str, str]]:
    found: dict[str, str] = {}

    def add(value: str | None, hint: str) -> None:
        if not value:
            return
        absolute = urljoin(base_url, value.strip())
        parsed = urlsplit(absolute)
        if parsed.scheme in _SAFE_SCHEMES and parsed.netloc:
            found.setdefault(absolute, hint)

    for entry in resource_entries:
        add(entry.get("url"), entry.get("type") or "resource")

    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup.find_all("img"):
        add(tag.get("src"), "img")
        add(tag.get("data-src"), "img")
        for item in _srcset_urls(tag.get("srcset")):
            add(item, "img")
    for tag in soup.find_all("source"):
        add(tag.get("src"), "source")
        for item in _srcset_urls(tag.get("srcset")):
            add(item, "img")
    for tag in soup.find_all("link"):
        rel = {str(x).lower() for x in (tag.get("rel") or [])}
        if "stylesheet" in rel:
            add(tag.get("href"), "css")
        elif rel & {"icon", "shortcut", "apple-touch-icon", "mask-icon"}:
            add(tag.get("href"), "icon")
    for tag in soup.find_all(["video"]):
        add(tag.get("poster"), "img")

    return list(found.items())


def _srcset_urls(srcset: str | None) -> list[str]:
    if not srcset:
        return []
    out = []
    for piece in srcset.split(","):
        value = piece.strip().split()
        if value:
            out.append(value[0])
    return out


def _category_for(url: str, content_type: str, hint: str) -> str | None:
    path = urlsplit(url).path.lower()
    ext = Path(path).suffix.lower()
    if content_type.startswith("image/") or ext in _IMAGE_EXTS or hint in {"img", "icon"}:
        return "icons" if hint == "icon" or ext == ".ico" else "images"
    if content_type.startswith("font/") or ext in _FONT_EXTS:
        return "fonts"
    if content_type == "text/css" or ext in _STYLE_EXTS or hint in {"css", "link"}:
        return "styles"
    return None


def _asset_path(url: str, category: str, content_type: str) -> str:
    parsed = urlsplit(url)
    basename = Path(parsed.path).name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", basename).strip("-._")
    ext = Path(stem).suffix.lower() if stem else ""
    if not ext:
        guessed = mimetypes.guess_extension(content_type) if content_type else None
        ext = guessed or {
            "image/svg+xml": ".svg",
            "image/webp": ".webp",
            "font/woff2": ".woff2",
            "font/woff": ".woff",
            "text/css": ".css",
        }.get(content_type, ".bin")
    name_stem = Path(stem).stem[:60] if stem else category
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
    return f"assets/{category}/{name_stem or category}-{digest}{ext}"


def _css_dependencies(css_url: str, css_text: str) -> list[str]:
    found: list[str] = []
    for match in _CSS_URL_RE.finditer(css_text):
        raw = match.group(2).strip()
        if raw and not raw.startswith(("data:", "#")):
            found.append(urljoin(css_url, raw))
    for match in _CSS_IMPORT_RE.finditer(css_text):
        raw = match.group(2).strip()
        if raw and not raw.startswith(("data:", "#")):
            found.append(urljoin(css_url, raw))
    return list(dict.fromkeys(found))


def _rewrite_css(css_url: str, css_rel_path: str, css_text: str, url_map: dict[str, str]) -> str:
    css_dir = Path(css_rel_path).parent

    def rel_for(raw: str) -> str | None:
        absolute = urljoin(css_url, raw)
        mapped = url_map.get(absolute)
        if not mapped:
            return None
        return Path(os.path.relpath(mapped, start=str(css_dir))).as_posix()

    def repl_url(match: re.Match) -> str:
        quote = match.group(1) or ""
        raw = match.group(2).strip()
        replacement = rel_for(raw)
        return f"url({quote}{replacement}{quote})" if replacement else match.group(0)

    text = _CSS_URL_RE.sub(repl_url, css_text)

    def repl_import(match: re.Match) -> str:
        quote = match.group(1)
        raw = match.group(2).strip()
        replacement = rel_for(raw)
        return f"@import {quote}{replacement}{quote}" if replacement else match.group(0)

    return _CSS_IMPORT_RE.sub(repl_import, text)


def _rewrite_html_offline(base_url: str, html_text: str, url_map: dict[str, str]) -> str:
    soup = BeautifulSoup(html_text, "html.parser")

    for tag in soup.find_all(["script", "iframe", "object", "embed"]):
        tag.decompose()

    for tag in soup.find_all("meta"):
        if str(tag.get("http-equiv", "")).lower() == "refresh":
            tag.decompose()

    for tag in soup.find_all("base"):
        tag.decompose()

    for form in soup.find_all("form"):
        form["action"] = "#"
        form["method"] = "get"
        form["data-offline-disabled"] = "true"
    for control in soup.find_all(["input", "button", "select", "textarea"]):
        control["disabled"] = "disabled"

    def mapped(value: str | None) -> str | None:
        if not value:
            return value
        absolute = urljoin(base_url, value)
        return url_map.get(absolute, value)

    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on"):
                del tag.attrs[attr]

        if tag.has_attr("src"):
            tag["src"] = mapped(tag.get("src"))
        if tag.has_attr("poster"):
            tag["poster"] = mapped(tag.get("poster"))
        if tag.has_attr("srcset"):
            parts = []
            for piece in str(tag["srcset"]).split(","):
                bits = piece.strip().split()
                if bits:
                    bits[0] = mapped(bits[0]) or bits[0]
                    parts.append(" ".join(bits))
            tag["srcset"] = ", ".join(parts)

        if tag.name == "link" and tag.has_attr("href"):
            rel = {str(x).lower() for x in (tag.get("rel") or [])}
            if rel & {"stylesheet", "icon", "shortcut", "apple-touch-icon", "mask-icon"}:
                tag["href"] = mapped(tag.get("href"))

        if tag.name == "a" and str(tag.get("href", "")).lower().startswith("javascript:"):
            tag["href"] = "#"

        if tag.has_attr("style"):
            tag["style"] = _rewrite_inline_css(base_url, str(tag["style"]), url_map)

    for style_tag in soup.find_all("style"):
        if style_tag.string:
            style_tag.string.replace_with(_rewrite_inline_css(base_url, style_tag.string, url_map))

    if soup.head is None:
        head = soup.new_tag("head")
        if soup.html:
            soup.html.insert(0, head)
        else:
            soup.insert(0, head)
    noindex = soup.new_tag("meta")
    noindex.attrs["name"] = "robots"
    noindex.attrs["content"] = "noindex,nofollow"
    soup.head.append(noindex)
    warning = soup.new_tag("meta")
    warning.attrs["name"] = "design-analyzer-offline-copy"
    warning.attrs["content"] = "interactive functionality disabled"
    soup.head.append(warning)

    return "<!doctype html>\n" + str(soup)


def _rewrite_inline_css(base_url: str, css_text: str, url_map: dict[str, str]) -> str:
    def repl(match: re.Match) -> str:
        quote = match.group(1) or ""
        raw = match.group(2).strip()
        absolute = urljoin(base_url, raw)
        replacement = url_map.get(absolute)
        return f"url({quote}{replacement}{quote})" if replacement else match.group(0)

    return _CSS_URL_RE.sub(repl, css_text)


def _detect_technologies(html_text: str, headers: dict[str, str]) -> list[str]:
    lower = html_text.lower()
    detected: list[str] = []

    def add(name: str, condition: bool) -> None:
        if condition and name not in detected:
            detected.append(name)

    add("Next.js", "/_next/" in lower or "__next_data__" in lower)
    add("Nuxt", "/_nuxt/" in lower or "__nuxt__" in lower)
    add("React", "data-reactroot" in lower or "react-dom" in lower or "Next.js" in detected)
    add("Vue.js", "vue.runtime" in lower or "data-v-" in lower or "Nuxt" in detected)
    add("WordPress", "wp-content/" in lower or "wp-includes/" in lower)
    add("Shopify", "cdn.shopify.com" in lower or "shopify.theme" in lower)
    add("Webflow", "data-wf-page" in lower or "webflow.js" in lower)
    add("Wix", "wixstatic.com" in lower)
    add("Google Tag Manager", "googletagmanager.com" in lower)
    add("Google Analytics", "google-analytics.com" in lower or "gtag(" in lower)

    server = headers.get("server", "").lower()
    add("Cloudflare", "cloudflare" in server or "cf-ray" in headers)
    add("nginx", "nginx" in server)
    add("Apache", "apache" in server)
    return detected


def _visual_similarity(original_path: Path, clone_path: Path) -> float:
    with Image.open(original_path) as a_img, Image.open(clone_path) as b_img:
        a = a_img.convert("RGB")
        b = b_img.convert("RGB")
        width = max(1, min(a.width, b.width, 1600))
        height = max(1, min(a.height, b.height, 9000))
        a = a.crop((0, 0, min(a.width, width), min(a.height, height))).resize((width, height))
        b = b.crop((0, 0, min(b.width, width), min(b.height, height))).resize((width, height))
        diff = ImageChops.difference(a, b)
        mean = ImageStat.Stat(diff).mean
        normalized = sum(mean) / (len(mean) * 255)
        return max(0.0, min(100.0, (1.0 - normalized) * 100.0))


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024
    return f"{size} B"
