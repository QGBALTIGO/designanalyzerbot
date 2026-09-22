
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import CommandHandler, ContextTypes

from .asset_gallery import AssetGallery
from .config import Settings
from .history_compare import VersionComparator
from .premium_audit import FullSiteAudit
from .premium_storage import PremiumQuotaExceeded, PremiumStorage
from .rebuild_export import RebuildExporter
from .redesign import InspiredRebuilder
from .security import UnsafeUrl, validate_public_url
from .site_clone import MultiPageCloner, SingleFileExporter
from .storage import Storage
from .tech_inspector import TechnologyInspector

logger = logging.getLogger(__name__)

PRO_MODES = {"audit", "singlefile", "multipage", "gallery", "tech", "compare", "versions"}
AGENCY_MODES = {"rebuild", "modernize", "inspire"}


def premium_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🧪 Auditoria completa", callback_data="premium:audit"),
            InlineKeyboardButton("🧠 Tecnologias", callback_data="premium:tech"),
        ],
        [
            InlineKeyboardButton("🕷 Clonar site", callback_data="premium:multipage"),
            InlineKeyboardButton("📄 HTML único", callback_data="premium:singlefile"),
        ],
        [
            InlineKeyboardButton("🖼 Galeria de assets", callback_data="premium:gallery"),
            InlineKeyboardButton("🔄 Comparar versões", callback_data="premium:compare"),
        ],
        [
            InlineKeyboardButton("🧱 Reconstruir", callback_data="premium:rebuild"),
            InlineKeyboardButton("✨ Modernizar", callback_data="premium:modernize"),
        ],
        [
            InlineKeyboardButton("💡 Inspire-se", callback_data="premium:inspire"),
            InlineKeyboardButton("🕘 Versões salvas", callback_data="premium:versions"),
        ],
        [InlineKeyboardButton("⬅️ Menu principal", callback_data="premium:back")],
    ])


def premium_command_specs() -> list[tuple[str, str]]:
    return [
        ("premium", "Ferramentas profissionais"),
        ("auditar", "SEO, performance, WCAG e segurança"),
        ("clonarsite", "Clonar várias páginas do site"),
        ("htmlunico", "Salvar tudo em um HTML"),
        ("galeria", "Galeria de imagens e assets"),
        ("tecnologias", "Detectar tecnologias do site"),
        ("comparar", "Comparar com versão anterior"),
        ("versoes", "Histórico de versões"),
        ("reconstruir", "Exportar HTML, React, Next e Tailwind"),
        ("modernizar", "Criar versão modernizada"),
        ("inspirar", "Criar nova composição inspirada"),
    ]


def install_premium(
    application,
    settings: Settings,
    storage: Storage,
) -> None:
    pstore = PremiumStorage(settings.database_path)
    application.bot_data.update(
        premium_storage=pstore,
        premium_audit=FullSiteAudit(settings),
        premium_multi=MultiPageCloner(settings),
        premium_single=SingleFileExporter(settings),
        premium_rebuild=RebuildExporter(settings),
        premium_redesign=InspiredRebuilder(settings),
        premium_compare=VersionComparator(settings, pstore),
        premium_gallery=AssetGallery(settings),
        premium_tech=TechnologyInspector(settings),
        premium_semaphore=asyncio.Semaphore(settings.max_concurrent_premium),
        premium_text_handler=premium_text_message,
        premium_callback_handler=premium_callback,
    )

    application.add_handler(CommandHandler("premium", premium_command))
    application.add_handler(CommandHandler("auditar", _cmd("audit")))
    application.add_handler(CommandHandler("clonarsite", _cmd("multipage")))
    application.add_handler(CommandHandler("htmlunico", _cmd("singlefile")))
    application.add_handler(CommandHandler("galeria", _cmd("gallery")))
    application.add_handler(CommandHandler("tecnologias", _cmd("tech")))
    application.add_handler(CommandHandler("comparar", _cmd("compare")))
    application.add_handler(CommandHandler("versoes", versions_command))
    application.add_handler(CommandHandler("reconstruir", _cmd("rebuild")))
    application.add_handler(CommandHandler("modernizar", _cmd("modernize")))
    application.add_handler(CommandHandler("inspirar", _cmd("inspire")))


