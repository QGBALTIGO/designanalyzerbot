from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import json
import os
import re
import time
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from axe_playwright_python.async_playwright import Axe
from bs4 import BeautifulSoup
from markdownify import markdownify as to_markdown
from playwright.async_api import async_playwright
from readability import Document
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from .analyzer import ProgressUpdate
from .config import Settings
from .security import validate_public_url
from .tech_fingerprint import TechnologyDetector, TechnologyMatch


ProgressCallback = Callable[[ProgressUpdate], Awaitable[None]]


@dataclass(frozen=True)
class AuditArtifacts:
    source_url: str
    output_dir: Path
    report_json: Path
    report_html: Path
    screenshot: Path
    pdf: Path
    markdown: Path
    warc: Path
    bundle: Path | None
    technologies: tuple[str, ...]
    score_performance: int | None
    score_seo: int
    score_accessibility: int
    score_security: int
    summary: str


class FullSiteAudit:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.premium_dir.mkdir(parents=True, exist_ok=True)
        self.tech = TechnologyDetector(settings.tech_fingerprints_path)

    async def _emit(self, callback: ProgressCallback | None, update: ProgressUpdate) -> None:
        if callback is None:
            return
        try:
            await callback(update)
        except Exception:
            pass

    async def run(
        self,
        audit_id: str,
        raw_url: str,
        *,
        progress: ProgressCallback | None = None,
    ) -> AuditArtifacts:
        started = time.monotonic()
        await self._emit(progress, ProgressUpdate(2, "Validando endereço", 0, None))
        validated = await validate_public_url(raw_url)
        url = validated.url

        out = self.settings.premium_dir / f"audit-{audit_id}"
        out.mkdir(parents=True, exist_ok=True)
        screenshot = out / "screenshot.png"
        pdf_path = out / "page.pdf"
        markdown_path = out / "content.md"
        warc_path = out / "archive.warc.gz"
        report_json = out / "audit.json"
        report_html = out / "report.html"

        await self._emit(progress, ProgressUpdate(8, "Abrindo página no Chromium", int(time.monotonic() - started), None))

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1440, "height": 1000},
                service_workers="block",
            )

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
            response = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception:
                pass

            await self._emit(progress, ProgressUpdate(18, "Coletando DOM, SEO e performance", int(time.monotonic() - started), None))

            html_text = await page.content()
            title = await page.title()
            headers = await response.all_headers() if response else {}
            cookies_list = await context.cookies()
            cookies = {item.get("name", ""): item.get("value", "") for item in cookies_list}
            page_data = await page.evaluate(
                """() => {
                    const nav = performance.getEntriesByType('navigation')[0] || {};
                    const paints = Object.fromEntries(
                        performance.getEntriesByType('paint').map(x => [x.name, x.startTime])
                    );
                    const resources = performance.getEntriesByType('resource');
                    const lcp = performance.getEntriesByType('largest-contentful-paint');
                    const shifts = performance.getEntriesByType('layout-shift');
                    const links = [...document.querySelectorAll('a[href]')].map(a => a.href);
                    const imgs = [...document.images].map(i => ({
                        src: i.currentSrc || i.src,
                        alt: i.getAttribute('alt')
                    }));
                    return {
                        lang: document.documentElement.lang || '',
                        title: document.title || '',
                        metaDescription: document.querySelector('meta[name="description"]')?.content || '',
                        viewport: document.querySelector('meta[name="viewport"]')?.content || '',
                        robots: document.querySelector('meta[name="robots"]')?.content || '',
                        canonical: document.querySelector('link[rel="canonical"]')?.href || '',
                        h1: [...document.querySelectorAll('h1')].map(x => x.innerText.trim()),
                        h2Count: document.querySelectorAll('h2').length,
                        structuredDataCount: document.querySelectorAll('script[type="application/ld+json"]').length,
                        links,
                        images: imgs,
                        performance: {
                            ttfb: Number(nav.responseStart || 0),
                            domContentLoaded: Number(nav.domContentLoadedEventEnd || 0),
                            load: Number(nav.loadEventEnd || 0),
                            transferSize: Number(nav.transferSize || 0),
                            encodedBodySize: Number(nav.encodedBodySize || 0),
                            resourceCount: resources.length,
                            resourceTransferSize: resources.reduce((s, r) => s + Number(r.transferSize || 0), 0),
                            fcp: Number(paints['first-contentful-paint'] || 0),
                            lcp: lcp.length ? Number(lcp[lcp.length - 1].startTime || 0) : 0,
                            cls: shifts.filter(x => !x.hadRecentInput).reduce((s, x) => s + Number(x.value || 0), 0)
                        }
                    };
                }"""
            )
            await page.screenshot(path=str(screenshot), full_page=True)
            await page.pdf(path=str(pdf_path), format="A4", print_background=True)

            await self._emit(progress, ProgressUpdate(31, "Executando axe-core / WCAG", int(time.monotonic() - started), None))
            axe_response: dict = {}
            try:
                axe_result = await Axe().run(page=page)
                axe_response = axe_result.response or {}
            except Exception as exc:
                axe_response = {"error": str(exc), "violations": [], "passes": [], "incomplete": []}

            chromium_executable = p.chromium.executable_path
            await context.close()
            await browser.close()

        await self._emit(progress, ProgressUpdate(43, "Detectando tecnologias", int(time.monotonic() - started), None))
        try:
            tech_matches = await asyncio.wait_for(
                asyncio.to_thread(
                    self.tech.detect,
                    url=url,
                    html=html_text,
                    headers=headers,
                    cookies=cookies,
                ),
                timeout=25,
            )
        except Exception:
            tech_matches = []

        await self._emit(progress, ProgressUpdate(54, "Checando links quebrados", int(time.monotonic() - started), None))
        links_report = await _check_links(url, page_data.get("links") or [], limit=70)

        await self._emit(progress, ProgressUpdate(63, "Extraindo conteúdo legível e Markdown", int(time.monotonic() - started), None))
        readable_title, readable_html = _readability(html_text, title)
        markdown = f"# {readable_title}\n\n" + to_markdown(readable_html, heading_style="ATX")
        markdown_path.write_text(markdown.strip() + "\n", encoding="utf-8")

        await self._emit(progress, ProgressUpdate(69, "Gerando WARC", int(time.monotonic() - started), None))
        _write_warc(warc_path, url, html_text, headers)

        await self._emit(progress, ProgressUpdate(75, "Executando Lighthouse", int(time.monotonic() - started), None))
        lighthouse = await _run_lighthouse(
            self.settings.lighthouse_bin,
            url,
            out / "lighthouse.json",
            chromium_executable,
            timeout=min(240, self.settings.audit_timeout_seconds),
        )

        seo = _seo_report(url, page_data)
        security = _security_report(url, headers, html_text)
        accessibility = _accessibility_report(axe_response)
        performance = _performance_report(page_data.get("performance") or {}, lighthouse)

        score_seo = _score_ratio(seo["checks"])
        score_security = _score_ratio(security["checks"])
        score_accessibility = _accessibility_score(accessibility)
        score_performance = performance.get("lighthouse_score")

        technologies_json = [
            {
                "name": item.name,
                "categories": list(item.categories),
                "version": item.version,
                "website": item.website,
            }
            for item in tech_matches
        ]

        report = {
            "source_url": url,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "title": title,
            "scores": {
                "performance": score_performance,
                "seo": score_seo,
                "accessibility": score_accessibility,
                "security": score_security,
            },
            "performance": performance,
            "seo": seo,
            "accessibility": accessibility,
            "security": security,
            "links": links_report,
            "technologies": technologies_json,
            "readability": {
                "title": readable_title,
                "markdown_file": markdown_path.name,
            },
            "artifacts": {
                "screenshot": screenshot.name,
                "pdf": pdf_path.name,
                "warc": warc_path.name,
                "lighthouse": "lighthouse.json" if (out / "lighthouse.json").exists() else None,
            },
            "content_sha256": hashlib.sha256(html_text.encode("utf-8")).hexdigest(),
        }
        report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report_html.write_text(_render_report_html(report), encoding="utf-8")

        await self._emit(progress, ProgressUpdate(92, "Compactando auditoria", int(time.monotonic() - started), 10))
        bundle = _zip_dir(out, self.settings.max_result_mb)

        summary = (
            "🧪 Auditoria completa concluída\n"
            f"⚡ Performance: {score_performance if score_performance is not None else '—'}/100\n"
            f"🔎 SEO: {score_seo}/100\n"
            f"♿ Acessibilidade: {score_accessibility}/100\n"
            f"🔐 Segurança: {score_security}/100\n"
            f"🧠 Tecnologias detectadas: {len(tech_matches)}\n"
            f"🔗 Links verificados: {links_report['checked']} · quebrados: {links_report['broken_count']}"
        )
        await self._emit(progress, ProgressUpdate(100, "Auditoria concluída", int(time.monotonic() - started), 0))

        return AuditArtifacts(
            source_url=url,
            output_dir=out,
            report_json=report_json,
            report_html=report_html,
            screenshot=screenshot,
            pdf=pdf_path,
            markdown=markdown_path,
            warc=warc_path,
            bundle=bundle,
            technologies=tuple(item.name for item in tech_matches),
            score_performance=score_performance,
            score_seo=score_seo,
            score_accessibility=score_accessibility,
            score_security=score_security,
            summary=summary,
        )


