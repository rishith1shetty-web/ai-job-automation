"""LinkedIn Find Matches must publish live batch progress, exactly like Dice already does.

Before: only Dice passed a `progress_callback` to `search_jobs_many`, so a LinkedIn run left the
dashboard at zero until the whole batch finished. Now ONE shared callback in main.py serves both
portals (same contract, same "jobs collected" definition -- each portal's already-filtered
per-variation result, never raw cards), and LinkedIn additionally reports a "started" phase so
the current variation is visible while it is still searching.

Dice's published fields and status message are byte-identical to before.
"""

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import delete, select

from app.config import Settings
from app.database import SessionLocal, init_db
from app.main import _create_search_batch, _run_find_matches_batch, set_active_portal, settings
from app.models import JobOpportunity, JobSearchBatch, ResumeProfile
from app.schemas import SearchBatchOut, SearchRequest
from app.services.find_matches import SearchVariation, per_variation_limits
from app.services.linkedin import LinkedInAutomation

MAIN_SOURCE = Path("app/main.py").read_text(encoding="utf-8", errors="replace")


def _variations(count: int) -> list[SearchVariation]:
    roles = [
        "Workforce Analytics Analyst", "People Analytics Analyst",
        "HR Data Analyst", "HR Reporting Analyst",
        "Compensation Analyst", "Talent Analytics Analyst",
        "HRIS Analyst", "Human Resources Analyst",
    ]
    return [
        SearchVariation(
            query=roles[i], location="Remote", role=roles[i], expanded_role=roles[i],
            pass_name="primary", search_role=roles[i],
            role_priority="primary" if i == 0 else "strong_fit", variation_index=i + 1,
        )
        for i in range(count)
    ]


class AdapterProgressEmissionTests(unittest.TestCase):
    """The real LinkedIn adapter emits the Dice-shaped progress events."""

    def setUp(self) -> None:
        self.adapter = LinkedInAutomation(Settings(openai_api_key=None))

    def _run(self, variations, jobs_per_variation, callback):
        @contextmanager
        def fake_context():
            class _Ctx:
                def new_page(self):
                    class _Page:
                        def bring_to_front(self):
                            return None

                        def title(self):
                            return "LinkedIn"
                    return _Page()
            yield _Ctx()

        def fake_search(_self, _context, _page, _resume, _location, keywords, limit, _diag, _ts):
            return [
                {"title": f"{keywords} {i}", "company": "Co", "location": "Remote",
                 "description": "d", "job_url": f"https://www.linkedin.com/jobs/view/{abs(hash(keywords)) % 10000}{i}",
                 "easy_apply": True}
                for i in range(jobs_per_variation)
            ]

        with patch.object(LinkedInAutomation, "_browser_context", lambda self: fake_context()):
            with patch.object(LinkedInAutomation, "_search_jobs_on_page", fake_search):
                with patch.object(self.adapter.settings, "linkedin_headless", True):
                    return self.adapter.search_jobs_many(
                        None, variations, 5, progress_callback=callback
                    )

    def test_find20_emits_variation_1_to_4_of_4(self) -> None:
        events: list[tuple] = []

        def callback(variation, raw_jobs, index, total, phase="completed"):
            events.append((phase, index, total, variation.query, len(raw_jobs)))

        self._run(_variations(4), 3, callback)

        completed = [e for e in events if e[0] == "completed"]
        self.assertEqual([(e[1], e[2]) for e in completed], [(1, 4), (2, 4), (3, 4), (4, 4)])
        self.assertEqual(
            [e[3] for e in completed],
            ["Workforce Analytics Analyst", "People Analytics Analyst",
             "HR Data Analyst", "HR Reporting Analyst"],
        )

    def test_started_phase_precedes_each_completion(self) -> None:
        events: list[tuple] = []

        def callback(variation, raw_jobs, index, total, phase="completed"):
            events.append((phase, index))

        self._run(_variations(4), 2, callback)

        # started(i) always comes before completed(i), for every variation.
        self.assertEqual(
            events,
            [("started", 1), ("completed", 1), ("started", 2), ("completed", 2),
             ("started", 3), ("completed", 3), ("started", 4), ("completed", 4)],
        )

    def test_per_variation_job_counts_are_reported(self) -> None:
        counts: list[int] = []

        def callback(variation, raw_jobs, index, total, phase="completed"):
            if phase == "completed":
                counts.append(len(raw_jobs))

        self._run(_variations(4), 3, callback)
        self.assertEqual(counts, [3, 3, 3, 3])

    def test_progress_failure_never_breaks_the_search(self) -> None:
        def exploding_callback(*_args, **_kwargs):
            raise RuntimeError("dashboard is down")

        results = self._run(_variations(4), 2, exploding_callback)

        # The search still completed and returned every variation's jobs.
        self.assertEqual(len(results), 4)
        self.assertTrue(all(len(jobs) == 2 for _variation, jobs in results))

    def test_legacy_callback_without_phase_still_works(self) -> None:
        seen: list[int] = []

        def legacy_callback(variation, raw_jobs, index, total):  # the original Dice contract
            seen.append(index)

        results = self._run(_variations(4), 1, legacy_callback)
        self.assertEqual(len(results), 4)
        self.assertEqual(seen, [1, 2, 3, 4])

    def test_results_are_unchanged_with_and_without_reporting(self) -> None:
        with_cb = self._run(_variations(4), 2, lambda *a, **k: None)
        without_cb = self._run(_variations(4), 2, None)
        self.assertEqual(
            [(v.query, [j["job_url"] for j in jobs]) for v, jobs in with_cb],
            [(v.query, [j["job_url"] for j in jobs]) for v, jobs in without_cb],
        )


