from __future__ import annotations

import html
from urllib.parse import urlsplit

from .analyzer import ProgressUpdate
from .storage import Job


def bar(percent: int) -> str:
    percent = max(0, min(100, percent))
    filled = max(0, min(10, round(percent / 10)))
    return "█" * filled + "░" * (10 - filled)


def duration(seconds: int | float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def eta_text(eta_seconds: int | None, percent: int) -> str:
    if percent >= 100:
        return "concluída"
    if eta_seconds is None:
        return "acima da previsão inicial — continuo trabalhando"
    if eta_seconds <= 20:
        return "menos de 1 min"
    low = max(20, int(eta_seconds * 0.75))
    high = max(low + 20, int(eta_seconds * 1.35))
    if high < 120:
        return f"~{duration(low)}–{duration(high)}"
    low_m = max(1, round(low / 60))
    high_m = max(low_m + 1, round(high / 60))
    return f"~{low_m}–{high_m} min"


def site_label(url: str) -> str:
    parsed = urlsplit(url)
    return parsed.netloc or url


def render_progress(job: Job, progress: ProgressUpdate) -> str:
    detail = f"\n📌 {html.escape(progress.detail)}" if progress.detail else ""
    return (
        f"🔎 <b>Análise #{job.id}</b>\n"
        f"🌐 {html.escape(site_label(job.url))}\n\n"
        f"<code>{bar(progress.percent)}</code> <b>{progress.percent}%</b>\n\n"
        f"⚙️ <b>{html.escape(progress.stage)}</b>{detail}\n"
        f"📄 Até {job.pages + 1} páginas\n"
        f"⏱ Decorrido: <b>{duration(progress.elapsed_seconds)}</b>\n"
        f"⌛ Restante estimado: <b>{eta_text(progress.eta_seconds, progress.percent)}</b>\n\n"
        "<i>A previsão é aproximada e melhora conforme o bot acumula análises concluídas.</i>"
    )


def render_queued(job: Job, position: int, estimate_seconds: int) -> str:
    return (
        f"🧪 <b>Análise #{job.id}</b>\n"
        f"🌐 {html.escape(job.url)}\n\n"
        "<code>░░░░░░░░░░</code> <b>0%</b>\n\n"
        f"🕓 <b>Na fila</b> · posição aproximada: {position}\n"
        f"📄 Até {job.pages + 1} páginas\n"
        f"⏳ Previsão inicial: <b>{eta_text(estimate_seconds, 0)}</b>\n\n"
        "Esta mensagem será atualizada automaticamente."
    )