def _readability(html_text: str, fallback_title: str) -> tuple[str, str]:
    try:
        doc = Document(html_text)
        title = doc.short_title() or fallback_title or "Conteúdo"
        content = doc.summary(html_partial=True)
        return title, content
    except Exception:
        soup = BeautifulSoup(html_text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        return fallback_title or "Conteúdo", str(soup.body or soup)


def _seo_report(url: str, data: dict) -> dict:
    title = (data.get("title") or "").strip()
    description = (data.get("metaDescription") or "").strip()
    h1 = [x for x in (data.get("h1") or []) if x]
    images = data.get("images") or []
    missing_alt = sum(1 for img in images if img.get("alt") is None)
    checks = [
        {"name": "Título presente", "ok": bool(title), "detail": f"{len(title)} caracteres"},
        {"name": "Título com tamanho útil", "ok": 20 <= len(title) <= 65, "detail": f"{len(title)} caracteres"},
        {"name": "Meta description", "ok": bool(description), "detail": f"{len(description)} caracteres"},
        {"name": "Description com tamanho útil", "ok": 70 <= len(description) <= 170, "detail": f"{len(description)} caracteres"},
        {"name": "Um H1 principal", "ok": len(h1) == 1, "detail": f"{len(h1)} H1"},
        {"name": "Canonical", "ok": bool(data.get("canonical")), "detail": data.get("canonical") or "ausente"},
        {"name": "Viewport responsivo", "ok": bool(data.get("viewport")), "detail": data.get("viewport") or "ausente"},
        {"name": "Idioma do documento", "ok": bool(data.get("lang")), "detail": data.get("lang") or "ausente"},
        {"name": "Dados estruturados", "ok": int(data.get("structuredDataCount") or 0) > 0, "detail": str(data.get("structuredDataCount") or 0)},
        {"name": "Imagens com alt", "ok": missing_alt == 0, "detail": f"{missing_alt} sem alt de {len(images)}"},
    ]
    return {
        "checks": checks,
        "title": title,
        "description": description,
        "h1": h1,
        "robots": data.get("robots") or "",
        "canonical": data.get("canonical") or "",
        "image_count": len(images),
        "missing_alt": missing_alt,
    }


def _security_report(url: str, headers: dict[str, str], html_text: str) -> dict:
    h = {str(k).lower(): str(v) for k, v in headers.items()}
    https = urlsplit(url).scheme.lower() == "https"
    mixed = len(re.findall(r"(?:src|href)=[\"']http://", html_text, flags=re.IGNORECASE)) if https else 0
    checks = [
        {"name": "HTTPS", "ok": https, "detail": urlsplit(url).scheme},
        {"name": "HSTS", "ok": "strict-transport-security" in h, "detail": h.get("strict-transport-security", "ausente")},
        {"name": "Content-Security-Policy", "ok": "content-security-policy" in h, "detail": "presente" if "content-security-policy" in h else "ausente"},
        {"name": "X-Content-Type-Options", "ok": h.get("x-content-type-options", "").lower() == "nosniff", "detail": h.get("x-content-type-options", "ausente")},
        {"name": "Referrer-Policy", "ok": "referrer-policy" in h, "detail": h.get("referrer-policy", "ausente")},
        {"name": "Permissions-Policy", "ok": "permissions-policy" in h, "detail": "presente" if "permissions-policy" in h else "ausente"},
        {"name": "Proteção de framing", "ok": "x-frame-options" in h or "frame-ancestors" in h.get("content-security-policy", ""), "detail": h.get("x-frame-options", "via CSP" if "frame-ancestors" in h.get("content-security-policy", "") else "ausente")},
        {"name": "Sem mixed content explícito", "ok": mixed == 0, "detail": f"{mixed} referências HTTP"},
    ]
    return {"checks": checks, "headers": h, "mixed_content_references": mixed}


def _accessibility_report(response: dict) -> dict:
    violations = response.get("violations") or []
    compact = []
    impacts: dict[str, int] = {}
    for item in violations:
        impact = item.get("impact") or "unknown"
        nodes = item.get("nodes") or []
        impacts[impact] = impacts.get(impact, 0) + len(nodes)
        compact.append(
            {
                "id": item.get("id"),
                "impact": impact,
                "description": item.get("description"),
                "help": item.get("help"),
                "helpUrl": item.get("helpUrl"),
                "nodes": len(nodes),
                "tags": item.get("tags") or [],
            }
        )
    return {
        "violations_count": len(violations),
        "violating_nodes": sum(item["nodes"] for item in compact),
        "passes_count": len(response.get("passes") or []),
        "incomplete_count": len(response.get("incomplete") or []),
        "impacts": impacts,
        "violations": compact,
        "error": response.get("error"),
    }


def _accessibility_score(report: dict) -> int:
    weights = {"critical": 12, "serious": 8, "moderate": 4, "minor": 2, "unknown": 3}
    penalty = sum(weights.get(k, 3) * v for k, v in (report.get("impacts") or {}).items())
    return max(0, min(100, 100 - penalty))


def _performance_report(raw: dict, lighthouse: dict) -> dict:
    score = lighthouse.get("scores", {}).get("performance")
    return {
        "lighthouse_score": round(float(score) * 100) if isinstance(score, (int, float)) else None,
        "lighthouse_scores": lighthouse.get("scores", {}),
        "metrics": raw,
        "lighthouse_metrics": lighthouse.get("metrics", {}),
        "lighthouse_error": lighthouse.get("error"),
    }


async def _run_lighthouse(
    binary: str,
    url: str,
    output_path: Path,
    chrome_path: str,
    *,
    timeout: int,
) -> dict:
    args = [
        binary,
        url,
        "--quiet",
        "--output=json",
        f"--output-path={output_path}",
        "--preset=desktop",
        f"--chrome-path={chrome_path}",
        "--chrome-flags=--headless --no-sandbox --disable-dev-shm-usage",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except FileNotFoundError:
        return {"error": "Lighthouse não instalado"}
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return {"error": "Lighthouse excedeu o tempo máximo"}
    if proc.returncode != 0 or not output_path.exists():
        err = (stderr or stdout).decode("utf-8", errors="replace")[-1200:]
        return {"error": err or f"Lighthouse saiu com código {proc.returncode}"}
    try:
        data = json.loads(output_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": f"Relatório Lighthouse inválido: {exc}"}
    categories = data.get("categories") or {}
    scores = {
        key: value.get("score")
        for key, value in categories.items()
        if isinstance(value, dict)
    }
    audits = data.get("audits") or {}
    metrics = {}
    for key in (
        "first-contentful-paint",
        "largest-contentful-paint",
        "speed-index",
        "total-blocking-time",
        "cumulative-layout-shift",
        "interactive",
    ):
        item = audits.get(key) or {}
        metrics[key] = {
            "score": item.get("score"),
            "numericValue": item.get("numericValue"),
            "displayValue": item.get("displayValue"),
        }
    return {"scores": scores, "metrics": metrics}


async def _check_links(base_url: str, raw_links: list[str], *, limit: int) -> dict:
    unique: list[str] = []
    seen: set[str] = set()
    for raw in raw_links:
        link = urljoin(base_url, raw)
        parsed = urlsplit(link)
        if parsed.scheme not in {"http", "https"}:
            continue
        clean = parsed._replace(fragment="").geturl()
        if clean not in seen:
            seen.add(clean)
            unique.append(clean)
        if len(unique) >= limit:
            break

    semaphore = asyncio.Semaphore(8)
    timeout = httpx.Timeout(12.0, connect=7.0)

    async def one(link: str) -> dict:
        async with semaphore:
            try:
                final, status = await _safe_status(link, timeout)
                return {"url": link, "final_url": final, "status": status, "broken": status >= 400}
            except Exception as exc:
                return {"url": link, "final_url": None, "status": None, "broken": True, "error": str(exc)[:250]}

    results = await asyncio.gather(*(one(link) for link in unique))
    broken = [item for item in results if item.get("broken")]
    return {
        "checked": len(results),
        "broken_count": len(broken),
        "broken": broken,
        "results": results,
    }


async def _safe_status(url: str, timeout: httpx.Timeout, max_redirects: int = 4) -> tuple[str, int]:
    current = url
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers={"User-Agent": "DesignAnalyzerBot/1.0"}) as client:
        for _ in range(max_redirects + 1):
            validated = await validate_public_url(current)
            current = validated.url
            response = await client.get(current, headers={"Range": "bytes=0-4095"})
            if response.status_code in {301, 302, 303, 307, 308} and response.headers.get("location"):
                current = urljoin(current, response.headers["location"])
                continue
            return current, response.status_code
    raise RuntimeError("redirecionamentos demais")


def _write_warc(path: Path, url: str, html_text: str, headers: dict[str, str]) -> None:
    with path.open("wb") as stream:
        writer = WARCWriter(stream, gzip=True)
        payload = html_text.encode("utf-8")
        http_headers = StatusAndHeaders(
            "200 OK",
            [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(payload)))]
            + [(str(k), str(v)) for k, v in headers.items() if str(k).lower() not in {"content-length", "content-type"}],
            protocol="HTTP/1.1",
        )
        record = writer.create_warc_record(url, "response", payload=BytesIO(payload), http_headers=http_headers)
        writer.write_record(record)


