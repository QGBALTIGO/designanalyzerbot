from app.analyzer import ProgressUpdate
from app.progress_ui import bar, duration, eta_text, render_progress, render_queued
from app.storage import Job


def make_job() -> Job:
    return Job(
        id=2,
        telegram_user_id=1,
        chat_id=1,
        url="https://baltigoflix.com.br/",
        plan="agency",
        status="running",
        pages=20,
        created_at="2026-09-22T00:00:00+00:00",
        started_at="2026-09-22T00:00:01+00:00",
        finished_at=None,
        output_dir=None,
        error=None,
        progress_message_id=123,
    )


def test_bar_and_duration():
    assert bar(0) == "░░░░░░░░░░"
    assert bar(50) == "█████░░░░░"
    assert bar(100) == "██████████"
    assert duration(12) == "12s"
    assert duration(75) == "1m 15s"


def test_eta_text_handles_overrun():
    assert "acima da previsão" in eta_text(None, 70)
    assert eta_text(0, 100) == "concluída"


def test_render_progress_contains_key_fields():
    text = render_progress(
        make_job(),
        ProgressUpdate(64, "Analisando páginas e componentes", 130, 180, "3 capturas geradas"),
    )
    assert "64%" in text
    assert "2m 10s" in text
    assert "baltigoflix.com.br" in text
    assert "3 capturas geradas" in text


def test_render_queued_shows_estimate_and_position():
    text = render_queued(make_job(), 2, 300)
    assert "0%" in text
    assert "posição aproximada: 2" in text
    assert "Previsão inicial" in text
