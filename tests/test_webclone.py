from pathlib import Path

from PIL import Image

from app.webclone import (
    _category_for,
    _collect_candidates,
    _css_dependencies,
    _detect_technologies,
    _rewrite_css,
    _rewrite_html_offline,
    _visual_similarity,
)


def test_collect_candidates_finds_dom_and_performance_assets():
    html = """
    <html><head>
      <link rel="stylesheet" href="/css/app.css">
      <link rel="icon" href="/favicon.ico">
    </head><body>
      <img src="/img/a.png" srcset="/img/a-2x.png 2x">
      <source srcset="/img/b.webp 1x">
    </body></html>
    """
    items = dict(_collect_candidates(
        "https://example.com/page",
        html,
        [{"url": "https://cdn.example.com/font.woff2", "type": "css"}],
    ))
    assert "https://example.com/css/app.css" in items
    assert "https://example.com/favicon.ico" in items
    assert "https://example.com/img/a.png" in items
    assert "https://example.com/img/a-2x.png" in items
    assert "https://example.com/img/b.webp" in items
    assert "https://cdn.example.com/font.woff2" in items


def test_rewrite_html_makes_clone_inert_and_local():
    html = """
    <html><head><script>alert(1)</script><meta http-equiv="refresh" content="1"></head>
    <body onload="steal()">
      <form action="/login"><input name="password"><button>Go</button></form>
      <iframe src="https://evil.example"></iframe>
      <img src="/img/logo.png">
      <a href="javascript:alert(1)">x</a>
    </body></html>
    """
    rewritten = _rewrite_html_offline(
        "https://example.com/",
        html,
        {"https://example.com/img/logo.png": "assets/images/logo.png"},
    )
    lower = rewritten.lower()
    assert "<script" not in lower
    assert "<iframe" not in lower
    assert "http-equiv=\"refresh\"" not in lower
    assert "onload=" not in lower
    assert 'action="#"' in rewritten
    assert 'disabled="disabled"' in rewritten
    assert 'src="assets/images/logo.png"' in rewritten
    assert 'href="#"' in rewritten
    assert "noindex,nofollow" in rewritten


def test_css_dependencies_and_rewrite():
    css = """
    @font-face { src: url('../fonts/x.woff2'); }
    .hero { background-image: url("/img/hero.webp"); }
    @import "more.css";
    """
    deps = _css_dependencies("https://example.com/css/app.css", css)
    assert "https://example.com/fonts/x.woff2" in deps
    assert "https://example.com/img/hero.webp" in deps
    assert "https://example.com/css/more.css" in deps

    mapped = {
        "https://example.com/fonts/x.woff2": "assets/fonts/x.woff2",
        "https://example.com/img/hero.webp": "assets/images/hero.webp",
        "https://example.com/css/more.css": "assets/styles/more.css",
    }
    rewritten = _rewrite_css(
        "https://example.com/css/app.css",
        "assets/styles/app.css",
        css,
        mapped,
    )
    assert "../fonts/x.woff2" in rewritten
    assert "../images/hero.webp" in rewritten
    assert "more.css" in rewritten


def test_category_filters_to_visual_assets():
    assert _category_for("https://x/a.png", "image/png", "img") == "images"
    assert _category_for("https://x/a.woff2", "font/woff2", "css-dependency") == "fonts"
    assert _category_for("https://x/a.css", "text/css", "css") == "styles"
    assert _category_for("https://x/app.js", "application/javascript", "script") is None


def test_technology_detection():
    html = """
    <html data-wf-page="x"><head>
      <script id="__NEXT_DATA__"></script>
      <script src="https://www.googletagmanager.com/gtm.js"></script>
      <link href="/wp-content/theme.css">
    </head></html>
    """
    tech = _detect_technologies(html, {"server": "cloudflare", "cf-ray": "abc"})
    assert "Next.js" in tech
    assert "React" in tech
    assert "WordPress" in tech
    assert "Webflow" in tech
    assert "Google Tag Manager" in tech
    assert "Cloudflare" in tech


def test_visual_similarity_identical_images(tmp_path: Path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    Image.new("RGB", (20, 20), (100, 120, 140)).save(a)
    Image.new("RGB", (20, 20), (100, 120, 140)).save(b)
    assert _visual_similarity(a, b) == 100.0
