"""Central Windows-safe resolution of every writable runtime location.

Nothing in the application may hard-code a writable path. All mutable runtime data --
diagnostics, logs, PID files, screenshots, uploaded resumes and portal session state --
is resolved here so that a packaged install never writes inside ``Program Files`` or the
install directory, while the existing source checkout keeps working unchanged.

Three resolution modes, highest priority first:

1. ``override``      -- ``AI_JOB_AUTOMATION_DATA_DIR`` names a single root and everything
                        lives under it. This is what tests use to stay out of the real
                        user's application data.
2. ``packaged``      -- a frozen build (``sys.frozen``) or an explicit
                        ``AI_JOB_AUTOMATION_MODE=packaged``. Everything lives under
                        ``%LOCALAPPDATA%\\AIJobAutomation``.
3. ``development``   -- running from a source checkout. The historical project-relative
                        layout is preserved byte-for-byte (``outputs/``, ``work/``,
                        ``storage/``), so an existing dev machine keeps its saved LinkedIn
                        and Dice sessions and its accumulated debug artifacts.

Machine-local by design: ``%LOCALAPPDATA%`` is deliberately the ONLY Windows base. Roaming
application data (``%APPDATA%``) is never used, because a managed Windows environment can
roam or synchronise it between computers -- which would carry resume uploads, application
history, configuration and signed-in LinkedIn/Dice sessions onto a second machine. Every
computer, and every Windows account on a computer, must keep completely separate data, so
there is no shared, networked, roaming or synchronised location anywhere in this module.

Resolution has NO side effects: importing this module, or calling :func:`runtime_paths`,
never creates a directory. Call :meth:`RuntimePaths.ensure` at an explicit startup point.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, fields
from functools import lru_cache
from pathlib import Path

APP_NAME = "AIJobAutomation"

# Single-root override; the tests and any portable install use this.
DATA_DIR_ENV = "AI_JOB_AUTOMATION_DATA_DIR"
# Finer-grained override of the machine-local root, honoured in packaged/development mode.
LOCAL_DIR_ENV = "AI_JOB_AUTOMATION_LOCAL_DATA_DIR"
# Force a mode without freezing the app (used by tests to exercise the packaged layout).
MODE_ENV = "AI_JOB_AUTOMATION_MODE"

MODE_OVERRIDE = "override"
MODE_PACKAGED = "packaged"
MODE_DEVELOPMENT = "development"

# Disposable data. Safe to delete; regenerated on demand.
DISPOSABLE_DIR_NAMES = ("outputs", "logs", "work", "screenshots", "html_diagnostics", "cache")
# Data the user would be upset to lose: portal sessions, uploaded resumes, configuration.
# Persistent does NOT mean roaming -- these are just as machine-local as the disposable ones.
PERSISTENT_DIR_NAMES = ("storage", "uploads", "browser_sessions", "config")

# Portal session-state filenames. Preserved exactly -- these files are written and read by
# Playwright's storage_state and their names/format must not change.
LINKEDIN_STATE_FILENAME = "linkedin_state.json"
DICE_STATE_FILENAME = "dice_state.json"


def project_root() -> Path:
    """The source checkout root (this file lives at ``<root>/app/paths.py``)."""
    return Path(__file__).resolve().parent.parent


def install_root() -> Path:
    """The directory holding the application's BUNDLED, READ-ONLY assets.

    This is deliberately NOT a writable location and NOT the current working directory: the
    app must find its own files no matter where it was launched from. A frozen build unpacks
    to ``sys._MEIPASS`` (one-file) or sits next to the executable (one-folder); a source
    checkout uses the project root."""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return project_root()


# Where ``npm run build`` writes the production React bundle, relative to the install root.
FRONTEND_DIST_PARTS = ("frontend", "dist")


def frontend_dist_dir() -> Path:
    """The built React assets. A bundled asset, so it lives with the installation -- never
    in the per-user data directory and never resolved from the working directory."""
    return install_root().joinpath(*FRONTEND_DIST_PARTS)


@dataclass(frozen=True)
class RuntimePaths:
    """Every writable runtime location, fully resolved. Immutable and side-effect free."""

    mode: str
    data_root: Path
    # --- disposable -----------------------------------------------------------------------
    outputs: Path
    logs: Path
    work: Path
    screenshots: Path
    html_diagnostics: Path
    cache: Path
    # --- persistent (still machine-local, never roaming) -----------------------------------
    storage: Path
    uploads: Path
    browser_sessions: Path
    config: Path

    # --- derived locations ----------------------------------------------------------------
    @property
    def linkedin_state_file(self) -> Path:
        return self.browser_sessions / LINKEDIN_STATE_FILENAME

    @property
    def dice_state_file(self) -> Path:
        return self.browser_sessions / DICE_STATE_FILENAME

    def debug_dir(self, name: str) -> Path:
        """A named diagnostics directory under ``outputs`` (e.g. ``dice_debug``)."""
        return self.outputs / name

    def disposable_directories(self) -> tuple[Path, ...]:
        """Directories a user may clear without losing sessions, uploads or configuration."""
        return tuple(getattr(self, name) for name in DISPOSABLE_DIR_NAMES)

    def persistent_directories(self) -> tuple[Path, ...]:
        """Directories holding data worth keeping. Machine-local, never synchronised."""
        return tuple(getattr(self, name) for name in PERSISTENT_DIR_NAMES)

    def directories(self) -> tuple[Path, ...]:
        """Every directory this layout owns, de-duplicated, parents first."""
        seen: dict[str, Path] = {}
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, Path):
                seen.setdefault(str(value), value)
        return tuple(sorted(seen.values(), key=lambda path: len(path.parts)))

    def ensure(self) -> RuntimePaths:
        """Create every directory in this layout. Idempotent; safe to call repeatedly."""
        for directory in self.directories():
            directory.mkdir(parents=True, exist_ok=True)
        return self


def _env_path(name: str) -> Path | None:
    """A configured directory from the environment, or None when unset/blank."""
    raw = (os.environ.get(name) or "").strip().strip('"')
    if not raw:
        return None
    # expandvars/expanduser so "%LOCALAPPDATA%\Foo" and "~/foo" both work; spaces and
    # non-ASCII characters need no special handling -- Path keeps them verbatim.
    return Path(os.path.expandvars(os.path.expanduser(raw)))


def _default_local_base() -> Path:
    """The machine-local application-data base for this OS, before the app name is added.

    Windows uses ``%LOCALAPPDATA%`` only. ``%APPDATA%`` is intentionally never consulted --
    see the module docstring."""
    home = Path.home()
    if os.name == "nt":
        return _env_path("LOCALAPPDATA") or (home / "AppData" / "Local")
    # POSIX developer/CI machines: the state directory is the local, non-synchronised one.
    return _env_path("XDG_STATE_HOME") or (home / ".local" / "state")


def _development_layout(root: Path) -> dict:
    """The historical source-checkout layout, preserved exactly.

    ``logs`` maps onto ``outputs`` and ``browser_sessions`` onto ``storage`` because that is
    where the launcher and Playwright already write; an existing checkout therefore keeps its
    logs and its signed-in LinkedIn/Dice sessions with no migration step."""
    return {
        "data_root": root,
        "outputs": root / "outputs",
        "logs": root / "outputs",
        "work": root / "work",
        "screenshots": root / "outputs" / "screenshots",
        "html_diagnostics": root / "outputs" / "html_diagnostics",
        "cache": root / "outputs" / "cache",
        "storage": root / "storage",
        "uploads": root / "storage" / "uploads",
        "browser_sessions": root / "storage",
        "config": root / "storage" / "config",
    }


def _local_layout(data_root: Path) -> dict:
    """The packaged layout: every directory directly under one machine-local root."""
    return {
        "data_root": data_root,
        **{name: data_root / name for name in DISPOSABLE_DIR_NAMES},
        **{name: data_root / name for name in PERSISTENT_DIR_NAMES},
    }


def is_packaged() -> bool:
    """True when running from a frozen build, or when the mode is forced to ``packaged``."""
    forced = (os.environ.get(MODE_ENV) or "").strip().lower()
    if forced in {MODE_PACKAGED, MODE_DEVELOPMENT}:
        return forced == MODE_PACKAGED
    return bool(getattr(sys, "frozen", False))


def resolve_runtime_paths() -> RuntimePaths:
    """Resolve the active layout. Pure: reads the environment, creates nothing."""
    override_root = _env_path(DATA_DIR_ENV)
    if override_root is not None:
        return RuntimePaths(mode=MODE_OVERRIDE, **_local_layout(override_root))

    if is_packaged():
        data_root = _env_path(LOCAL_DIR_ENV) or (_default_local_base() / APP_NAME)
        return RuntimePaths(mode=MODE_PACKAGED, **_local_layout(data_root))

    root = _env_path(LOCAL_DIR_ENV) or project_root()
    return RuntimePaths(mode=MODE_DEVELOPMENT, **_development_layout(root))


@lru_cache(maxsize=1)
def runtime_paths() -> RuntimePaths:
    """The process-wide layout. Cached so every caller agrees on one set of directories."""
    return resolve_runtime_paths()


def reset_runtime_paths_cache() -> None:
    """Drop the cached layout so a changed environment is re-read. For tests and launchers."""
    runtime_paths.cache_clear()


def describe() -> dict[str, str]:
    """A JSON-serialisable view of the resolved layout, for the launcher and diagnostics.

    Contains directory paths only -- never configuration values, credentials or secrets."""
    paths = runtime_paths()
    described = {"mode": paths.mode}
    for field in fields(paths):
        value = getattr(paths, field.name)
        if isinstance(value, Path):
            described[field.name] = str(value)
    described["linkedin_state_file"] = str(paths.linkedin_state_file)
    described["dice_state_file"] = str(paths.dice_state_file)
    return described


if __name__ == "__main__":  # pragma: no cover - launcher/diagnostic helper
    import json

    if "--ensure" in sys.argv:
        runtime_paths().ensure()
    print(json.dumps(describe(), indent=2))
