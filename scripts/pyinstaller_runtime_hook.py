"""Runtime hook: runs inside the packaged executable before any application code.

Two things have to be true before ``app.*`` is imported:

1. Playwright must look for Chromium inside the installation, not in the user's global
   ``%LOCALAPPDATA%\\ms-playwright`` cache -- a packaged computer has no such cache, and a
   developer machine has one that must not be used.
2. Alembic must find ``alembic.ini`` and the revision scripts, which PyInstaller unpacks
   next to the executable rather than in the current working directory.

Setting the browser path here (rather than in the launcher) means it is already correct for
any code path that reaches Playwright, including one that bypasses the launcher's own setup.
"""

import os
import sys
from pathlib import Path

BROWSER_ENV = "PLAYWRIGHT_BROWSERS_PATH"


def _install_root() -> Path:
    """Where PyInstaller placed the bundled data files."""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle)
    return Path(sys.executable).resolve().parent


def _configure_browsers(root: Path) -> None:
    # An explicit setting from the environment always wins, so a support engineer can point
    # a packaged build at a different browser directory without rebuilding.
    if os.environ.get(BROWSER_ENV):
        return
    bundled = root / "ms-playwright"
    if bundled.is_dir():
        os.environ[BROWSER_ENV] = str(bundled)


def _configure_alembic(root: Path) -> None:
    # Consumed by app/schema_verification.py when it builds the Alembic Config.
    config = root / "alembic.ini"
    if config.is_file():
        os.environ.setdefault("AI_JOB_AUTOMATION_ALEMBIC_INI", str(config))
        os.environ.setdefault("AI_JOB_AUTOMATION_ALEMBIC_DIR", str(root / "alembic"))


_root = _install_root()
_configure_browsers(_root)
_configure_alembic(_root)