def _score_ratio(checks: list[dict]) -> int:
    if not checks:
        return 0
    passed = sum(1 for item in checks if item.get("ok"))
    return round(passed / len(checks) * 100)


def _render_report_html(report: dict) -> str:
    scores = report["scores"]
    cards = "".join(
        f"<div class='card'><span>{html_lib.escape(name.title())}</span><strong>{'—' if value is None else value}</strong><small>/100</small></div>"
        for name, value in scores.items()
    )

    def checks_table(title: str, checks: list[dict]) -> str:
        rows = "".join(
            "<tr>"
            f"<td>{'✅' if item.get('ok') else '⚠️'}</td>"
            f"<td>{html_lib.escape(str(item.get('name', '')))}</td>"
            f"<td>{html_lib.escape(str(item.get('detail', '')))}</td>"
            "</tr>"
            for item in checks
        )
        return f"<section><h2>{html_lib.escape(title)}</h2><table>{rows}</table></section>"

    tech = "".join(
        f"<li><b>{html_lib.escape(item['name'])}</b> <span>{html_lib.escape(', '.join(item.get('categories') or []))}</span></li>"
        for item in report.get("technologies", [])
    )
    broken = "".join(
        f"<li>{html_lib.escape(str(item.get('status') or 'erro'))} — {html_lib.escape(item['url'])}</li>"
        for item in report["links"]["broken"][:30]
    )
    a11y = "".join(
        f"<li><b>{html_lib.escape(str(item.get('impact')))}</b> — {html_lib.escape(str(item.get('help') or item.get('id')))} ({item.get('nodes', 0)} elementos)</li>"
        for item in report["accessibility"]["violations"][:30]
    )
    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Auditoria - {html_lib.escape(report.get('title') or report['source_url'])}</title>