def _cmd(mode: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        raw = " ".join(context.args).strip() if context.args else ""
        if raw:
            await _start_premium(update, context, mode, raw)
            return
        context.user_data["awaiting_premium"] = mode
        await update.effective_message.reply_text(_prompt(mode), parse_mode=ParseMode.HTML)
    return handler


async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "💎 <b>Ferramentas Premium</b>\n\n"
        "Auditoria profissional, clone multipágina, exports para desenvolvimento, "
        "histórico de versões e reconstrução visual.",
        parse_mode=ParseMode.HTML,
        reply_markup=premium_menu(),
    )


async def premium_text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    mode = context.user_data.pop("awaiting_premium", None)
    if not mode:
        return False
    raw = (update.effective_message.text or "").strip()
    if mode == "versions":
        await _show_versions(update, context, raw or None)
        return True
    await _start_premium(update, context, mode, raw)
    return True


async def premium_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    query = update.callback_query
    action = query.data or ""
    if action == "premium":
        await query.answer()
        await query.message.reply_text(
            "💎 <b>Ferramentas Premium</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=premium_menu(),
        )
        return True
    if not action.startswith("premium:"):
        return False

    await query.answer()
    mode = action.split(":", 1)[1]
    if mode == "back":
        from .bot import menu
        await query.message.reply_text("🎨 <b>Design Analyzer</b>", parse_mode=ParseMode.HTML, reply_markup=menu())
        return True
    if mode == "versions":
        await _show_versions(update, context, None)
        return True

    context.user_data["awaiting_premium"] = mode
    await query.message.reply_text(_prompt(mode), parse_mode=ParseMode.HTML)
    return True


async def versions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = " ".join(context.args).strip() if context.args else None
    await _show_versions(update, context, raw)


