"""Portal-independent pause/resume for automation that needs manual help.

WHY THIS SHAPE
--------------
An apply batch runs as a synchronous FastAPI background task, i.e. on ONE worker thread,
and Playwright's sync API is bound to the thread (greenlet) that created it. A page can
therefore only be touched from the automation thread.

So the pause is implemented as: the automation thread *blocks inside the running apply*,
holding its live Playwright page open, while HTTP request threads only ever SIGNAL it.
Nothing reopens the job, nothing re-drives the browser from a request handler, and no
polling for a reloaded page happens anywhere. When the signal arrives the automation thread
wakes up exactly where it stopped, re-validates the offending field itself (again, on the
automation thread), and either carries on or pauses again.

This module owns no portal knowledge. Portals supply a `validate` callable that re-checks
their own field; the pause/resume lifecycle is identical for LinkedIn, Dice and any future
portal, which get it by using `ManualAssistMixin`.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable


class ManualDecision(str, Enum):
    """What the user chose while automation was paused."""

    CONTINUE = "continue"
    SKIP = "skip"


class ManualOutcome(str, Enum):
    """What the automation should do after asking for manual help."""

    #: No coordinator attached (e.g. apply-selected, or tests) -- caller keeps its existing
    #: behaviour unchanged (needs-review / skip). This is what preserves the old lifecycle.
    UNAVAILABLE = "unavailable"
    #: The user completed the field and re-validation passed -- resume automation.
    COMPLETED = "completed"
    #: The user pressed "Skip This Job" -- close only this application, record unsuccessful.
    SKIPPED = "skipped"


@dataclass
class ManualPauseState:
    """Everything the UI needs to explain the pause. Purely descriptive -- it holds no
    Playwright handles, so it is safe to read from a request thread."""

    portal: str
    job_id: int
    job_title: str
    company: str
    job_url: str
    current_step: str
    question: str
    reason: str
    dry_run: bool
    batch_id: str | None = None
    attempt: int = 1
    #: Monotonic id, bumped for every pause. Lets a client (or a test) tell "this is a NEW
    #: pause with an updated reason" apart from "the old pause is still showing", which
    #: matters after a Continue whose re-validation failed.
    sequence: int = 0
    paused_at: datetime = field(default_factory=datetime.utcnow)

    def as_payload(self) -> dict:
        payload = asdict(self)
        payload["paused_at"] = self.paused_at.isoformat()
        return payload


class ManualAssistCoordinator:
    """Hand-off point between the automation thread and the API threads.

    Exactly one pause can be active at a time, which matches the product rule: the whole
    batch stops on the paused job and no other application is opened.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: ManualPauseState | None = None
        self._decision: ManualDecision | None = None
        self._signal = threading.Event()
        self._sequence = 0
        self._on_pause: Callable[[ManualPauseState], None] | None = None
        self._on_resume: Callable[[ManualDecision], None] | None = None
        self._owner: str | None = None

    def set_listeners(
        self,
        on_pause: Callable[[ManualPauseState], None] | None = None,
        on_resume: Callable[[ManualDecision], None] | None = None,
        owner: str | None = None,
    ) -> None:
        """Register batch-level side effects (e.g. flipping the batch row to "paused").

        Both fire on the AUTOMATION thread -- on_pause just before it blocks, on_resume just
        after it wakes -- so they may safely use the database, and they never run on a
        request thread. `owner` (the batch id) scopes them, see :meth:`release`.
        """
        with self._lock:
            self._on_pause = on_pause
            self._on_resume = on_resume
            self._owner = owner

    def release(self, owner: str) -> None:
        """Detach `owner`'s listeners and drop any pause it left behind.

        Owner-scoped on purpose: a batch that is finishing (or unwinding late) must never
        clear the coordinator out from under a DIFFERENT batch that has already claimed it,
        which would silently disable that batch's pause handling.
        """
        with self._lock:
            if self._owner is not None and self._owner != owner:
                return
        self.clear()
        with self._lock:
            self._on_pause = None
            self._on_resume = None
            self._owner = None

    # -- read side (API threads) --------------------------------------------------------
    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._state is not None

    def snapshot(self) -> dict | None:
        """The current pause, or None. Safe to call from any thread."""
        with self._lock:
            return self._state.as_payload() if self._state else None

    def resolve(self, decision: ManualDecision) -> bool:
        """Signal the paused automation thread. Returns False when nothing is paused.

        This NEVER touches the browser -- it only wakes the automation thread, which then
        re-validates the field itself. That is what keeps the Playwright session legal.
        """
        with self._lock:
            if self._state is None:
                return False
            self._decision = decision
        self._signal.set()
        return True

    # -- write side (automation thread) -------------------------------------------------
    def wait_for_decision(self, state: ManualPauseState) -> ManualDecision:
        """Publish the pause and BLOCK this thread until the user decides.

        Waits indefinitely by design: the product rule is "WAIT FOR USER". The user always
        has an escape hatch (Skip This Job), so a paused batch can never be truly wedged.
        The Playwright page stays open for the whole wait because this thread simply parks
        here rather than unwinding out of the apply.
        """
        with self._lock:
            self._sequence += 1
            state.sequence = self._sequence
            self._state = state
            self._decision = None
            self._signal.clear()
            on_pause = self._on_pause
        if on_pause:
            on_pause(state)
        self._signal.wait()
        with self._lock:
            decision = self._decision or ManualDecision.SKIP
            self._state = None
            self._decision = None
            self._signal.clear()
            on_resume = self._on_resume
        if on_resume:
            on_resume(decision)
        return decision

    def clear(self) -> None:
        """Drop any pause (batch finished/aborted). Releases a waiter as SKIP."""
        with self._lock:
            if self._state is None:
                return
            self._decision = ManualDecision.SKIP
        self._signal.set()


