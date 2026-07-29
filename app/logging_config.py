import logging
import sys
from logging.config import dictConfig


def _console_handler() -> dict:
    """A stream handler when there is a stream, and a sink when there is not.

    A windowed (GUI subsystem) build has no console, so ``sys.stderr`` is ``None``. A
    ``logging.StreamHandler`` created there resolves its stream lazily and then fails on
    every single record it tries to emit. The guard in :func:`configure_logging` normally
    stops this function running at all in the packaged launcher -- but that guard depends on
    import order, and the same missing-stream assumption is what broke uvicorn's own
    formatter on PC9. One condition removes the assumption instead of relying on it."""
    if sys.stderr is None:
        return {"class": "logging.NullHandler"}
    return {"class": "logging.StreamHandler", "formatter": "default"}


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
            "handlers": {"console": _console_handler()},
            "root": {"handlers": ["console"], "level": "INFO"},
        }
    )


logger = logging.getLogger("linkedin_auto_apply")
