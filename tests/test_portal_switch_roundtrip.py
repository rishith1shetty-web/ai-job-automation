"""Portal switching: the read path and the write path must agree.

Regression: GET /portal/active was changed to report the in-memory runtime session, but
POST /portal/active/{portal} still wrote ONLY the portal_runtime_state DB row. So a switch
was invisible to the dashboard -- GET kept returning active_portal: null, the Sidebar fell
back to its LinkedIn panel, and the Dice Connect button could never be rendered, making
POST /dice/connect unreachable from the UI.

The earlier tests missed this because they checked the DB write and the session read
SEPARATELY. test_switch_then_get_roundtrip below is the true round-trip: it drives the exact
pair of calls the UI makes (POST the switch, then refetch GET) and asserts they agree.
"""

import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.database import SessionLocal, init_db
from app.main import (
    connect_dice,
    get_active_portal,
    get_active_portal_status,
    startup,
    switch_active_portal,
)
from app.services.runtime_session import get_runtime_session, reset_runtime_session


class _FakeDice:
    def __init__(self, connected: bool = True) -> None:
        self._connected = connected
        self.disconnect_calls = 0
        self.connect_calls = 0

    def connect(self) -> dict:
        self.connect_calls += 1
        return {"status": "connected"}

    def disconnect(self) -> None:
        self.disconnect_calls += 1

    def is_connected(self) -> bool:
        return self._connected


class _FakeLinkedIn:
    disconnect_calls = 0

    def __init__(self, *_a, **_k) -> None:
        pass

    def disconnect_account(self) -> None:
        type(self).disconnect_calls += 1

    def has_authenticated_session(self) -> bool:
        return True


class PortalSwitchRoundTripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        reset_runtime_session()
        _FakeLinkedIn.disconnect_calls = 0

    def tearDown(self) -> None:
        reset_runtime_session()

    def _switch(self, portal: str, dice: _FakeDice | None = None) -> dict:
        dice = dice or _FakeDice()
        with patch("app.main.dice_adapter", return_value=dice), patch(
            "app.main.LinkedInAutomation", _FakeLinkedIn
        ):
            with SessionLocal() as db:
                return switch_active_portal(portal, db)

    # 1. Fresh startup returns null.
    def test_fresh_startup_reports_no_active_portal(self) -> None:
        with patch("app.main.init_db"):
            startup()
        self.assertEqual(
            get_active_portal_status(),
            {"active_portal": None, "linkedin_connected": False, "dice_connected": False},
        )

    # THE round-trip the previous tests missed: POST the switch, then refetch GET.
    def test_switch_then_get_roundtrip(self) -> None:
        for portal in ("dice", "linkedin", "dice"):
            with self.subTest(portal=portal):
                posted = self._switch(portal)
                # What the POST claims...
                self.assertEqual(posted["active_portal"], portal)
                # ...must be what the UI's refetch actually sees.
                self.assertEqual(get_active_portal_status()["active_portal"], portal)

    # 2. Switch to Dice -> GET returns dice.
    def test_switch_to_dice_is_visible_to_get(self) -> None:
        self._switch("dice")
        status = get_active_portal_status()
        self.assertEqual(status["active_portal"], "dice")

    # 3. Switch to LinkedIn -> GET returns linkedin.
    def test_switch_to_linkedin_is_visible_to_get(self) -> None:
        self._switch("dice")
        self._switch("linkedin")
        self.assertEqual(get_active_portal_status()["active_portal"], "linkedin")

    # 4. Switching does not mark the portal connected.
    def test_switch_never_marks_connected(self) -> None:
        self._switch("dice")
        status = get_active_portal_status()
        self.assertEqual(status["active_portal"], "dice")
        self.assertFalse(status["dice_connected"])
        self.assertFalse(status["linkedin_connected"])
        self._switch("linkedin")
        self.assertFalse(get_active_portal_status()["linkedin_connected"])

    # 5. Switching does not delete either portal's saved storage-state file.
    def test_switch_never_deletes_saved_sessions(self) -> None:
        dice = _FakeDice()
        self._switch("dice", dice)
        self._switch("linkedin", dice)
        self.assertEqual(dice.disconnect_calls, 0)
        self.assertEqual(_FakeLinkedIn.disconnect_calls, 0)

    # 6. Switching does not require an active resume.
    def test_switch_requires_no_active_resume(self) -> None:
        self.assertIsNone(get_runtime_session().active_resume_id)
        self._switch("dice")
        self.assertEqual(get_active_portal_status()["active_portal"], "dice")
        self.assertIsNone(get_runtime_session().active_resume_id)

    # 7. Runtime session resets to null after a restart.
    def test_restart_resets_active_portal_to_null(self) -> None:
        self._switch("dice")
        self.assertEqual(get_active_portal_status()["active_portal"], "dice")
        with patch("app.main.init_db"):
            startup()
        self.assertIsNone(get_active_portal_status()["active_portal"])

    # 10. The connect endpoints are unchanged: Connect (not switch) is what connects.
    def test_connect_dice_still_connects_after_a_switch(self) -> None:
        dice = _FakeDice()
        self._switch("dice", dice)
        self.assertFalse(get_active_portal_status()["dice_connected"])
        with patch("app.main.dice_adapter", return_value=dice), patch(
            "app.main.LinkedInAutomation", _FakeLinkedIn
        ):
            result = connect_dice()
        self.assertEqual(result["status"], "connected")
        self.assertEqual(dice.connect_calls, 1)
        status = get_active_portal_status()
        self.assertEqual(status["active_portal"], "dice")
        self.assertTrue(status["dice_connected"])

    def test_switch_still_persists_the_db_preference(self) -> None:
        # Endpoints that must resolve a concrete portal keep reading the DB preference.
        self._switch("dice")
        with SessionLocal() as db:
            self.assertEqual(get_active_portal(db), "dice")

    def test_invalid_portal_is_rejected(self) -> None:
        for bad in ("both", "monster"):
            with self.subTest(portal=bad):
                with self.assertRaises(HTTPException) as ctx:
                    self._switch(bad)
                self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
