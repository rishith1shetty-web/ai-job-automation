import logging
from logging.config import dictConfig


def configure_logging(force: bool = False) -> None:
    """Send application logs to the console.

    Skipped when the root logger already has handlers, because ``dictConfig`` REPLACES them.
    The packaged launcher opens ``launcher.log`` before importing anything (see
    ``app/bootstrap_log.py``), and ``app/main.py`` calls this at import time -- so without
    the guard, importing the API would throw the launcher's file handler away and every line
    after that point would be lost. In a windowed build it is worse than lost: the console
    handler installed here writes to a ``sys.stdout`` that does not exist.

    Running uvicorn or the tests directly still configures logging normally, because nothing
    has set up the root logger at that point. ``force=True`` overrides the guard for a caller
    that genuinely wants to reconfigure.

    (The same mistake was found and fixed in ``alembic/env.py``; this was the second
    instance of it.)"""
    if not force and logging.getLogger().handlers:
        return
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                }
            },
            "root": {"handlers": ["console"], "level": "INFO"},
        }
    )


logger = logging.getLogger("linkedin_auto_apply")