<style>
body{{font:15px/1.5 system-ui,sans-serif;margin:0;background:#0d1017;color:#eef2ff}}main{{max-width:1100px;margin:auto;padding:32px}}
h1{{font-size:30px}}h2{{margin-top:36px}}.muted{{color:#9ca7bd}}.scores{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}}
.card{{background:#171c27;border:1px solid #293144;border-radius:16px;padding:18px}}.card span{{display:block;color:#aab4c8}}.card strong{{font-size:36px}}.card small{{color:#7f8ba3}}
section{{background:#121722;border:1px solid #252d3d;border-radius:16px;padding:20px;margin:18px 0}}table{{width:100%;border-collapse:collapse}}
td{{border-bottom:1px solid #252d3d;padding:9px;vertical-align:top}}a{{color:#8ab4ff}}li{{margin:7px 0}}
</style></head><body><main>
<h1>Auditoria completa</h1><p class="muted">{html_lib.escape(report['source_url'])}</p>
<div class="scores">{cards}</div>
{checks_table("SEO", report["seo"]["checks"])}
{checks_table("Segurança", report["security"]["checks"])}
<section><h2>Acessibilidade</h2><p>{report["accessibility"]["violations_count"]} violações · {report["accessibility"]["violating_nodes"]} elementos afetados</p><ul>{a11y or "<li>Nenhuma violação automática encontrada.</li>"}</ul></section>
<section><h2>Tecnologias</h2><ul>{tech or "<li>Nenhuma tecnologia identificada.</li>"}</ul></section>
<section><h2>Links quebrados</h2><p>{report["links"]["checked"]} verificados · {report["links"]["broken_count"]} com falha</p><ul>{broken or "<li>Nenhum link quebrado entre os verificados.</li>"}</ul></section>
</main></body></html>"""


def _zip_dir(root: Path, max_mb: int) -> Path | None:
    bundle = root / "full-audit.zip"
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_file() and path != bundle:
                zf.write(path, path.relative_to(root))
    if bundle.stat().st_size > max_mb * 1024 * 1024:
        bundle.unlink(missing_ok=True)
        return None
    return bundle
