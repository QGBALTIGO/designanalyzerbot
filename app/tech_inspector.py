
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from .config import Settings
from .security import validate_public_url
from .tech_fingerprint import TechnologyDetector, TechnologyMatch


@dataclass(frozen=True)
class TechnologyInspection:
    source_url: str
    title: str
    matches: tuple[TechnologyMatch, ...]


class TechnologyInspector:
    def __init__(self, settings: Settings) -> None:
        self.detector = TechnologyDetector(settings.tech_fingerprints_path)

    async def inspect(self, raw_url: str) -> TechnologyInspection:
        validated = await validate_public_url(raw_url)
        url = validated.url
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
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
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            try:
                await page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass
            html = await page.content()
            title = await page.title()
            headers = await response.all_headers() if response else {}
            cookies_list = await context.cookies()
            cookies = {item.get("name", ""): item.get("value", "") for item in cookies_list}
            final_url = page.url
            await context.close()
            await browser.close()

        matches = self.detector.detect(
            url=final_url,
            html=html,
            headers=headers,
            cookies=cookies,
        )
        return TechnologyInspection(
            source_url=final_url,
            title=title,
            matches=tuple(matches),
        )
