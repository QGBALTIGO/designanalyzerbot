from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class TechnologyMatch:
    name: str
    categories: tuple[str, ...]
    version: str | None = None
    website: str | None = None


@lru_cache(maxsize=4096)
def _regex(pattern: str) -> re.Pattern[str] | None:
    raw = (pattern or "").split(r"\;", 1)[0]
    if not raw:
        raw = ".*"
    try:
        return re.compile(raw, re.IGNORECASE)
    except re.error:
        return None


def _version_from_pattern(pattern: str, match: re.Match[str] | None) -> str | None:
    if match is None or r"\;version:" not in pattern:
        return None
    template = pattern.split(r"\;version:", 1)[1]
    value = template
    for idx in range(1, 4):
        token = f"\\{idx}"
        if token in value:
            try:
                group = match.group(idx) or ""
            except IndexError:
                group = ""
            value = value.replace(token, group)
    value = re.sub(r"\\\d", "", value).strip()
    return value or None


class TechnologyDetector:
    """Wappalyzer-compatible passive detector using WebAnalyze's MIT fingerprint database."""

    def __init__(self, fingerprints_path: Path) -> None:
        self.path = Path(fingerprints_path)
        self._data: dict[str, Any] | None = None

    def available(self) -> bool:
        return self.path.is_file()

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        return self._data

    def detect(
        self,
        *,
        url: str,
        html: str,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> list[TechnologyMatch]:
        if not self.available():
            return []

        data = self._load()
        technologies: dict[str, Any] = data.get("technologies") or {}
        categories: dict[str, Any] = data.get("categories") or {}
        headers_lower = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        cookies_map = {str(k): str(v) for k, v in (cookies or {}).items()}

        # Keep CPU bounded on pathological documents while retaining the rendered head/body signals.
        html_scan = html[:2_500_000]
        scripts = "\n".join(
            re.findall(
                r"<script[^>]+src=[\"']([^\"']+)[\"']",
                html_scan,
                flags=re.IGNORECASE,
            )
        )
        meta: dict[str, str] = {}
        for name, content in re.findall(
            r"<meta[^>]+name=[\"']([^\"']+)[\"'][^>]+content=[\"']([^\"']*)[\"']",
            html_scan,
            flags=re.IGNORECASE,
        ):
            meta[name.lower()] = content
        for content, name in re.findall(
            r"<meta[^>]+content=[\"']([^\"']*)[\"'][^>]+name=[\"']([^\"']+)[\"']",
            html_scan,
            flags=re.IGNORECASE,
        ):
            meta[name.lower()] = content

        matches: dict[str, TechnologyMatch] = {}

        for name, spec in technologies.items():
            if not isinstance(spec, dict):
                continue
            version: str | None = None
            found = False

            def check_patterns(value: str, patterns: Any) -> bool:
                nonlocal version
                if not patterns:
                    return False
                if isinstance(patterns, str):
                    patterns = [patterns]
                for raw in patterns:
                    if not isinstance(raw, str):
                        continue
                    rx = _regex(raw)
                    if rx is None:
                        continue
                    m = rx.search(value)
                    if m:
                        version = version or _version_from_pattern(raw, m)
                        return True
                return False

            if check_patterns(url, spec.get("url")):
                found = True
            if not found and check_patterns(html_scan, spec.get("html")):
                found = True
            if not found and check_patterns(scripts, spec.get("scripts")):
                found = True

            if not found:
                for header_name, pattern in (spec.get("headers") or {}).items():
                    value = headers_lower.get(str(header_name).lower())
                    if value is not None and check_patterns(value, pattern):
                        found = True
                        break

            if not found:
                for meta_name, patterns in (spec.get("meta") or {}).items():
                    value = meta.get(str(meta_name).lower())
                    if value is not None and check_patterns(value, patterns):
                        found = True
                        break

            if not found:
                for cookie_name, pattern in (spec.get("cookies") or {}).items():
                    if cookie_name in cookies_map and check_patterns(cookies_map[cookie_name], pattern):
                        found = True
                        break

            if not found:
                continue

            cat_names: list[str] = []
            cats = spec.get("cats") or []
            if not isinstance(cats, list):
                cats = [cats]
            for cid in cats:
                cat = categories.get(str(cid)) or {}
                cat_name = cat.get("name") if isinstance(cat, dict) else None
                if cat_name:
                    cat_names.append(str(cat_name))

            matches[name] = TechnologyMatch(
                name=name,
                categories=tuple(dict.fromkeys(cat_names)),
                version=version,
                website=spec.get("website") or None,
            )

            for implied in spec.get("implies") or []:
                implied_name = str(implied).split(r"\;", 1)[0]
                if implied_name and implied_name not in matches:
                    implied_spec = technologies.get(implied_name) or {}
                    implied_cats = []
                    for cid in implied_spec.get("cats") or []:
                        cat = categories.get(str(cid)) or {}
                        if isinstance(cat, dict) and cat.get("name"):
                            implied_cats.append(str(cat["name"]))
                    matches[implied_name] = TechnologyMatch(
                        name=implied_name,
                        categories=tuple(dict.fromkeys(implied_cats)),
                        website=implied_spec.get("website") or None,
                    )

        return sorted(matches.values(), key=lambda x: (x.categories[:1], x.name.lower()))


def compact_technology_names(matches: list[TechnologyMatch], limit: int = 20) -> list[str]:
    return [m.name for m in matches[: max(1, limit)]]
