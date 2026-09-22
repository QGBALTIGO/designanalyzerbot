
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from app.history_compare import _create_visual_diff
from app.premium_audit import (
    _accessibility_report,
    _accessibility_score,
    _score_ratio,
    _security_report,
    _seo_report,
)
from app.premium_bot import _allowed
from app.premium_storage import PremiumQuotaExceeded, PremiumStorage
from app.rebuild_common import (
    content_brief,
    deterministic_redesign,
    extract_colors,
    extract_fonts,
    jsx_from_html,
)
from app.site_clone import _make_single_file, _page_candidate, _sitemap_xml
from app.tech_fingerprint import TechnologyDetector


def test_premium_storage_records_and_filters_versions(tmp_path: Path):
    store = PremiumStorage(tmp_path / "db.sqlite3")
    first = store.record_snapshot(
        telegram_user_id=1,
        url="https://example.com/a",
        mode="version",
        output_dir=tmp_path / "one",
        content_hash="a",
        asset_count=3,
        technologies=("React",),
        metadata={"x": 1},
    )
    second = store.record_snapshot(
        telegram_user_id=1,
        url="https://example.com/b",
        mode="audit",
        output_dir=tmp_path / "two",
        content_hash="b",
        asset_count=4,
        technologies=("React", "Cloudflare"),
    )
    store.record_snapshot(
        telegram_user_id=2,
        url="https://example.com/c",
        mode="version",
        output_dir=tmp_path / "other",
    )

    items = store.list_snapshots(1, url="https://example.com/", limit=10)
    assert [x.id for x in items] == [second.id, first.id]
    assert items[0].technologies == ("React", "Cloudflare")
    assert store.previous_snapshot(1, "https://example.com", before_id=second.id).id == first.id


def test_wappalyzer_compatible_detector(tmp_path: Path):
    payload = {
        "technologies": {
            "ExampleCMS": {
                "cats": ["1"],
                "html": ["example-cms"],
                "headers": {"x-powered-by": "Example\\;version:\\1"},
                "scripts": [],
                "url": [],
                "meta": {},
                "cookies": {},
                "implies": ["PHP"],
                "website": "https://example.invalid",
            },
            "PHP": {
                "cats": ["2"],
                "html": [],
                "headers": {},
                "scripts": [],
                "url": [],
                "meta": {},
                "cookies": {},
                "implies": [],
                "website": "https://php.net",
            },
        },
        "categories": {
            "1": {"name": "CMS"},
            "2": {"name": "Programming languages"},
        },
    }
    path = tmp_path / "technologies.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    detector = TechnologyDetector(path)
    matches = detector.detect(
        url="https://example.com",
        html="<html><body class='example-cms'></body></html>",
        headers={"x-powered-by": "Example 5"},
    )
    names = {m.name for m in matches}
    assert "ExampleCMS" in names
    assert "PHP" in names


def test_audit_scoring_helpers():
    seo = _seo_report(
        "https://example.com",
        {
            "title": "Uma página de exemplo com um título adequado",
            "metaDescription": "Descrição " * 12,
            "h1": ["Título"],
            "canonical": "https://example.com",
            "viewport": "width=device-width",
            "lang": "pt-BR",
            "structuredDataCount": 1,
            "images": [{"alt": "foto"}, {"alt": ""}],
            "robots": "index,follow",
        },
    )
    assert _score_ratio(seo["checks"]) >= 80

    security = _security_report(
        "https://example.com",
        {
            "strict-transport-security": "max-age=1",
            "content-security-policy": "default-src 'self'; frame-ancestors 'none'",
            "x-content-type-options": "nosniff",
            "referrer-policy": "strict-origin",
            "permissions-policy": "camera=()",
        },
        "<html></html>",
    )
    assert _score_ratio(security["checks"]) >= 80

    a11y = _accessibility_report(
        {
            "violations": [
                {"id": "x", "impact": "serious", "nodes": [{}, {}], "help": "Fix", "tags": []}
            ],
            "passes": [{}, {}],
            "incomplete": [],
        }
    )
    assert a11y["violating_nodes"] == 2
    assert 0 <= _accessibility_score(a11y) < 100


