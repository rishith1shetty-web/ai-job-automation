"""Stop Automation: one active apply batch, one cancellation token, checked at safe points.

Apply Jobs runs in a FastAPI background task while the user watches a status card. Until now
the only way to stop a run that was going wrong was to kill the process, which lost the batch
summary and left the browser in an unknown state.

The model here is deliberately small:

    * exactly ONE apply batch may be active per backend process (``AutomationController``);
    * that batch owns ONE ``CancellationToken`` -- a ``threading.Event`` behind a lock, so the
      HTTP request thread that asks to stop and the worker thread that is driving the browser
      can safely touch it at the same time;
    * the worker never cancels mid-action. It calls :meth:`CancellationToken.check` at named
      CHECKPOINTS between actions; a checkpoint raises :class:`AutomationCancelled`, and the
      currently running browser call is always allowed to return first.

Cancellation is enforced in the BACKEND. The Stop button is only a request: whether a job is
still submitted is decided by the checkpoint immediately before the final submit click, not by
any frontend state.

Nothing here knows about LinkedIn or Dice. Both adapters take a token and call ``check`` at the
same named checkpoints, so a stop behaves identically on either portal.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime

# Batch-level lifecycle. `status` on ApplyTopBatch is a plain string column, so these need no
# schema change and no enum migration.
BATCH_STATUS_STOPPING = "stopping"
BATCH_STATUS_STOPPED = "stopped"

# Per-job outcome recorded in ApplyTopBatch.job_results (JSONB, free-form -- it already carries
# non-ApplicationStatus values such as "reached_review"). A job that was interrupted or never
# started is STOPPED, never failed: nothing went wrong, the user asked us to stop.
JOB_RESULT_STOPPED = "stopped"

# Controller states, exposed to the dashboard.
AUTOMATION_IDLE = "idle"
AUTOMATION_RUNNING = "running"
AUTOMATION_STOPPING = "stopping"


class AutomationCancelled(Exception):
    """Raised at a checkpoint when the user has asked the active batch to stop.

    This is control flow, not a failure. Every layer that catches broad exceptions to record a
    FAILED outcome must let this one through."""

    def __init__(self, checkpoint: str = "", batch_id: str | None = None) -> None:
        self.checkpoint = checkpoint
        self.batch_id = batch_id
        super().__init__(f"Automation stopped by the user at checkpoint '{checkpoint or 'unknown'}'.")


class BatchAlreadyActive(RuntimeError):
    """A second apply batch was requested while one is still running or stopping."""

    def __init__(self, batch_id: str, state: str) -> None:
        self.batch_id = batch_id
        self.state = state
        super().__init__(f"Apply batch {batch_id} is already {state}.")


@dataclass
class CancellationToken:
    """One batch's stop signal. Safe to set from one thread and read from another."""

    batch_id: str
    _event: threading.Event = field(default_factory=threading.Event, repr=False)
    requested_at: datetime | None = None
    requested_checkpoint: str | None = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> bool:
        """Ask this batch to stop. Idempotent; returns True the first time only."""
        if self._event.is_set():
            return False
        self.requested_at = datetime.utcnow()
        self._event.set()
        return True

    def check(self, checkpoint: str) -> None:
        """A safe stopping point. Raises :class:`AutomationCancelled` if a stop was requested.

        Call this BETWEEN browser actions, never inside one. The action already in flight is
        allowed to finish and return -- interrupting Playwright mid-call would leave the page
        and the trace in an unknown state, which is exactly what a graceful stop avoids."""
        if self._event.is_set():
            self.requested_checkpoint = checkpoint
            raise AutomationCancelled(checkpoint, self.batch_id)

    def wait(self, timeout: float) -> bool:
        """Block until cancelled or the timeout expires. Used by the graceful-stop deadline."""
        return self._event.wait(timeout)


def check_cancelled(token: CancellationToken | None, checkpoint: str) -> None:
    """Checkpoint helper for callers that may or may not have a token (single-job applies,
    tests, and every pre-existing call site) -- ``None`` simply never cancels."""
    if token is not None:
        token.check(checkpoint)


class AutomationController:
    """The single active apply batch for this process.

    Enforces one-at-a-time: a second Apply Jobs batch cannot start while one is running OR
    stopping, so a stop can never race a fresh batch into the same browser."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: CancellationToken | None = None
        self._stopping = False

    # lifecycle ------------------------------------------------------------------------
    def start(self, batch_id: str) -> CancellationToken:
        """Claim the automation slot for ``batch_id``. Raises :class:`BatchAlreadyActive`."""
        with self._lock:
            if self._token is not None:
                raise BatchAlreadyActive(
                    self._token.batch_id,
                    AUTOMATION_STOPPING if self._stopping else AUTOMATION_RUNNING,
                )
            self._token = CancellationToken(batch_id=batch_id)
            self._stopping = False
            return self._token

    def finish(self, batch_id: str) -> None:
        """Release the slot. Ignores a stale id so a late finish cannot free a newer batch."""
        with self._lock:
            if self._token is not None and self._token.batch_id == batch_id:
                self._token = None
                self._stopping = False

    def request_stop(self, batch_id: str | None = None) -> bool:
        """Ask the active batch to stop. ``batch_id`` scopes the request: a stop aimed at a
        batch that is no longer active is ignored rather than stopping the current one.

        Returns True if this call is what moved the batch into stopping."""
        with self._lock:
            token = self._token
            if token is None:
                return False
            if batch_id is not None and token.batch_id != batch_id:
                return False
            first = token.cancel()
            self._stopping = True
            return first

    # inspection -----------------------------------------------------------------------
    @property
    def active_batch_id(self) -> str | None:
        with self._lock:
            return self._token.batch_id if self._token else None

    @property
    def state(self) -> str:
        with self._lock:
            if self._token is None:
                return AUTOMATION_IDLE
            return AUTOMATION_STOPPING if self._stopping else AUTOMATION_RUNNING

    def is_active(self) -> bool:
        """True while a batch is running OR stopping -- the window in which a new one is refused."""
        return self.state != AUTOMATION_IDLE

    def token_for(self, batch_id: str) -> CancellationToken | None:
        with self._lock:
            if self._token is not None and self._token.batch_id == batch_id:
                return self._token
            return None

    def is_cancelled(self, batch_id: str) -> bool:
        token = self.token_for(batch_id)
        return token is not None and token.cancelled

    def snapshot(self) -> dict:
        """What the dashboard polls: is anything running, and has a stop been asked for."""
        with self._lock:
            token = self._token
            return {
                "state": AUTOMATION_IDLE if token is None else (
                    AUTOMATION_STOPPING if self._stopping else AUTOMATION_RUNNING
                ),
                "active_batch_id": token.batch_id if token else None,
                "cancellation_requested": bool(token and token.cancelled),
                "requested_at": token.requested_at if token else None,
            }

    def reset(self) -> None:
        """Drop any active batch. Startup only -- a fresh process has no automation running."""
        with self._lock:
            self._token = None
            self._stopping = False


_controller = AutomationController()


def get_automation_controller() -> AutomationController:
    return _controller


def reset_automation_controller() -> AutomationController:
    """Runs on FastAPI startup, alongside the runtime-session reset: a new process cannot have
    inherited a running batch, and a stale claim would block every future Apply Jobs run."""
    _controller.reset()
    return _controller
