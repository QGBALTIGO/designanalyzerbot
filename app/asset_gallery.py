
from __future__ import annotations

import html as html_lib
import shutil
import time
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, ImageDraw

from .analyzer import ProgressUpdate
from .config import Settings
from .security import validate_public_url
from .webclone import WebsiteCapture


ProgressCallback = Callable[[ProgressUpdate], Awaitable[None]]


@dataclass(frozen=True)
class GalleryArtifacts:
    source_url: str
    output_dir: Path
    gallery_html: Path
    contact_sheet: Path | None
    bundle: Path | None
    image_count: int
    summary: str


class AssetGallery:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.capture = WebsiteCapture(settings)

    async def build(
        self,
        gallery_id: str,
        raw_url: str,
        *,
        progress: ProgressCallback | None = None,
    ) -> GalleryArtifacts:
        started = time.monotonic()
        validated = await validate_public_url(raw_url)

        async def cap_progress(update: ProgressUpdate) -> None:
            if progress is None:
                return
            await progress(
                ProgressUpdate(
                    min(78, 2 + int(update.percent * 0.76)),
                    update.stage,
                    int(time.monotonic() - started),
                    None,
                    update.detail,
                )
            )

        capture = await self.capture.capture(
            f"{gallery_id}-gallery",
            validated.url,
            "assets",
            progress=cap_progress,
        )

        out = capture.output_dir
        image_dir = out / "assets" / "images"
        icon_dir = out / "assets" / "icons"
        images = []
        for directory in (image_dir, icon_dir):
            if directory.exists():
                images.extend(path for path in directory.rglob("*") if path.is_file())
        images = sorted(images)

        await _emit(
            progress,
            ProgressUpdate(84, "Montando galeria visual", int(time.monotonic() - started), 15),
        )
        gallery_html = out / "gallery.html"
        gallery_html.write_text(
            _gallery_html(validated.url, images, out),
            encoding="utf-8",
        )

        contact_sheet = out / "contact-sheet.jpg"
        if not _make_contact_sheet(images, contact_sheet):
            contact_sheet = None

        await _emit(
            progress,
            ProgressUpdate(94, "Compactando galeria", int(time.monotonic() - started), 8),
        )
        bundle = out / "asset-gallery.zip"
        with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for path in sorted(out.rglob("*")):
                if path.is_file() and path != bundle:
                    zf.write(path, path.relative_to(out))
        if bundle.stat().st_size > self.settings.max_result_mb * 1024 * 1024:
            bundle.unlink(missing_ok=True)
            bundle = None

        await _emit(
            progress,
            ProgressUpdate(100, "Galeria concluída", int(time.monotonic() - started), 0),
        )
        return GalleryArtifacts(
            source_url=validated.url,
            output_dir=out,
            gallery_html=gallery_html,
            contact_sheet=contact_sheet,
            bundle=bundle,
            image_count=len(images),
            summary=(
                "🖼 Galeria de assets concluída\n"
                f"Imagens e ícones encontrados: {len(images)}\n"
                f"Assets totais: {capture.asset_count}"
            ),
        )


async def _emit(callback: ProgressCallback | None, update: ProgressUpdate) -> None:
    if callback is None:
        return
    try:
        await callback(update)
    except Exception:
        pass


def _gallery_html(source_url: str, images: list[Path], root: Path) -> str:
    cards = []
    for path in images:
        rel = path.relative_to(root).as_posix()
        size_kb = path.stat().st_size / 1024
        cards.append(
            "<figure>"
            f"<img src='{html_lib.escape(rel)}' loading='lazy' alt=''>"
            f"<figcaption>{html_lib.escape(path.name)} · {size_kb:.1f} KB</figcaption>"
            "</figure>"
        )
    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Galeria de assets</title><style>
body{{font-family:system-ui,sans-serif;background:#0d1117;color:#f0f6fc;margin:0;padding:30px}}
header{{max-width:1200px;margin:auto  auto 24px}}p{{color:#8b949e}}main{{max-width:1200px;margin:auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:14px}}
figure{{margin:0;background:#161b22;border:1px solid #30363d;border-radius:14px;overflow:hidden}}img{{display:block;width:100%;height:180px;object-fit:contain;background:#fff}}
figcaption{{padding:10px;font-size:12px;word-break:break-all;color:#b1bac4}}
</style></head><body><header><h1>Galeria de assets</h1><p>{html_lib.escape(source_url)}</p></header><main>{''.join(cards)}</main></body></html>"""


def _make_contact_sheet(paths: list[Path], output: Path) -> bool:
    thumbs = []
    for path in paths[:36]:
        try:
            with Image.open(path) as image:
                frame = ImageOps.contain(image.convert("RGB"), (220, 160))
                canvas = Image.new("RGB", (240, 190), "white")
                x = (240 - frame.width) // 2
                y = (165 - frame.height) // 2
                canvas.paste(frame, (x, y))
                draw = ImageDraw.Draw(canvas)
                label = path.name[:28]
                draw.text((8, 169), label, fill="black")
                thumbs.append(canvas)
        except Exception:
            continue

    if not thumbs:
        return False
    cols = min(4, len(thumbs))
    rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * 240, rows * 190), "white")
    for index, thumb in enumerate(thumbs):
        sheet.paste(thumb, ((index % cols) * 240, (index // cols) * 190))
    sheet.save(output, "JPEG", quality=85, optimize=True)
    return True