def test_single_file_embeds_local_assets(tmp_path: Path):
    root = tmp_path / "clone"
    root.mkdir()
    image = root / "assets" / "images" / "a.png"
    image.parent.mkdir(parents=True)
    Image.new("RGB", (10, 10), (100, 100, 100)).save(image)
    css = root / "assets" / "styles" / "app.css"
    css.parent.mkdir(parents=True)
    css.write_text(".x{background:url('../images/a.png')}", encoding="utf-8")
    index = root / "index.html"
    index.write_text(
        '<html><head><link rel="stylesheet" href="assets/styles/app.css"></head>'
        '<body><img src="assets/images/a.png"></body></html>',
        encoding="utf-8",
    )
    result = _make_single_file(index, root)
    assert "data:image/png;base64," in result
    assert "<style" in result
    assert "assets/styles/app.css" in result


def test_multipage_helpers():
    assert _page_candidate("/about")
    assert not _page_candidate("/movie.mp4")
    xml = _sitemap_xml(["https://example.com/", "https://example.com/a?x=1&y=2"])
    assert "<urlset" in xml
    assert "&amp;" in xml


def test_rebuild_helpers_generate_editable_outputs():
    css = "body{color:#123456;font-family:'Inter',sans-serif}.x{background:#abc}"
    assert extract_colors(css)[0] in {"#123456", "#aabbcc"}
    assert extract_fonts(css) == ["Inter"]

    source = "<html><head><title>Teste</title></head><body><main class='x'><h1>Oi</h1><img src='assets/images/a.png'></main></body></html>"
    jsx = jsx_from_html(source)
    assert "className" in jsx
    assert 'src="/assets/images/a.png"' in jsx
    assert "export default function App" in jsx

    brief = content_brief(
        "<html><head><title>Marca</title></head><body><nav><a>Início</a></nav><h1>Olá</h1><p>Um texto grande o bastante para entrar no resumo da página.</p></body></html>",
        css,
        "https://example.com",
    )
    html = deterministic_redesign(brief, "modernize")
    assert "<!doctype html>" in html.lower()
    assert "Modernizado" in html
    assert "<script" not in html.lower()


def test_visual_diff_reports_change(tmp_path: Path):
    old = tmp_path / "old.png"
    new = tmp_path / "new.png"
    diff = tmp_path / "diff.png"
    Image.new("RGB", (100, 100), (255, 255, 255)).save(old)
    Image.new("RGB", (100, 100), (230, 230, 230)).save(new)
    similarity = _create_visual_diff(old, new, diff)
    assert 0 < similarity < 100
    assert diff.exists()


def test_plan_gates():
    class Settings:
        admin_ids = frozenset({99})

    settings = Settings()
    assert not _allowed("free", "audit", 1, settings)
    assert _allowed("pro", "audit", 1, settings)
    assert not _allowed("pro", "rebuild", 1, settings)
    assert _allowed("agency", "rebuild", 1, settings)
    assert _allowed("free", "rebuild", 99, settings)


def test_premium_credits_refund_failed_operations(tmp_path: Path):
    store = PremiumStorage(tmp_path / "credits.sqlite3")
    op = store.begin_operation(1, "audit", 4, 5)
    assert store.premium_credits_used(1) == 4
    with __import__("pytest").raises(PremiumQuotaExceeded):
        store.begin_operation(1, "tech", 2, 5)
    store.finish_operation(op, False)
    assert store.premium_credits_used(1) == 0
    op2 = store.begin_operation(1, "tech", 2, 5)
    store.finish_operation(op2, True)
    assert store.premium_credits_used(1) == 2
