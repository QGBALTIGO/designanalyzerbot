
from __future__ import annotations

import html as html_lib
import json
import re
import shutil
import zipfile
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b")
_FONT_RE = re.compile(r"font-family\s*:\s*([^;}{]+)", re.IGNORECASE)
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_ATTR_MAP = {
    "class": "className",
    "for": "htmlFor",
    "tabindex": "tabIndex",
    "readonly": "readOnly",
    "maxlength": "maxLength",
    "colspan": "colSpan",
    "rowspan": "rowSpan",
    "contenteditable": "contentEditable",
    "crossorigin": "crossOrigin",
    "referrerpolicy": "referrerPolicy",
}


def collect_css(root: Path) -> str:
    chunks: list[str] = []
    styles_dir = root / "assets" / "styles"
    if styles_dir.exists():
        for path in sorted(styles_dir.glob("*.css")):
            try:
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
    return "\n\n".join(chunks)


def extract_colors(css: str, limit: int = 24) -> list[str]:
    counts: dict[str, int] = {}
    for value in _HEX_RE.findall(css):
        normalized = value.lower()
        if len(normalized) == 4:
            normalized = "#" + "".join(ch * 2 for ch in normalized[1:])
        counts[normalized] = counts.get(normalized, 0) + 1
    return [k for k, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


def extract_fonts(css: str, limit: int = 12) -> list[str]:
    counts: dict[str, int] = {}
    for value in _FONT_RE.findall(css):
        family = value.split(",")[0].strip().strip("'\"")
        if not family or family.lower() in {"inherit", "initial", "sans-serif", "serif", "monospace"}:
            continue
        counts[family] = counts.get(family, 0) + 1
    return [k for k, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


def copy_assets(source: Path, destination: Path) -> None:
    assets = source / "assets"
    if assets.exists():
        shutil.copytree(assets, destination, dirs_exist_ok=True)


def jsx_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    body = soup.body or soup

    def render(node, depth: int = 0) -> str:
        indent = "  " * depth
        if isinstance(node, NavigableString):
            text = str(node)
            if not text.strip():
                return ""
            return indent + text.replace("{", "&#123;").replace("}", "&#125;")
        if not isinstance(node, Tag):
            return ""

        name = node.name
        attrs: list[str] = []
        for key, value in node.attrs.items():
            mapped = _ATTR_MAP.get(key, key)
            if mapped.startswith("on") or mapped == "disabled" or key == "style":
                continue
            if key == "class" and isinstance(value, list):
                value = " ".join(value)
            if isinstance(value, list):
                value = " ".join(str(x) for x in value)
            if value is None or value is True:
                attrs.append(f"{mapped}={{true}}")
            else:
                text_value = str(value)
                if mapped in {"src", "poster"} and text_value.startswith("assets/"):
                    text_value = "/" + text_value
                attrs.append(f"{mapped}={json.dumps(text_value)}")

        attr_text = (" " + " ".join(attrs)) if attrs else ""
        if name in _VOID:
            return f"{indent}<{name}{attr_text} />"

        children = [render(child, depth + 1) for child in node.children]
        children = [x for x in children if x]
        if not children:
            return f"{indent}<{name}{attr_text}></{name}>"
        inner = "\n".join(children)
        return f"{indent}<{name}{attr_text}>\n{inner}\n{indent}</{name}>"

    body_jsx = "\n".join(x for x in (render(child, 3) for child in body.children) if x)
    return (
        "export default function App() {\n"
        "  return (\n"
        "    <>\n"
        + body_jsx
        + "\n    </>\n"
        "  );\n"
        "}\n"
    )


def build_react_export(root: Path, source: Path, html: str, css: str) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "public").mkdir(parents=True, exist_ok=True)
    copy_assets(source, root / "public" / "assets")
    (root / "src" / "App.jsx").write_text(jsx_from_html(html), encoding="utf-8")
    (root / "src" / "styles.css").write_text(css or "/* CSS original não encontrado */\n", encoding="utf-8")
    (root / "src" / "main.jsx").write_text(
        "import React from 'react';\n"
        "import { createRoot } from 'react-dom/client';\n"
        "import './styles.css';\n"
        "import App from './App.jsx';\n"
        "createRoot(document.getElementById('root')).render(<App />);\n",
        encoding="utf-8",
    )
    (root / "index.html").write_text(
        '<!doctype html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="root"></div><script type="module" src="/src/main.jsx"></script></body></html>\n',
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        json.dumps(
            {
                "scripts": {"dev": "vite", "build": "vite build"},
                "dependencies": {"vite": "^7.0.0", "react": "^19.0.0", "react-dom": "^19.0.0"},
                "devDependencies": {"@vitejs/plugin-react": "^5.0.0"},
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def build_next_export(root: Path, source: Path, html: str, css: str) -> None:
    (root / "app").mkdir(parents=True, exist_ok=True)
    (root / "public").mkdir(parents=True, exist_ok=True)
    copy_assets(source, root / "public" / "assets")
    jsx = jsx_from_html(html).replace("export default function App()", "export default function Page()")
    (root / "app" / "page.jsx").write_text(jsx, encoding="utf-8")
    (root / "app" / "globals.css").write_text(css or "/* CSS original não encontrado */\n", encoding="utf-8")
    (root / "app" / "layout.jsx").write_text(
        "import './globals.css';\n"
        "export const metadata={title:'Rebuild'};\n"
        "export default function RootLayout({children}){return <html lang=\"pt-BR\"><body>{children}</body></html>}\n",
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        json.dumps(
            {
                "scripts": {"dev": "next dev", "build": "next build", "start": "next start"},
                "dependencies": {"next": "^16.0.0", "react": "^19.0.0", "react-dom": "^19.0.0"},
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def build_tailwind_export(
    root: Path,
    source: Path,
    html: str,
    css: str,
    colors: tuple[str, ...],
    fonts: tuple[str, ...],
) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "public").mkdir(parents=True, exist_ok=True)
    copy_assets(source, root / "public" / "assets")
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on"):
                del tag.attrs[attr]
    (root / "index.html").write_text("<!doctype html>\n" + str(soup), encoding="utf-8")
    (root / "src" / "input.css").write_text(
        '@import "tailwindcss";\n\n/* CSS original mantido para migração gradual */\n' + (css or ""),
        encoding="utf-8",
    )
    color_obj = {f"brand{i + 1}": value for i, value in enumerate(colors[:12])}
    font_obj = {"sans": [fonts[0], "ui-sans-serif", "system-ui"]} if fonts else {}
    (root / "tailwind.config.js").write_text(
        "export default "
        + json.dumps({"theme": {"extend": {"colors": color_obj, "fontFamily": font_obj}}}, ensure_ascii=False, indent=2)
        + ";\n",
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        json.dumps(
            {
                "scripts": {"build": "tailwindcss -i ./src/input.css -o ./dist.css --minify"},
                "devDependencies": {"tailwindcss": "^4.1.0", "@tailwindcss/cli": "^4.1.0"},
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def content_brief(html: str, css: str, source_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else source_url
    headings = [x.get_text(" ", strip=True) for x in soup.find_all(["h1", "h2", "h3"]) if x.get_text(" ", strip=True)][:16]
    paragraphs = [x.get_text(" ", strip=True) for x in soup.find_all("p") if len(x.get_text(" ", strip=True)) > 20][:18]
    nav = [x.get_text(" ", strip=True) for x in soup.select("nav a") if x.get_text(" ", strip=True)][:10]
    images = [str(x.get("src")) for x in soup.find_all("img", src=True)][:12]
    return {
        "source_url": source_url,
        "title": title,
        "headings": headings,
        "paragraphs": paragraphs,
        "navigation": nav,
        "images": images,
        "colors": extract_colors(css),
        "fonts": extract_fonts(css),
    }


def deterministic_redesign(brief: dict, mode: str) -> str:
    colors = brief.get("colors") or ["#7c3aed", "#111827", "#f8fafc"]
    primary = colors[0]
    dark = colors[1] if len(colors) > 1 else "#111827"
    title = html_lib.escape(brief.get("title") or "Projeto")
    headings = brief.get("headings") or [brief.get("title") or "Uma experiência melhor"]
    paragraphs = brief.get("paragraphs") or ["Conteúdo reorganizado em uma experiência responsiva e moderna."]
    nav = brief.get("navigation") or ["Início", "Sobre", "Contato"]
    images = brief.get("images") or []

    nav_html = "".join(f"<a href='#s{i}'>{html_lib.escape(item)}</a>" for i, item in enumerate(nav[:6], start=1))
    sections: list[str] = []
    for i, heading in enumerate(headings[:6], start=1):
        paragraph = paragraphs[(i - 1) % len(paragraphs)]
        image = images[(i - 1) % len(images)] if images else None
        media = f"<img src='{html_lib.escape(image)}' alt=''>" if image else "<div class='visual'></div>"
        sections.append(
            f"<section id='s{i}' class='section'><div><span class='eyebrow'>Seção {i:02d}</span>"
            f"<h2>{html_lib.escape(heading)}</h2><p>{html_lib.escape(paragraph)}</p></div>{media}</section>"
        )

    mode_label = "Modernizado" if mode == "modernize" else "Inspirado na referência"
    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
:root{{--primary:{primary};--ink:{dark};--paper:#f8fafc;--card:#fff;--muted:#64748b}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;font-family:Inter,ui-sans-serif,system-ui,sans-serif;color:var(--ink);background:var(--paper)}}
.container{{width:min(1180px,calc(100% - 36px));margin:auto}}header{{position:sticky;top:0;z-index:5;background:rgba(248,250,252,.88);backdrop-filter:blur(14px);border-bottom:1px solid #e2e8f0}}
nav{{min-height:72px;display:flex;align-items:center;justify-content:space-between;gap:20px}}nav .links{{display:flex;gap:20px;flex-wrap:wrap}}a{{color:inherit;text-decoration:none}}.brand{{font-weight:850;letter-spacing:-.04em}}
.hero{{padding:96px 0 72px;display:grid;grid-template-columns:1.2fr .8fr;gap:56px;align-items:center}}.hero h1{{font-size:clamp(42px,7vw,82px);line-height:.96;letter-spacing:-.055em;margin:0 0 24px}}
.hero p{{font-size:20px;color:var(--muted);max-width:680px}}.pill{{display:inline-flex;padding:9px 14px;border-radius:999px;background:#eee;color:var(--primary);font-weight:750;margin-bottom:20px}}
.hero-card,.section{{background:var(--card);border:1px solid #e2e8f0;border-radius:28px;box-shadow:0 18px 55px rgba(15,23,42,.07)}}.hero-card{{min-height:360px;background:linear-gradient(145deg,var(--primary),var(--ink));position:relative;overflow:hidden}}
main{{padding-bottom:80px}}.section{{padding:42px;margin:24px 0;display:grid;grid-template-columns:1fr 1fr;gap:42px;align-items:center}}.section:nth-child(even){{direction:rtl}}.section>*{{direction:ltr}}
h2{{font-size:clamp(30px,4vw,52px);line-height:1.05;letter-spacing:-.04em;margin:8px 0 18px}}p{{line-height:1.75;color:var(--muted)}}.eyebrow{{font-size:12px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;color:var(--primary)}}
.section img,.visual{{width:100%;aspect-ratio:4/3;object-fit:cover;border-radius:20px;background:linear-gradient(135deg,var(--primary),var(--ink))}}footer{{padding:46px 0;border-top:1px solid #e2e8f0;color:var(--muted)}}
@media(max-width:760px){{nav .links{{display:none}}.hero,.section{{grid-template-columns:1fr}}.hero{{padding-top:64px}}.section{{padding:26px}}}}
</style></head><body>
<header><div class="container"><nav><a class="brand" href="#top">{title}</a><div class="links">{nav_html}</div></nav></div></header>
<div id="top" class="container"><section class="hero"><div><span class="pill">{mode_label}</span><h1>{html_lib.escape(headings[0])}</h1><p>{html_lib.escape(paragraphs[0])}</p></div><div class="hero-card"></div></section>
<main>{''.join(sections)}</main></div><footer><div class="container">{title} · reconstrução visual</div></footer></body></html>"""


def sanitize_generated_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "iframe", "object", "embed"]):
        tag.decompose()
    for form in soup.find_all("form"):
        form["action"] = "#"
        for control in form.find_all(["input", "button", "select", "textarea"]):
            control["disabled"] = "disabled"
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on"):
                del tag.attrs[attr]
    return "<!doctype html>\n" + str(soup)


def zip_dir(root: Path, name: str, max_mb: int) -> Path | None:
    bundle = root / name
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_file() and path != bundle:
                zf.write(path, path.relative_to(root))
    if bundle.stat().st_size > max_mb * 1024 * 1024:
        bundle.unlink(missing_ok=True)
        return None
    return bundle