#: Process-wide coordinator. Only one apply batch runs at a time, so a single instance is
#: the whole story; it is injected into the portal adapters rather than imported by them.
manual_assist_coordinator = ManualAssistCoordinator()


class ManualAssistMixin:
    """Gives a portal adapter the pause/resume lifecycle. LinkedIn and Dice both use this,
    so the behaviour cannot drift between portals and a future portal inherits it for free.

    A portal calls :meth:`pause_for_manual_input` at the point where it would otherwise give
    up on a required field, passing a `validate` callable that re-checks THAT field.
    """

    #: Set by the batch runner. None -> the portal keeps its existing behaviour untouched.
    manual_assist: ManualAssistCoordinator | None = None
    #: Set by the batch runner so the pause payload can name the batch.
    manual_assist_batch_id: str | None = None

    def attach_manual_assist(
        self, coordinator: ManualAssistCoordinator | None, batch_id: str | None = None
    ) -> None:
        self.manual_assist = coordinator
        self.manual_assist_batch_id = batch_id

    def pause_for_manual_input(
        self,
        *,
        portal: str,
        job,
        dry_run: bool,
        current_step: str,
        question: str,
        reason: str,
        validate: Callable[[], bool],
        on_event: Callable[[str, str], None] | None = None,
    ) -> ManualOutcome:
        """Stop automation with the browser alive and wait for the user.

        Returns:
          * COMPLETED  -- user fixed the field, re-validation passed: resume from here.
          * SKIPPED    -- user chose Skip This Job.
          * UNAVAILABLE-- no coordinator attached: caller keeps its existing behaviour.

        Re-validation runs on the automation thread (the only thread allowed to touch the
        page). If it still fails, we pause again with an updated reason -- automation never
        silently continues past a field it could not verify.
        """
        coordinator = self.manual_assist
        if coordinator is None:
            return ManualOutcome.UNAVAILABLE

        attempt = 0
        current_reason = reason
        while True:
            attempt += 1
            if on_event:
                on_event("manual_input_required", current_reason)
            decision = coordinator.wait_for_decision(
                ManualPauseState(
                    portal=portal,
                    job_id=getattr(job, "id", 0),
                    job_title=getattr(job, "title", "") or "",
                    company=getattr(job, "company", "") or "",
                    job_url=getattr(job, "job_url", "") or "",
                    current_step=current_step,
                    question=question,
                    reason=current_reason,
                    dry_run=dry_run,
                    batch_id=self.manual_assist_batch_id,
                    attempt=attempt,
                )
            )
            if decision is ManualDecision.SKIP:
                if on_event:
                    on_event("manual_input_skipped", question)
                return ManualOutcome.SKIPPED

            # "Continue Automation": verify the user really completed the field before
            # resuming. This is the resume_after_manual_input step of the lifecycle.
            try:
                completed = bool(validate())
            except Exception as exc:  # a torn-down page, a detached node, ...
                completed = False
                current_reason = f"Could not verify the answer after manual input: {exc}"
            else:
                if completed:
                    if on_event:
                        on_event("manual_input_completed", question)
                    return ManualOutcome.COMPLETED
                current_reason = (
                    "This question still looks incomplete. Please finish it in the open "
                    "browser, then press Continue Automation again."
                )