class ProgressCallbackFakeLinkedIn:
    """A LinkedIn adapter that accepts the shared callback and snapshots the batch row while the
    search is still running -- proving the dashboard can see progress before the batch ends."""

    snapshots: list[dict] = []
    batch_id: str | None = None

    def __init__(self, _settings) -> None:
        pass

    def search_jobs_many(self, _resume, variations, limit_per_query, collect_card_diagnostics=False,
                         progress_callback=None):
        variations = list(variations)
        limits = per_variation_limits(limit_per_query, len(variations))
        results = []
        counter = 0
        for index, variation in enumerate(variations):
            if progress_callback:
                progress_callback(variation, [], index + 1, len(variations), phase="started")
            raw_jobs = []
            for _ in range(min(3, limits[index])):
                counter += 1
                raw_jobs.append({
                    "title": f"Analyst {counter}", "company": f"Co {counter}", "location": "Remote",
                    "description": "SQL Excel reporting",
                    "job_url": f"https://www.linkedin.com/jobs/view/{880000000 + counter}",
                    "easy_apply": True,
                })
            results.append((variation, raw_jobs))
            if progress_callback:
                progress_callback(variation, raw_jobs, index + 1, len(variations))
            self._snapshot()
        return results

    def _snapshot(self) -> None:
        # A separate session, exactly like the polling dashboard request would use.
        with SessionLocal() as db:
            row = db.get(JobSearchBatch, self.batch_id)
            if row is not None:
                db.refresh(row)
                self.snapshots.append({
                    "status": row.status,
                    "current_variation_index": row.current_variation_index,
                    "total_variations": row.total_variations,
                    "current_search_variation": row.current_search_variation,
                    "jobs_found_this_variation": row.jobs_found_this_variation,
                    "jobs_collected": row.jobs_collected,
                    "latest_status_message": row.latest_status_message,
                })


class LiveBatchProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ProgressCallbackFakeLinkedIn.snapshots = []
        with SessionLocal() as db:
            set_active_portal(db, "linkedin")

    def test_dashboard_sees_per_variation_progress_before_the_batch_finishes(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".json") as state_file:
            with patch.object(settings, "linkedin_storage_state", Path(state_file.name)):
                with SessionLocal() as db:
                    resume = ResumeProfile(
                        filename="hr-resume.txt", stored_path="hr-resume.txt",
                        raw_text="HR analytics SQL Excel workforce reporting",
                        skills=["sql", "excel"], experience=[], education=[], job_titles=[],
                        keywords=["hr", "analytics"], technologies=["SQL", "Excel"],
                        domain_expertise=["People Analytics"], detected_role="HR Data Analyst",
                        generated_search_keywords=["HR Data Analyst", "People Analytics Analyst"],
                        role_confidence=92,
                    )
                    db.add(resume)
                    db.commit()
                    db.refresh(resume)
                    request = SearchRequest(resume_id=resume.id, location="Remote", keywords=None, limit=20)
                    try:
                        with patch("app.main.LinkedInAutomation", ProgressCallbackFakeLinkedIn):
                            batch = _create_search_batch(request, db)
                            ProgressCallbackFakeLinkedIn.batch_id = batch.id
                            _run_find_matches_batch(batch.id)

                        snaps = ProgressCallbackFakeLinkedIn.snapshots
                        # Find 20 -> exactly 4 variations, reported 1/4 .. 4/4.
                        self.assertEqual(len(snaps), 4)
                        self.assertEqual([s["current_variation_index"] for s in snaps], [1, 2, 3, 4])
                        self.assertTrue(all(s["total_variations"] == 4 for s in snaps))

                        # Progress was visible WHILE running (status still "running" mid-batch).
                        self.assertEqual(snaps[0]["status"], "running")
                        self.assertNotEqual(snaps[0]["jobs_collected"], 0)

                        # Cumulative counts increase after each variation.
                        cumulative = [s["jobs_collected"] for s in snaps]
                        self.assertEqual(cumulative, sorted(cumulative))
                        self.assertLess(cumulative[0], cumulative[-1])
                        self.assertEqual(cumulative, [3, 6, 9, 12])

                        # Per-variation count and role/query are published each step.
                        self.assertTrue(all(s["jobs_found_this_variation"] == 3 for s in snaps))
                        self.assertTrue(all(s["current_search_variation"] for s in snaps))
                        for i, snap in enumerate(snaps, start=1):
                            self.assertIn(f"LinkedIn search {i}/4", snap["latest_status_message"])

                        # Final results are still persisted and grouped by variation_index.
                        with SessionLocal() as verify:
                            jobs = list(verify.scalars(
                                select(JobOpportunity)
                                .where(JobOpportunity.search_batch_id == batch.id)
                            ).all())
                        self.assertTrue(jobs)
                        self.assertTrue(all(job.easy_apply for job in jobs))
                        indexes = sorted(
                            job.job_description_metadata.get("variation_index") for job in jobs
                        )
                        self.assertTrue(all(1 <= i <= 4 for i in indexes))
                    finally:
                        with SessionLocal() as cleanup:
                            cleanup.execute(delete(JobOpportunity).where(JobOpportunity.resume_id == resume.id))
                            cleanup.execute(delete(JobSearchBatch).where(JobSearchBatch.resume_id == resume.id))
                            cleanup.commit()
                        db.delete(resume)
                        db.commit()


class SharedMechanismTests(unittest.TestCase):
    def test_one_shared_callback_serves_both_portals(self) -> None:
        # No second, incompatible progress system: a single factory, used by both portals.
        self.assertEqual(MAIN_SOURCE.count("def make_progress_callback("), 1)
        self.assertIn('make_progress_callback("LinkedIn")', MAIN_SOURCE)
        self.assertIn('make_progress_callback("Dice")', MAIN_SOURCE)
        self.assertNotIn("dice_progress_callback", MAIN_SOURCE)

    def test_dice_status_message_format_is_unchanged(self) -> None:
        # Dice's message stays exactly "Dice search i/N: <query> returned N jobs."
        self.assertIn('f"{portal_label} search {index}/{total}: {query} "', MAIN_SOURCE)
        self.assertIn('f"returned {found_now} jobs."', MAIN_SOURCE)

    def test_progress_reporting_is_wrapped_against_failure(self) -> None:
        block = MAIN_SOURCE.split("def make_progress_callback(")[1].split("return progress_callback")[0]
        self.assertIn("try:", block)
        self.assertIn("except Exception", block)
        self.assertIn("progress reporting failed", block)

    def test_counts_use_each_portals_filtered_result_not_raw_cards(self) -> None:
        block = MAIN_SOURCE.split("def make_progress_callback(")[1].split("return progress_callback")[0]
        # jobs_collected accumulates len(raw_jobs) -- the adapter's already-filtered result.
        self.assertIn('totals["jobs"] += len(raw_jobs)', block)
        self.assertIn("jobs_found_this_variation=found_now", block)

    def test_progress_fields_are_exposed_by_the_batch_schema(self) -> None:
        for field in ("current_variation_index", "total_variations", "jobs_found_this_variation",
                      "jobs_collected", "current_search_variation", "latest_status_message"):
            self.assertIn(field, SearchBatchOut.model_fields)


if __name__ == "__main__":
    unittest.main()
