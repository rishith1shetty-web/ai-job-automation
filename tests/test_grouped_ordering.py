"""Skill-priority GROUPED ordering of the completed matched batch.

Relevance/eligibility decide INCLUSION (unchanged); the search variation that discovered a job
then decides its GROUP, and posting time orders jobs only WITHIN a group:

    variation_index ASC
      -> confirmed posted_at before unknown
      -> posted_at DESC
      -> match_score DESC
      -> priority_score DESC
      -> id ASC

Posting time is never compared across variations. The dashboard (GET /jobs -> _job_apply_ordering)
and Apply Top share this one order. variation_index lives in job_description_metadata (JSON).
"""

import unittest
from datetime import datetime, timedelta

from sqlalchemy import delete

from app.config import Settings
from app.database import SessionLocal, init_db
from app.main import _apply_top_jobs_statement, list_jobs, set_active_portal
from app.models import JobOpportunity, JobSearchBatch, ResumeProfile
from app.services.posting_time import freshness_score_for, parse_posted_text

REF = datetime(2026, 7, 17, 14, 30, 0)


def _resume() -> ResumeProfile:
    return ResumeProfile(
        portal="dice", filename="grouped.txt", stored_path="grouped.txt",
        raw_text="Python SQL", skills=["Python", "SQL"], experience=[], education=[],
        job_titles=["Data Analyst"], keywords=["analytics"], technologies=["Python", "SQL"],
        domain_expertise=["Data Analytics"], detected_role="Data Analyst",
        generated_search_keywords=["Data Analyst"], generated_search_query="Data Analyst",
        role_confidence=90,
    )


class GroupedOrderingSqlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        self.db = SessionLocal()
        set_active_portal(self.db, "dice")
        self.resume = _resume()
        self.db.add(self.resume)
        self.db.commit()
        self.db.refresh(self.resume)
        self.batch = JobSearchBatch(id=f"grouped-{self.resume.id}", resume_id=self.resume.id,
                                    search_query="Data Analyst", location="Remote", portal="dice")
        self.db.add(self.batch)
        self.db.commit()
        self._n = 0

    def tearDown(self) -> None:
        self.db.execute(delete(JobOpportunity).where(JobOpportunity.resume_id == self.resume.id))
        self.db.execute(delete(JobSearchBatch).where(JobSearchBatch.resume_id == self.resume.id))
        self.db.delete(self.resume)
        self.db.commit()
        self.db.close()

    def _job(self, name: str, *, variation_index: int | None, posted: str | None,
             match: float = 50, priority: float = 50) -> JobOpportunity:
        self._n += 1
        posted_at = parse_posted_text(posted, REF) if posted else None
        meta = {"posted_date": posted or "", "posted_at": posted_at.isoformat() if posted_at else None}
        if variation_index is not None:
            meta["variation_index"] = variation_index
        job = JobOpportunity(
            resume_id=self.resume.id, search_batch_id=self.batch.id, portal="dice",
            title=name, company="Acme", location="Remote",
            job_url=f"https://www.dice.com/job-detail/{self._n:08d}-2222-4333-8444-555555555555",
            description="SQL", easy_apply=True, match_score=match, priority_score=priority,
            freshness_score=freshness_score_for(posted_at, REF), job_description_metadata=meta,
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return job

    def _order(self) -> list[str]:
        rows = list_jobs(resume_id=self.resume.id, search_batch_id=self.batch.id, portal="dice", db=self.db)
        return [row.title for row in rows]

    def test_variation_groups_are_contiguous_and_priority_ordered(self) -> None:
        # Interleave insertion across variations; the result groups var1, then var2, then var3.
        self._job("v2-a", variation_index=2, posted="3 minutes ago")
        self._job("v1-a", variation_index=1, posted="1 hour ago")
        self._job("v3-a", variation_index=3, posted="1 minute ago")
        self._job("v1-b", variation_index=1, posted="7 minutes ago")
        self._job("v2-b", variation_index=2, posted="2 hours ago")
        order = self._order()
        self.assertEqual(order, ["v1-b", "v1-a", "v2-a", "v2-b", "v3-a"])
        # All var1 before all var2 before all var3.
        self.assertLess(order.index("v1-a"), order.index("v2-a"))
        self.assertLess(order.index("v2-b"), order.index("v3-a"))

    def test_a_newer_lower_variation_job_never_jumps_above_a_higher_variation(self) -> None:
        self._job("v1-old", variation_index=1, posted="1 day ago")
        self._job("v3-fresh", variation_index=3, posted="1 minute ago")
        self._job("v2-mid", variation_index=2, posted="2 hours ago")
        self.assertEqual(self._order(), ["v1-old", "v2-mid", "v3-fresh"])

    def test_within_a_variation_newest_ranks_first(self) -> None:
        self._job("v1-1h", variation_index=1, posted="1 hour ago")
        self._job("v1-7m", variation_index=1, posted="7 minutes ago")
        self._job("v1-20m", variation_index=1, posted="20 minutes ago")
        self.assertEqual(self._order(), ["v1-7m", "v1-20m", "v1-1h"])

    def test_within_a_variation_two_hours_beats_one_day(self) -> None:
        self._job("v2-1d", variation_index=2, posted="1 day ago")
        self._job("v2-2h", variation_index=2, posted="2 hours ago")
        # A higher variation exists but must not interleave.
        self._job("v1-x", variation_index=1, posted="3 days ago")
        self.assertEqual(self._order(), ["v1-x", "v2-2h", "v2-1d"])

    def test_unknown_dates_sink_to_the_bottom_of_their_own_variation(self) -> None:
        self._job("v1-unknown", variation_index=1, posted=None)
        self._job("v1-conf", variation_index=1, posted="2 days ago")
        self._job("v2-conf", variation_index=2, posted="5 minutes ago")
        # Unknown var1 is last WITHIN var1, but the whole var1 group still precedes var2.
        self.assertEqual(self._order(), ["v1-conf", "v1-unknown", "v2-conf"])

    def test_unknown_in_a_top_variation_still_beats_a_confirmed_lower_variation(self) -> None:
        self._job("v2-fresh", variation_index=2, posted="1 minute ago")
        self._job("v1-unknown", variation_index=1, posted=None)
        self.assertEqual(self._order(), ["v1-unknown", "v2-fresh"])

    def test_equal_time_breaks_on_match_then_priority_then_id(self) -> None:
        low = self._job("v1-lowmatch", variation_index=1, posted="3 days ago", match=60, priority=40)
        high = self._job("v1-highmatch", variation_index=1, posted="3 days ago", match=80, priority=40)
        self.assertEqual(self._order()[:2], ["v1-highmatch", "v1-lowmatch"])
        self.assertLess(low.id, high.id)  # newer row, yet ranked below on the match tie-break

    def test_insertion_order_has_no_influence(self) -> None:
        # Insert strictly reversed; grouped order is still var1..var3, newest-first within.
        self._job("v3", variation_index=3, posted="1 minute ago")
        self._job("v2", variation_index=2, posted="2 hours ago")
        self._job("v1", variation_index=1, posted="1 day ago")
        self.assertEqual(self._order(), ["v1", "v2", "v3"])

    def test_apply_top_shares_the_grouped_order(self) -> None:
        self._job("v2-fresh", variation_index=2, posted="1 minute ago")
        self._job("v1-a", variation_index=1, posted="1 hour ago")
        self._job("v1-b", variation_index=1, posted="7 minutes ago")
        statement = _apply_top_jobs_statement(self.resume.id, self.batch.id, "dice")
        titles = [row.title for row in self.db.scalars(statement).all()]
        # Apply Top's first rows are variation 1's, newest-first -- same as the dashboard.
        self.assertEqual(titles[:3], ["v1-b", "v1-a", "v2-fresh"])
        self.assertEqual(titles[:2], self._order()[:2])


if __name__ == "__main__":
    unittest.main()