async def _show_versions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    raw_url: str | None,
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    storage: Storage = context.application.bot_data["storage"]
    pstore: PremiumStorage = context.application.bot_data["premium_storage"]
    user = _upsert(update, storage)
    if not _allowed(user.plan, "versions", user.telegram_user_id, settings):
        await _locked(update)
        return

    url = None
    if raw_url:
        try:
            url = (await validate_public_url(raw_url)).url
        except UnsafeUrl as exc:
            await update.effective_message.reply_text(f"⚠️ {html.escape(str(exc))}")
            return

    snapshots = pstore.list_snapshots(user.telegram_user_id, url=url, limit=12)
    if not snapshots:
        await update.effective_message.reply_text(
            "🕘 Nenhuma versão premium registrada ainda. Use /comparar URL para criar a primeira."
        )
        return

    lines = ["🕘 <b>Versões registradas</b>", ""]
    for item in snapshots:
        lines.append(
            f"<b>#{item.id}</b> · {html.escape(item.mode)} · "
            f"{html.escape(item.host)} · {html.escape(item.created_at[:16].replace('T', ' '))}"
        )
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def _start_premium(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    mode: str,
    raw_url: str,
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    storage: Storage = context.application.bot_data["storage"]
    user = _upsert(update, storage)

    if not _allowed(user.plan, mode, user.telegram_user_id, settings):
        await _locked(update, agency=mode in AGENCY_MODES)
        return

    try:
        normalized = (await validate_public_url(raw_url)).url
    except UnsafeUrl as exc:
        await update.effective_message.reply_text(f"⚠️ {html.escape(str(exc))}")
        return

    operation_id = uuid.uuid4().hex[:10]
    pstore: PremiumStorage = context.application.bot_data["premium_storage"]
    credit_operation_id = None
    if user.telegram_user_id not in settings.admin_ids:
        cost = _premium_cost(mode, user.plan, settings)
        limit = settings.premium_credit_limit(user.plan)
        try:
            credit_operation_id = pstore.begin_operation(
                user.telegram_user_id,
                mode,
                cost,
                limit,
            )
        except PremiumQuotaExceeded as exc:
            await update.effective_message.reply_text(
                "💳 <b>Créditos premium esgotados</b>\n\n"
                f"Usados: <b>{exc.used}/{exc.limit}</b>\n"
                f"Este recurso custa: <b>{exc.cost}</b> créditos.\n\n"
                "Os créditos renovam no início de cada mês.",
                parse_mode=ParseMode.HTML,
            )
            return

    message = await update.effective_message.reply_text(
        _render_progress(mode, normalized, 0, "Preparando", 0, None),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    context.application.create_task(
        _run_premium(
            context,
            user.telegram_user_id,
            user.plan,
            operation_id,
            mode,
            normalized,
            message.chat_id,
            message.message_id,
            credit_operation_id,
        ),
        name=f"premium-{mode}-{operation_id}",
    )


async def _run_premium(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    plan: str,
    operation_id: str,
    mode: str,
    url: str,
    chat_id: int,
    message_id: int,
    credit_operation_id: int | None,
) -> None:
    app = context.application
    settings: Settings = app.bot_data["settings"]
    pstore: PremiumStorage = app.bot_data["premium_storage"]
    semaphore: asyncio.Semaphore = app.bot_data["premium_semaphore"]
    last_edit = 0.0

    async def progress(update) -> None:
        nonlocal last_edit
        now = time.monotonic()
        important = update.percent in {0, 2, 3, 8, 15, 31, 43, 54, 60, 67, 74, 80, 87, 92, 94, 100}
        if not important and now - last_edit < 6:
            return
        try:
            await app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=_render_progress(
                    mode,
                    url,
                    update.percent,
                    update.stage,
                    update.elapsed_seconds,
                    update.detail,
                ),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            last_edit = now
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.warning("premium progress edit failed: %s", exc)
        except TelegramError as exc:
            logger.warning("premium progress Telegram error: %s", exc)

    try:
        async with semaphore:
            if mode == "audit":
                result = await app.bot_data["premium_audit"].run(operation_id, url, progress=progress)
                _record_audit(pstore, user_id, result)
                await _finish_text(app, chat_id, message_id, mode, result.summary)
                await _send_document(app, chat_id, result.report_html, "audit-report.html", "📊 Relatório navegável")
                if result.bundle:
                    await _send_document(app, chat_id, result.bundle, "full-audit.zip", "📦 Auditoria completa")

            elif mode == "singlefile":
                result = await app.bot_data["premium_single"].export(operation_id, url, progress=progress)
                pstore.record_snapshot(
                    telegram_user_id=user_id,
                    url=result.source_url,
                    mode="singlefile",
                    output_dir=result.output_dir,
                    screenshot_path=result.screenshot,
                    html_path=result.html,
                    content_hash=_sha(result.html),
                    metadata={"bytes": result.size_bytes},
                )
                await _finish_text(app, chat_id, message_id, mode, result.summary)
                await _send_document(app, chat_id, result.html, "single-file.html", "📄 Página inteira em um HTML")

            elif mode == "multipage":
                effective_plan = "agency" if user_id in settings.admin_ids else plan
                max_pages = settings.clone_pages(effective_plan)
                result = await app.bot_data["premium_multi"].clone(
                    operation_id,
                    url,
                    max_pages=max_pages,
                    progress=progress,
                )
                root_html = result.output_dir / "index.html"
                root_shot = result.output_dir / "clone-preview.png"
                root_manifest = result.output_dir / "manifest.json"
                pstore.record_snapshot(
                    telegram_user_id=user_id,
                    url=result.source_url,
                    mode="multipage",
                    output_dir=result.output_dir,
                    screenshot_path=root_shot if root_shot.exists() else None,
                    manifest_path=root_manifest if root_manifest.exists() else None,
                    html_path=root_html if root_html.exists() else None,
                    content_hash=_sha(root_html) if root_html.exists() else None,
                    asset_count=result.asset_count,
                    total_bytes=result.total_bytes,
                    metadata={"pages": result.page_count, "average_similarity": result.average_similarity},
                )
                await _finish_text(app, chat_id, message_id, mode, result.summary)
                if result.bundle:
                    await _send_document(app, chat_id, result.bundle, "multipage-clone.zip", "🕷 Site offline multipágina")
                else:
                    await _send_document(app, chat_id, result.page_map, "site-map.json", "⚠️ ZIP excedeu o limite")

            elif mode == "gallery":
                result = await app.bot_data["premium_gallery"].build(operation_id, url, progress=progress)
                await _finish_text(app, chat_id, message_id, mode, result.summary)
                if result.contact_sheet:
                    await _send_photo_or_document(app, chat_id, result.contact_sheet, "🖼 Visão geral dos assets")
                await _send_document(app, chat_id, result.gallery_html, "gallery.html", "🖼 Galeria navegável")
                if result.bundle:
                    await _send_document(app, chat_id, result.bundle, "asset-gallery.zip", "📦 Galeria + assets")

            elif mode == "tech":
                await progress(_P(15, "Carregando site", 0))
                result = await app.bot_data["premium_tech"].inspect(url)
                await progress(_P(100, "Tecnologias identificadas", 1))
                summary = _tech_summary(result)
                await _finish_text(app, chat_id, message_id, mode, summary)

            elif mode == "compare":
                result = await app.bot_data["premium_compare"].capture_and_compare(
                    user_id,
                    operation_id,
                    url,
                    progress=progress,
                )
                await _finish_text(app, chat_id, message_id, mode, result.summary)
                if result.diff_image:
                    await _send_photo_or_document(app, chat_id, result.diff_image, "🔄 Diferença visual")
                await _send_document(app, chat_id, result.report, "comparison.json", "📋 Dados da comparação")

            elif mode == "rebuild":
                result = await app.bot_data["premium_rebuild"].export(operation_id, url, progress=progress)
                html_path = result.output_dir / "html-css" / "index.html"
                pstore.record_snapshot(
                    telegram_user_id=user_id,
                    url=result.source_url,
                    mode="rebuild",
                    output_dir=result.output_dir,
                    screenshot_path=result.preview,
                    html_path=html_path if html_path.exists() else None,
                    content_hash=_sha(html_path) if html_path.exists() else None,
                    metadata={"formats": list(result.formats), "colors": list(result.colors), "fonts": list(result.fonts)},
                )
                await _finish_text(app, chat_id, message_id, mode, result.summary)
                if result.preview:
                    await _send_photo_or_document(app, chat_id, result.preview, "🧱 Prévia da base reconstruída")
                if result.bundle:
                    await _send_document(app, chat_id, result.bundle, "rebuild-export.zip", "📦 HTML/CSS + React + Next.js + Tailwind")

            elif mode in {"modernize", "inspire"}:
                result = await app.bot_data["premium_redesign"].build(
                    operation_id,
                    url,
                    mode=mode,
                    progress=progress,
                )
                pstore.record_snapshot(
                    telegram_user_id=user_id,
                    url=result.source_url,
                    mode=mode,
                    output_dir=result.output_dir,
                    screenshot_path=result.preview,
                    html_path=result.html,
                    content_hash=_sha(result.html),
                    metadata={"used_ai": result.used_ai},
                )
                await _finish_text(app, chat_id, message_id, mode, result.summary)
                if result.preview:
                    await _send_photo_or_document(app, chat_id, result.preview, "✨ Prévia da nova composição")
                await _send_document(app, chat_id, result.html, "index.html", "📄 HTML editável")

            else:
                raise RuntimeError(f"modo premium desconhecido: {mode}")

        if credit_operation_id is not None:
            pstore.finish_operation(credit_operation_id, True)

    except Exception as exc:
        if credit_operation_id is not None:
            try:
                pstore.finish_operation(credit_operation_id, False)
            except Exception:
                logger.exception("failed to refund premium credits for operation %s", credit_operation_id)
        logger.exception("premium operation %s %s failed", mode, operation_id)
        detail = f"\n\n<code>{html.escape(str(exc)[:700])}</code>" if user_id in settings.admin_ids else ""
        try:
            await app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=(
                    f"❌ <b>{html.escape(_label(mode))} não concluído</b>\n\n"
                    "O processamento encontrou um erro técnico. Tente novamente em alguns instantes."
                    + detail
                ),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except TelegramError:
            pass


def _record_audit(store: PremiumStorage, user_id: int, result) -> None:
    content_hash = None
    try:
        data = json.loads(result.report_json.read_text(encoding="utf-8"))
        content_hash = data.get("content_sha256")
    except Exception:
        pass
    store.record_snapshot(
        telegram_user_id=user_id,
        url=result.source_url,
        mode="audit",
        output_dir=result.output_dir,
        screenshot_path=result.screenshot,
        pdf_path=result.pdf,
        warc_path=result.warc,
        markdown_path=result.markdown,
        content_hash=content_hash,
        technologies=result.technologies,
        metadata={
            "scores": {
                "performance": result.score_performance,
                "seo": result.score_seo,
                "accessibility": result.score_accessibility,
                "security": result.score_security,
            }
        },
    )


async def _finish_text(app, chat_id: int, message_id: int, mode: str, summary: str) -> None:
    await app.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=(
            f"✅ <b>{html.escape(_label(mode))} concluído</b>\n\n"
            "<code>██████████</code> <b>100%</b>\n\n"
            f"{html.escape(summary)}"
        ),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def _send_document(app, chat_id: int, path: Path, filename: str, caption: str) -> None:
    if not path or not path.exists():
        return
    try:
        with path.open("rb") as fp:
            await app.bot.send_document(chat_id=chat_id, document=fp, filename=filename, caption=caption)
    except TelegramError as exc:
        logger.warning("document delivery failed for %s: %s", path, exc)


async def _send_photo_or_document(app, chat_id: int, path: Path, caption: str) -> None:
    if not path or not path.exists():
        return
    preview = _safe_preview(path)
    try:
        with preview.open("rb") as fp:
            await app.bot.send_photo(chat_id=chat_id, photo=fp, caption=caption)
    except TelegramError:
        try:
            with path.open("rb") as fp:
                await app.bot.send_document(chat_id=chat_id, document=fp, filename=path.name, caption=caption)
        except TelegramError as exc:
            logger.warning("preview delivery failed for %s: %s", path, exc)


def _safe_preview(path: Path) -> Path:
    target = path.with_name(path.stem + "-tg.jpg")
    try:
        with Image.open(path) as image:
            image = image.convert("RGB")
            if image.width > 1280:
                ratio = 1280 / image.width
                image = image.resize((1280, max(1, int(image.height * ratio))))
            if image.height > 1800:
                image = image.crop((0, 0, image.width, 1800))
            image.save(target, "JPEG", quality=85, optimize=True)
        return target
    except Exception:
        return path


def _upsert(update: Update, storage: Storage):
    user = update.effective_user
    if user is None:
        raise RuntimeError("usuário Telegram ausente")
    return storage.upsert_user(user.id, user.username, user.first_name)


def _allowed(plan: str, mode: str, user_id: int, settings: Settings) -> bool:
    if user_id in settings.admin_ids:
        return True
    if mode in AGENCY_MODES:
        return plan == "agency"
    if mode in PRO_MODES:
        return plan in {"pro", "agency"}
    return False


async def _locked(update: Update, agency: bool = False) -> None:
    target = "Agency" if agency else "Pro ou Agency"
    await update.effective_message.reply_text(
        f"🔒 Este recurso faz parte do plano <b>{target}</b>.",
        parse_mode=ParseMode.HTML,
    )


def _prompt(mode: str) -> str:
    prompts = {
        "audit": "🧪 Envie a URL para auditoria completa: Lighthouse, SEO, WCAG, segurança, links e tecnologias.",
        "multipage": "🕷 Envie a URL inicial. Vou descobrir e clonar páginas internas do mesmo domínio.",
        "singlefile": "📄 Envie a URL para gerar um único HTML com os assets incorporados.",
        "gallery": "🖼 Envie a URL para montar uma galeria visual de imagens, ícones e assets.",
        "tech": "🧠 Envie a URL para detectar framework, CMS, CDN, analytics, bibliotecas e infraestrutura.",
        "compare": "🔄 Envie a URL. Vou criar uma nova versão e comparar com a última salva.",
        "rebuild": "🧱 Envie a URL para exportar HTML/CSS, React/Vite, Next.js e Tailwind.",
        "modernize": "✨ Envie a URL para criar uma versão modernizada e editável.",
        "inspire": "💡 Envie a URL de referência para criar uma nova composição inspirada nela.",
        "versions": "🕘 Envie uma URL para filtrar o histórico ou use /versoes sem argumentos.",
    }
    return prompts.get(mode, "🌐 Envie a URL.")


def _premium_cost(mode: str, plan: str, settings: Settings) -> int:
    fixed = {
        "tech": 1,
        "singlefile": 2,
        "gallery": 2,
        "compare": 2,
        "audit": 4,
        "rebuild": 6,
        "modernize": 8,
        "inspire": 8,
    }
    if mode == "multipage":
        return max(2, settings.clone_pages(plan))
    return fixed.get(mode, 1)


def _label(mode: str) -> str:
    return {
        "audit": "Auditoria completa",
        "multipage": "Clone multipágina",
        "singlefile": "HTML único",
        "gallery": "Galeria de assets",
        "tech": "Detecção de tecnologias",
        "compare": "Comparação de versões",
        "rebuild": "Reconstrução editável",
        "modernize": "Modernização",
        "inspire": "Projeto inspirado",
    }.get(mode, mode)


def _render_progress(
    mode: str,
    url: str,
    percent: int,
    stage: str,
    elapsed: int,
    detail: str | None,
) -> str:
    percent = max(0, min(100, int(percent)))
    filled = max(0, min(10, round(percent / 10)))
    bar = "█" * filled + "░" * (10 - filled)
    host = urlsplit(url).netloc or url
    detail_line = f"\n📌 {html.escape(detail)}" if detail else ""
    return (
        f"💎 <b>{html.escape(_label(mode))}</b>\n"
        f"🌐 {html.escape(host)}\n\n"
        f"<code>{bar}</code> <b>{percent}%</b>\n\n"
        f"⚙️ <b>{html.escape(stage)}</b>{detail_line}\n"
        f"⏱ Decorrido: <b>{_duration(elapsed)}</b>"
    )


def _duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    return f"{minutes}m {sec:02d}s"


def _tech_summary(result) -> str:
    if not result.matches:
        return "🧠 Nenhuma tecnologia foi identificada pelos fingerprints disponíveis."
    lines = [f"🧠 Tecnologias detectadas: {len(result.matches)}", ""]
    for item in result.matches[:35]:
        cats = ", ".join(item.categories[:2])
        version = f" {item.version}" if item.version else ""
        suffix = f" · {cats}" if cats else ""
        lines.append(f"• {item.name}{version}{suffix}")
    if len(result.matches) > 35:
        lines.append(f"… e mais {len(result.matches) - 35}")
    return "\n".join(lines)


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _P:
    def __init__(self, percent: int, stage: str, elapsed_seconds: int) -> None:
        self.percent = percent
        self.stage = stage
        self.elapsed_seconds = elapsed_seconds
        self.detail = None
