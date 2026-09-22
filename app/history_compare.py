
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageEnhance, ImageOps

from .analyzer import ProgressUpdate
from .config import Settings
from .premium_storage import PremiumStorage, Snapshot
from .security import validate_public_url
from .webclone import WebsiteCapture


ProgressCallback = Callable[[ProgressUpdate], Awaitable[None]]


@dataclass(frozen=True)
class ComparisonArtifacts:
    source_url: str
    current_snapshot: Snapshot
    previous_snapshot: Snapshot | None
    report: Path
    diff_image: Path | None
    visual_similarity: float | None
    added_assets: tuple[str, ...]
    removed_assets: tuple[str, ...]
    added_technologies: tuple[str, ...]
    removed_technologies: tuple[str, ...]
    content_changed: bool
    summary: str


class VersionComparator:
    def __init__(self, settings: Settings, storage: PremiumStorage) -> None:
        self.settings = settings
        self.storage = storage
        self.capture = WebsiteCapture(settings)

    async def capture_and_compare(
        self,
        user_id: int,
        compare_id: str,
        raw_url: str,
        *,
        progress: ProgressCallback | None = None,
    ) -> ComparisonArtifacts:
        started = time.monotonic()
        validated = await validate_public_url(raw_url)
        previous = self.storage.previous_snapshot(user_id, validated.url)

        async def cap_progress(update: ProgressUpdate) -> None:
            if progress is None:
                return
            mapped = min(72, 3 + int(update.percent * 0.69))
            await progress(
                ProgressUpdate(
                    mapped,
                    update.stage,
                    int(time.monotonic() - started),
                    None,
                    update.detail,
                )
            )

        capture = await self.capture.capture(
            f"{compare_id}-version",
            validated.url,
            "clone",
            progress=cap_progress,
        )

        index_path = capture.output_dir / "index.html"
        content_hash = _sha256(index_path) if index_path.exists() else None
        current = self.storage.record_snapshot(
            telegram_user_id=user_id,
            url=validated.url,
            mode="version",
            output_dir=capture.output_dir,
            screenshot_path=capture.clone_screenshot,
            manifest_path=capture.manifest,
            html_path=index_path if index_path.exists() else None,
            content_hash=content_hash,
            asset_count=capture.asset_count,
            total_bytes=capture.total_bytes,
            technologies=capture.technologies,
            metadata={"similarity_to_source": capture.similarity},
        )

        await _emit(
            progress,
            ProgressUpdate(80, "Comparando com versão anterior", int(time.monotonic() - started), 20),
        )

        current_manifest = _load_manifest(Path(current.manifest_path)) if current.manifest_path else {}
        previous_manifest = (
            _load_manifest(Path(previous.manifest_path))
            if previous and previous.manifest_path and Path(previous.manifest_path).exists()
            else {}
        )

        current_assets = _asset_urls(current_manifest)
        previous_assets = _asset_urls(previous_manifest)
        added_assets = tuple(sorted(current_assets - previous_assets))
        removed_assets = tuple(sorted(previous_assets - current_assets))

        current_tech = set(current.technologies)
        previous_tech = set(previous.technologies) if previous else set()
        added_tech = tuple(sorted(current_tech - previous_tech))
        removed_tech = tuple(sorted(previous_tech - current_tech))

        content_changed = bool(
            previous
            and current.content_hash
            and previous.content_hash
            and current.content_hash != previous.content_hash
        )

        diff_image: Path | None = None
        visual_similarity: float | None = None
        if (
            previous
            and previous.screenshot_path
            and current.screenshot_path
            and Path(previous.screenshot_path).exists()
            and Path(current.screenshot_path).exists()
        ):
            diff_image = capture.output_dir / "version-diff.png"
            visual_similarity = _create_visual_diff(
                Path(previous.screenshot_path),
                Path(current.screenshot_path),
                diff_image,
            )

        report_path = capture.output_dir / "comparison.json"
        payload = {
            "source_url": validated.url,
            "current_snapshot_id": current.id,
            "previous_snapshot_id": previous.id if previous else None,
            "visual_similarity_percent": visual_similarity,
            "content_changed": content_changed,
            "assets": {
                "current": len(current_assets),
                "previous": len(previous_assets),
                "added": list(added_assets),
                "removed": list(removed_assets),
            },
            "technologies": {
                "current": list(current.technologies),
                "previous": list(previous.technologies) if previous else [],
                "added": list(added_tech),
                "removed": list(removed_tech),
            },
        }
        report_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        await _emit(
            progress,
            ProgressUpdate(100, "Comparação concluída", int(time.monotonic() - started), 0),
        )

        if previous is None:
            summary = (
                "🕘 Primeira versão registrada\n"
                f"Snapshot #{current.id}\n"
                f"📦 Assets: {capture.asset_count}\n"
                "Na próxima comparação o bot mostrará exatamente o que mudou."
            )
        else:
            summary = (
                "🔄 Comparação de versões concluída\n"
                f"Anterior: #{previous.id} → Atual: #{current.id}\n"
                f"🎯 Similaridade visual: {f'{visual_similarity:.1f}%' if visual_similarity is not None else '—'}\n"
                f"➕ Assets novos: {len(added_assets)}\n"
                f"➖ Assets removidos: {len(removed_assets)}\n"
                f"🧠 Tecnologias novas/removidas: {len(added_tech)}/{len(removed_tech)}\n"
                f"📝 Conteúdo mudou: {'sim' if content_changed else 'não'}"
            )

        return ComparisonArtifacts(
            source_url=validated.url,
            current_snapshot=current,
            previous_snapshot=previous,
            report=report_path,
            diff_image=diff_image,
            visual_similarity=visual_similarity,
            added_assets=added_assets,
            removed_assets=removed_assets,
            added_technologies=added_tech,
            removed_technologies=removed_tech,
            content_changed=content_changed,
            summary=summary,
        )


async def _emit(callback: ProgressCallback | None, update: ProgressUpdate) -> None:
    if callback is None:
        return
    try:
        await callback(update)
    except Exception:
        pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _asset_urls(manifest: dict) -> set[str]:
    return {
        str(item.get("url"))
        for item in (manifest.get("assets") or [])
        if isinstance(item, dict) and item.get("url")
    }


def _create_visual_diff(previous_path: Path, current_path: Path, output_path: Path) -> float:
    with Image.open(previous_path) as old_img, Image.open(current_path) as new_img:
        old = old_img.convert("RGB")
        new = new_img.convert("RGB")
        width = max(1, min(old.width, new.width, 1600))
        height = max(1, min(old.height, new.height, 9000))
        old = old.crop((0, 0, min(old.width, width), min(old.height, height))).resize((width, height))
        new = new.crop((0, 0, min(new.width, width), min(new.height, height))).resize((width, height))
        diff = ImageChops.difference(old, new)
        gray = ImageOps.grayscale(diff)
        histogram = gray.histogram()
        total_pixels = width * height
        weighted = sum(index * count for index, count in enumerate(histogram))
        normalized = weighted / max(1, total_pixels * 255)
        similarity = max(0.0, min(100.0, (1.0 - normalized) * 100.0))

        visible = ImageEnhance.Contrast(diff).enhance(3.0)
        visible.thumbnail((1280, 1800))
        visible.save(output_path, "PNG")
        return similarity
