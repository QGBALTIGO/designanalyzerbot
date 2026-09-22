from __future__ import annotations

import logging

from app.bot import create_application
from app.config import Settings


def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Evita que bibliotecas HTTP registrem URLs da Bot API, que contêm o token.
    for noisy_logger in ("httpx", "httpcore", "telegram.request"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    app = create_application(settings)
    app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False)


if __name__ == "__main__":
    main()
