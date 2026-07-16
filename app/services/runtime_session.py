"""Per-process runtime (active dashboard session) state.

State in this project falls into two clearly separate categories:

1. PERSISTENT database state -- resumes and their analysis, candidate profile, employment
   history, screening answers, search batches, matched jobs, application history and
   successful live APPLIED jobs. This is the long-term record. It is never deleted or
   modified here, and it stays reachable through explicit user actions (re-analysing a
   resume reattaches to its fingerprint family, opening Application History, picking a
   previous batch).

2. TEMPORARY active-session state -- what the dashboard is *currently* working on. That is
   this module. It lives only in memory and is rebuilt from scratch on every backend start,
   so a fresh process always begins with a clean dashboard.

Nothing about the active session may be inferred from persistent data: not the newest
uploaded/analysed resume, not the newest search batch, not the most recently matched jobs,
not a previously chosen portal, and not the presence of a saved Playwright storage-state
file. A portal counts as connected only after the user explicitly clicks Connect in *this*
process.

Saved portal cookies / storage-state files are deliberately left on disk: an explicit
Connect may reuse a still-valid saved session, but their mere existence must never mark a
portal connected or restore a dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The idle/zero progress shape the dashboard shows before anything has run this session.
RUN_STATUS_IDLE = "idle"


def _zero_progress() -> dict:
    return {
        "total": 0,
        "completed": 0,
        "applied": 0,
        "failed": 0,
        "skipped": 0,
        "needs_review": 0,
    }


@dataclass
class RuntimeSession:
    """The active dashboard session for one backend process. Reset on every startup."""

    active_portal: str | None = None
    linkedin_connected: bool = False
    dice_connected: bool = False
    active_resume_id: int | None = None
    active_search_batch_id: str | None = None
    current_matches: list = field(default_factory=list)
    current_apply_queue: list = field(default_factory=list)
    current_progress: dict = field(default_factory=_zero_progress)
    current_run_status: str = RUN_STATUS_IDLE
    current_run_error: str | None = None

    def reset(self) -> None:
        """Return to a clean session. Called on backend startup; touches no persistent data."""
        self.active_portal = None
        self.linkedin_connected = False
        self.dice_connected = False
        self.active_resume_id = None
        self.active_search_batch_id = None
        self.current_matches = []
        self.current_apply_queue = []
        self.current_progress = _zero_progress()
        self.current_run_status = RUN_STATUS_IDLE
        self.current_run_error = None

    # portal -------------------------------------------------------------------------
    def select_portal(self, portal: str | None) -> None:
        """Record an EXPLICIT portal switch: which portal the dashboard is working on.

        Selecting is not connecting. The connected flags are untouched here -- only an
        explicit Connect (mark_connected) may set them -- and no saved storage-state file is
        involved, so a later Connect can still reuse one.
        """
        self.active_portal = (portal or "").strip().lower() or None

    def mark_connected(self, portal: str) -> None:
        """Record an EXPLICIT connect. Only one portal is active at a time, mirroring the
        existing single-active-portal rule."""
        portal = (portal or "").strip().lower()
        self.active_portal = portal
        self.linkedin_connected = portal == "linkedin"
        self.dice_connected = portal == "dice"

    def mark_disconnected(self, portal: str) -> None:
        portal = (portal or "").strip().lower()
        if portal == "linkedin":
            self.linkedin_connected = False
        elif portal == "dice":
            self.dice_connected = False
        if self.active_portal == portal:
            self.active_portal = None

    def is_connected(self, portal: str) -> bool:
        portal = (portal or "").strip().lower()
        if portal == "linkedin":
            return self.linkedin_connected
        if portal == "dice":
            return self.dice_connected
        return False

    # resume -------------------------------------------------------------------------
    def activate_resume(self, resume_id: int) -> None:
        """Make a resume the session's active one (an explicit upload+analyse action).
        Any previous session's batch/matches are dropped so old Find Matches results never
        reappear after re-analysing."""
        self.active_resume_id = resume_id
        self.active_search_batch_id = None
        self.current_matches = []
        self.current_apply_queue = []
        self.current_progress = _zero_progress()
        self.current_run_status = RUN_STATUS_IDLE
        self.current_run_error = None


_session = RuntimeSession()


def get_runtime_session() -> RuntimeSession:
    return _session


def reset_runtime_session() -> RuntimeSession:
    """Wipe the active session. Runs on every FastAPI startup -- never deletes DB rows or
    saved portal storage-state files."""
    _session.reset()
    return _session
