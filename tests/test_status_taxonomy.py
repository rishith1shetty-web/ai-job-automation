"""Phase 3 tests: ApplicationStatus taxonomy (dry_run_ready + unsupported_form).

Covers the new enum members, the safe idempotent PostgreSQL enum migration,
backward compatibility, and batch-status mapping.
"""

import unittest

from sqlalchemy import text

from app.database import engine, init_db
from app.main import _batch_job_status
from app.models import ApplicationRecord, ApplicationStatus


class StatusTaxonomyTests(unittest.TestCase):
    def test_new_enum_members_exist(self) -> None:
        self.assertEqual(ApplicationStatus.DRY_RUN_READY.value, "dry_run_ready")
        self.assertEqual(ApplicationStatus.UNSUPPORTED_FORM.value, "unsupported_form")

    def test_backward_compatible_members_preserved(self) -> None:
        for name in ("MATCHED", "QUEUED", "APPLIED", "NEEDS_REVIEW", "SKIPPED", "FAILED"):
            self.assertIn(name, ApplicationStatus.__members__)

    def test_migration_is_idempotent_and_adds_labels(self) -> None:
        # Running the migration twice must not error (ADD VALUE IF NOT EXISTS).
        init_db()
        init_db()
        with engine.connect() as connection:
            labels = {
                row[0]
                for row in connection.execute(
                    text("SELECT unnest(enum_range(NULL::applicationstatus))::text")
                )
            }
        # SQLAlchemy stores enum member NAMES as the Postgres labels.
        self.assertIn("DRY_RUN_READY", labels)
        self.assertIn("UNSUPPORTED_FORM", labels)
        # Existing labels remain (backward compatibility at the DB level).
        for name in ("MATCHED", "QUEUED", "APPLIED", "NEEDS_REVIEW", "SKIPPED", "FAILED"):
            self.assertIn(name, labels)

    def test_batch_status_mapping_for_new_statuses(self) -> None:
        dry_run = ApplicationRecord(status=ApplicationStatus.DRY_RUN_READY)
        unsupported = ApplicationRecord(status=ApplicationStatus.UNSUPPORTED_FORM)
        self.assertEqual(_batch_job_status(dry_run), "reached_review")
        self.assertEqual(_batch_job_status(unsupported), "needs_review")

    def test_batch_status_mapping_unchanged_for_existing_statuses(self) -> None:
        self.assertEqual(_batch_job_status(ApplicationRecord(status=ApplicationStatus.NEEDS_REVIEW)), "needs_review")
        self.assertEqual(_batch_job_status(ApplicationRecord(status=ApplicationStatus.FAILED)), "failed")
        self.assertEqual(_batch_job_status(ApplicationRecord(status=ApplicationStatus.APPLIED)), "applied")
        self.assertEqual(_batch_job_status(ApplicationRecord(status=ApplicationStatus.SKIPPED)), "skipped")


if __name__ == "__main__":
    unittest.main()
