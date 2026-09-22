from __future__ import annotations

import asyncio
import html
import logging
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .analyzer import AnalysisArtifacts, DesignAnalyzer
from .config import Settings
from .manager import AnalysisManager
from .progress_ui import render_capture_progress, render_capture_queued, render_progress, render_queued
from .security import UnsafeUrl, validate_public_url
from .storage import Job, QuotaExceeded, Storage
from .webclone import WebsiteCapture

logger = logging.getLogger(__name__)

STATUS_LABEL = {
    "queued": "🕓 Na fila",
    "running": "🔎 Analisando",
    "completed": "✅ Concluída",
    "failed": "❌ Falhou",
    "cancelled": "🚫 Cancelada",
}
PLAN_LABEL = {"free": "Free", "pro": "Pro", "agency": "Agency"}


def menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Analisar site", callback_data="analyze")],
        [InlineKeyboardButton("🧬 Clonar página", callback_data="clone"), InlineKeyboardButton("📦 Extrair assets", callback_data="assets")],
        [InlineKeyboardButton("📊 Meu plano", callback_data="plan"), InlineKeyboardButton("📋 Histórico", callback_data="history")],
        [InlineKeyboardButton("❓ Ajuda", callback_data="help")],
    ])


def _services(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    return app.bot_data["settings"], app.bot_data["storage"], app.bot_data["manager"]


def _upsert(update: Update, storage: Storage):
    user = update.effective_user
    if user is None:
        raise RuntimeError("missing Telegram user")
    return storage.upsert_user(user.id, user.username, user.first_name)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, storage, _ = _services(context)
    _upsert(update, storage)
    text = (
        "🎨 <b>Design Analyzer</b>\n\n"
        "Analise design systems, extraia imagens/fontes e gere clones offline seguros de páginas públicas.\n\n"
        "Escolha uma opção abaixo para começar."
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=menu())


async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        await _submit_url(update, context, " ".join(context.args))
        return
    context.user_data["awaiting_url"] = True
    await update.effective_message.reply_text("🌐 Envie o link do site que deseja analisar.\n\nExemplo: <code>https://www.aniquim.com.br</code>", parse_mode=ParseMode.HTML)


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    capture_mode = context.user_data.pop("awaiting_capture", None)
    if capture_mode in {"clone", "assets"}:
        await _start_capture(update, context, text, capture_mode)
        return
    if context.user_data.pop("awaiting_url", False) or "." in text:
        await _submit_url(update, context, text)
        return
    await update.effective_message.reply_text("Use uma das opções abaixo.", reply_markup=menu())


async def clone_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        await _start_capture(update, context, " ".join(context.args), "clone")
        return
    context.user_data["awaiting_capture"] = "clone"
    await update.effective_message.reply_text(
        "🧬 Envie a URL da página pública que deseja salvar offline.\n\n"
        "O clone preserva o visual e os assets, mas desativa scripts, formulários, login e checkout."
    )


async def assets_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        await _start_capture(update, context, " ".join(context.args), "assets")
        return
    context.user_data["awaiting_capture"] = "assets"
    await update.effective_message.reply_text(
        "📦 Envie a URL do site para extrair imagens, fontes, ícones e folhas de estilo."
    )


async def _start_capture(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    raw_url: str,
    mode: str,
) -> None:
    settings, storage, _ = _services(context)
    user = _upsert(update, storage)

    if user.plan == "free" and user.telegram_user_id not in settings.admin_ids:
        await update.effective_message.reply_text(
            "🔒 Clonagem e extração de assets estão disponíveis nos planos <b>Pro</b> e <b>Agency</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        normalized = (await validate_public_url(raw_url)).url
    except UnsafeUrl as exc:
        await update.effective_message.reply_text(f"⚠️ {html.escape(str(exc))}")
        return

    capture = context.application.bot_data["capture"]
    semaphore = context.application.bot_data["capture_semaphore"]
    capture_id = uuid.uuid4().hex[:10]
    message = await update.effective_message.reply_text(
        render_capture_queued(mode, normalized),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

    async def runner() -> None:
        last_edit = 0.0

        async def progress_cb(progress) -> None:
            nonlocal last_edit
            now = time.monotonic()
            important = progress.percent in {3, 8, 12, 24, 34, 80, 87, 94, 100}
            if not important and now - last_edit < 6:
                return
            try:
                await context.bot.edit_message_text(
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    text=render_capture_progress(mode, normalized, progress),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                last_edit = now
            except BadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    logger.warning("capture progress edit failed: %s", exc)
            except TelegramError as exc:
                logger.warning("capture progress publish failed: %s", exc)

        try:
            async with semaphore:
                artifacts = await capture.capture(
                    capture_id,
                    normalized,
                    mode,
                    progress=progress_cb,
                )

            final_text = (
                ("🧬 <b>Clone offline concluído</b>\n\n" if mode == "clone" else "📦 <b>Extração concluída</b>\n\n")
                + f"<code>██████████</code> <b>100%</b>\n\n"
                + html.escape(artifacts.summary)
            )
            await context.bot.edit_message_text(
                chat_id=message.chat_id,
                message_id=message.message_id,
                text=final_text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

            if mode == "clone" and artifacts.clone_screenshot and artifacts.clone_screenshot.exists():
                with artifacts.clone_screenshot.open("rb") as fp:
                    await context.bot.send_photo(
                        chat_id=message.chat_id,
                        photo=fp,
                        caption="🖼 Prévia do clone offline",
                    )

            if artifacts.bundle and artifacts.bundle.exists():
                with artifacts.bundle.open("rb") as fp:
                    await context.bot.send_document(
                        chat_id=message.chat_id,
                        document=fp,
                        filename=artifacts.bundle.name,
                        caption=(
                            "🧬 HTML + CSS + imagens + fontes + manifest + screenshots"
                            if mode == "clone"
                            else "📦 Imagens + fontes + ícones + CSS + manifest"
                        ),
                    )
            else:
                with artifacts.manifest.open("rb") as fp:
                    await context.bot.send_document(
                        chat_id=message.chat_id,
                        document=fp,
                        filename="manifest.json",
                        caption="⚠️ O ZIP excedeu o limite de envio; segue o manifest da captura.",
                    )
        except asyncio.TimeoutError:
            await context.bot.edit_message_text(
                chat_id=message.chat_id,
                message_id=message.message_id,
                text="❌ <b>Captura interrompida</b>\n\nA página excedeu o tempo máximo de processamento.",
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.exception("capture %s failed", capture_id)
            await context.bot.edit_message_text(
                chat_id=message.chat_id,
                message_id=message.message_id,
                text=(
                    "❌ <b>Não consegui concluir a captura.</b>\n\n"
                    "O site pode bloquear automação, exigir autenticação ou carregar recursos incompatíveis."
                ),
                parse_mode=ParseMode.HTML,
            )

    context.application.create_task(runner(), name=f"capture-{mode}-{capture_id}")


async def _submit_url(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_url: str) -> None:
    settings, storage, manager = _services(context)
    user = _upsert(update, storage)
    try:
        if settings.analyzer_mock:
            from .security import normalize_url
            normalized = normalize_url(raw_url)
        else:
            normalized = (await validate_public_url(raw_url)).url
    except UnsafeUrl as exc:
        await update.effective_message.reply_text(f"⚠️ {html.escape(str(exc))}")
        return

    plan = user.plan
    pages = settings.plan_pages(plan)
    limit = settings.plan_limit(plan)
    try:
        job = storage.create_job_with_quota(
            telegram_user_id=user.telegram_user_id,
            chat_id=update.effective_chat.id,
            url=normalized,
            pages=pages,
            limit=limit,
        )
    except QuotaExceeded as exc:
        await update.effective_message.reply_text(
            f"📊 Você já usou <b>{exc.used}/{exc.limit}</b> análises do plano <b>{PLAN_LABEL.get(exc.plan, exc.plan)}</b> neste mês.",
            parse_mode=ParseMode.HTML,
        )
        return

    position = manager.approximate_position()
    estimate = storage.estimate_duration_seconds(job.plan, job.pages)
    message = await update.effective_message.reply_text(
        render_queued(job, position, estimate),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    storage.set_progress_message(job.id, message.message_id)
    manager.enqueue(job.id)


async def plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, storage, _ = _services(context)
    user = _upsert(update, storage)
    used = storage.usage_this_month(user.telegram_user_id)
    limit = settings.plan_limit(user.plan)
    await update.effective_message.reply_text(
        f"📊 <b>Seu plano: {PLAN_LABEL.get(user.plan, user.plan)}</b>\n\n"
        f"Análises neste mês: <b>{used}/{limit}</b>\n"
        f"Páginas internas por análise: <b>{settings.plan_pages(user.plan)}</b>",
        parse_mode=ParseMode.HTML,
    )


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, storage, _ = _services(context)
    user = _upsert(update, storage)
    jobs = storage.recent_jobs(user.telegram_user_id, 8)
    if not jobs:
        await update.effective_message.reply_text("📋 Você ainda não fez nenhuma análise.")
        return
    lines = ["📋 <b>Suas análises</b>", ""]
    for job in jobs:
        lines.append(f"<b>#{job.id}</b> · {STATUS_LABEL.get(job.status, job.status)}")
        lines.append(html.escape(job.url))
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, storage, manager = _services(context)
    user = _upsert(update, storage)
    jobs = storage.recent_jobs(user.telegram_user_id, 1)
    if not jobs:
        await update.effective_message.reply_text("Nenhuma análise encontrada.")
        return
    job = jobs[0]
    if context.args and context.args[0].isdigit():
        try:
            candidate = storage.get_job(int(context.args[0]))
        except KeyError:
            await update.effective_message.reply_text("Análise não encontrada.")
            return
        if candidate.telegram_user_id != user.telegram_user_id:
            await update.effective_message.reply_text("Análise não encontrada.")
            return
        job = candidate
    progress = manager.get_progress(job.id)
    if job.status == "running" and progress is not None:
        msg = render_progress(job, progress)
    elif job.status == "queued":
        msg = render_queued(job, 1, storage.estimate_duration_seconds(job.plan, job.pages))
    else:
        msg = f"{STATUS_LABEL.get(job.status, job.status)} <b>Análise #{job.id}</b>\n🌐 {html.escape(job.url)}"
        if job.error and job.status == "failed":
            msg += "\n\nA análise falhou. Tente novamente ou use outro endereço."
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, storage, _ = _services(context)
    user = _upsert(update, storage)
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Use <code>/cancelar ID</code>. Ex.: <code>/cancelar 12</code>", parse_mode=ParseMode.HTML)
        return
    job_id = int(context.args[0])
    ok = storage.cancel_queued(job_id, user.telegram_user_id)
    if ok:
        job = storage.get_job(job_id)
        if job.progress_message_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=job.chat_id,
                    message_id=job.progress_message_id,
                    text=f"🚫 <b>Análise #{job.id} cancelada</b>\n\n🌐 {html.escape(job.url)}",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except TelegramError:
                pass
    await update.effective_message.reply_text("🚫 Análise cancelada." if ok else "Não encontrei uma análise sua que ainda esteja na fila.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "❓ <b>Como usar</b>\n\n"
        "<code>/analisar site.com</code> — nova análise\n"
        "<code>/status</code> — status e progresso da última\n"
        "<code>/historico</code> — análises recentes\n"
        "<code>/plano</code> — uso mensal\n"
        "<code>/cancelar ID</code> — cancela uma análise ainda na fila\n"
        "<code>/id</code> — mostra seu ID do Telegram",
        parse_mode=ParseMode.HTML,
    )


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(f"🆔 Seu ID: <code>{update.effective_user.id}</code>", parse_mode=ParseMode.HTML)


async def admin_setplan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, storage, _ = _services(context)
    if update.effective_user.id not in settings.admin_ids:
        return
    if len(context.args) != 2 or not context.args[0].isdigit() or context.args[1] not in {"free", "pro", "agency"}:
        await update.effective_message.reply_text("Uso: /setplan USER_ID free|pro|agency")
        return
    user_id = int(context.args[0])
    try:
        storage.set_plan(user_id, context.args[1])
    except KeyError:
        await update.effective_message.reply_text("Usuário ainda não iniciou o bot.")
        return
    await update.effective_message.reply_text(f"✅ Plano de {user_id} alterado para {context.args[1]}.")


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = query.data
    if action == "analyze":
        context.user_data["awaiting_url"] = True
        await query.message.reply_text("🌐 Envie o link do site que deseja analisar.")
    elif action == "plan":
        await plan(update, context)
    elif action == "history":
        await history(update, context)
    elif action == "help":
        await help_command(update, context)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled Telegram update error", exc_info=context.error)


def create_application(settings: Settings) -> Application:
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN não configurado. Adicione apenas quando for ligar o bot real.")
    storage = Storage(settings.database_path)
    analyzer = DesignAnalyzer(settings)

    application = Application.builder().token(settings.bot_token).build()
    last_progress_edit: dict[int, float] = {}

    async def progress_notify(job: Job, progress) -> None:
        if not job.progress_message_id:
            try:
                created = await application.bot.send_message(
                    chat_id=job.chat_id,
                    text=render_progress(job, progress),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                storage.set_progress_message(job.id, created.message_id)
                last_progress_edit[job.id] = time.monotonic()
                return
            except TelegramError as exc:
                logger.warning("could not create progress message for job %s: %s", job.id, exc)
                return
        now = time.monotonic()
        previous = last_progress_edit.get(job.id, 0.0)
        important = progress.percent in {1, 3, 8, 12, 91, 95, 98, 100}
        if not important and now - previous < 7:
            return
        try:
            await application.bot.edit_message_text(
                chat_id=job.chat_id,
                message_id=job.progress_message_id,
                text=render_progress(job, progress),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            last_progress_edit[job.id] = now
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.warning("could not edit progress for job %s: %s", job.id, exc)
        except TelegramError as exc:
            logger.warning("could not publish progress for job %s: %s", job.id, exc)

    async def notify(job: Job, artifacts: AnalysisArtifacts | None, error: str | None) -> None:
        last_progress_edit.pop(job.id, None)
        if error:
            text = (
                f"❌ <b>Análise #{job.id} não foi concluída</b>\n\n"
                f"🌐 {html.escape(job.url)}\n\n"
                "O processamento falhou. Você pode tentar novamente; falhas não consomem sua cota mensal."
            )
            if job.progress_message_id:
                try:
                    await application.bot.edit_message_text(
                        chat_id=job.chat_id,
                        message_id=job.progress_message_id,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                    return
                except TelegramError:
                    pass
            await application.bot.send_message(chat_id=job.chat_id, text=text, parse_mode=ParseMode.HTML)
            return

        assert artifacts is not None
        final_text = (
            f"✅ <b>Análise #{job.id} concluída — 100%</b>\n\n"
            "<code>██████████</code> <b>100%</b>\n\n"
            f"{html.escape(artifacts.summary)}\n\n"
            "📦 Preparando os arquivos para envio…"
        )
        if job.progress_message_id:
            try:
                await application.bot.edit_message_text(
                    chat_id=job.chat_id,
                    message_id=job.progress_message_id,
                    text=final_text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except TelegramError:
                await application.bot.send_message(chat_id=job.chat_id, text=final_text, parse_mode=ParseMode.HTML)
        else:
            await application.bot.send_message(chat_id=job.chat_id, text=final_text, parse_mode=ParseMode.HTML)

        max_bytes = settings.max_result_mb * 1024 * 1024
        if artifacts.pdf and artifacts.pdf.exists() and artifacts.pdf.stat().st_size <= max_bytes:
            with artifacts.pdf.open("rb") as fp:
                await application.bot.send_document(
                    chat_id=job.chat_id,
                    document=fp,
                    filename=f"design-system-{job.id}.pdf",
                    caption="📄 Relatório visual completo",
                )
        if artifacts.bundle and artifacts.bundle.exists():
            with artifacts.bundle.open("rb") as fp:
                await application.bot.send_document(
                    chat_id=job.chat_id,
                    document=fp,
                    filename=f"design-analysis-{job.id}.zip",
                    caption="📦 Tokens, CSS, Tailwind, componentes e arquivos técnicos",
                )

    manager = AnalysisManager(
        storage,
        analyzer,
        settings.max_concurrent_analyses,
        notify,
        progress_notify,
    )
    application.bot_data.update(settings=settings, storage=storage, manager=manager)

    async def post_init(app: Application) -> None:
        await manager.start()
        await app.bot.set_my_commands([
            ("start", "Abrir o Design Analyzer"),
            ("analisar", "Analisar um site"),
            ("status", "Status e progresso"),
            ("historico", "Minhas análises"),
            ("plano", "Meu plano e uso"),
            ("ajuda", "Como usar"),
            ("id", "Meu ID"),
        ])

    async def post_shutdown(app: Application) -> None:
        await manager.stop()

    application.post_init = post_init
    application.post_shutdown = post_shutdown
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("analisar", analyze_command))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("historico", history))
    application.add_handler(CommandHandler("plano", plan))
    application.add_handler(CommandHandler("cancelar", cancel))
    application.add_handler(CommandHandler("ajuda", help_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler("setplan", admin_setplan))
    application.add_handler(CallbackQueryHandler(callbacks))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    application.add_error_handler(error_handler)
    return application
