"""Deterministic posting-time normalization (shared by LinkedIn and Dice).

One reference timestamp per Find Matches run; portal text -> comparable naive-UTC datetime;
freshness score strictly monotone in age with unknown BELOW every confirmed date. No date is
ever invented and discovery time is never substituted.
"""

import unittest
from datetime import datetime, timedelta

from app.services.posting_time import (
    CONFIRMED_FRESHNESS_FLOOR,
    UNKNOWN_FRESHNESS,
    extract_posted_phrase,
    freshness_score_for,
    parse_posted_text,
    relative_posted_label,
)

REF = datetime(2026, 7, 17, 14, 30, 0)


def _score(text: str) -> float:
    return freshness_score_for(parse_posted_text(text, REF), REF)


class ParsePostedTextTests(unittest.TestCase):
    def test_minutes_hours_days_weeks(self) -> None:
        self.assertEqual(parse_posted_text("7 minutes ago", REF), REF - timedelta(minutes=7))
        self.assertEqual(parse_posted_text("12 minutes ago", REF), REF - timedelta(minutes=12))
        self.assertEqual(parse_posted_text("1 hour ago", REF), REF - timedelta(hours=1))
        self.assertEqual(parse_posted_text("3 hours ago", REF), REF - timedelta(hours=3))
        self.assertEqual(parse_posted_text("2 days ago", REF), REF - timedelta(days=2))
        self.assertEqual(parse_posted_text("1 week ago", REF), REF - timedelta(weeks=1))

    def test_portal_wording_variants(self) -> None:
        # Prefixes and abbreviations the portals actually use.
        self.assertEqual(parse_posted_text("Posted 3 days ago", REF), REF - timedelta(days=3))
        self.assertEqual(parse_posted_text("Reposted 2 weeks ago", REF), REF - timedelta(weeks=2))
        self.assertEqual(parse_posted_text("5 mins ago", REF), REF - timedelta(minutes=5))
        self.assertEqual(parse_posted_text("2 hrs ago", REF), REF - timedelta(hours=2))
        self.assertEqual(parse_posted_text("just now", REF), REF)
        self.assertEqual(parse_posted_text("moments ago", REF), REF)
        # "30+ days ago" normalizes to its documented minimum age.
        self.assertEqual(parse_posted_text("30+ days ago", REF), REF - timedelta(days=30))

    def test_today_and_yesterday_normalize_to_day_boundaries(self) -> None:
        # Documented semantics: imprecise words map to a consistent point, never a fake time.
        self.assertEqual(parse_posted_text("today", REF), datetime(2026, 7, 17))
        self.assertEqual(parse_posted_text("Posted today", REF), datetime(2026, 7, 17))
        self.assertEqual(parse_posted_text("yesterday", REF), datetime(2026, 7, 16))

    def test_explicit_calendar_dates(self) -> None:
        self.assertEqual(parse_posted_text("2026-07-15", REF), datetime(2026, 7, 15))
        self.assertEqual(parse_posted_text("July 15, 2026", REF), datetime(2026, 7, 15))
        self.assertEqual(parse_posted_text("15 July 2026", REF), datetime(2026, 7, 15))

    def test_exact_iso_datetimes_keep_full_precision(self) -> None:
        # Dice's embedded payload format, captured live: time is preserved, never truncated
        # to midnight.
        self.assertEqual(parse_posted_text("2026-07-17T10:16:41Z", REF),
                         datetime(2026, 7, 17, 10, 16, 41))
        self.assertEqual(parse_posted_text("2026-07-17T10:16:41.000Z", REF),
                         datetime(2026, 7, 17, 10, 16, 41))
        self.assertEqual(parse_posted_text("2026-07-16T00:20:06Z", REF),
                         datetime(2026, 7, 16, 0, 20, 6))

    def test_missing_and_malformed_stay_unknown(self) -> None:
        for bad in ("", None, "N/A", "soon", "posted", "13 fortnights ago", "2026-13-45", "ago"):
            with self.subTest(value=bad):
                self.assertIsNone(parse_posted_text(bad, REF))

    def test_one_reference_time_gives_stable_comparisons(self) -> None:
        # All conversions in a run use the SAME reference, so repeated normalization is
        # byte-stable and cross-job comparisons cannot flip on clock boundaries.
        first = [parse_posted_text(t, REF) for t in ("7 minutes ago", "12 minutes ago", "1 hour ago")]
        second = [parse_posted_text(t, REF) for t in ("7 minutes ago", "12 minutes ago", "1 hour ago")]
        self.assertEqual(first, second)
        self.assertEqual(first[0] - first[1], timedelta(minutes=5))


class ExtractPostedPhraseTests(unittest.TestCase):
    def test_extracts_phrase_from_linkedin_top_card_line(self) -> None:
        line = "Example Health · New York, NY · Reposted 2 weeks ago · Over 100 applicants"
        self.assertEqual(extract_posted_phrase(line).lower(), "reposted 2 weeks ago")

    def test_extracts_phrase_from_dice_card_text(self) -> None:
        text = "Data Analyst\nAcme\nRemote\nEasy Apply\nPosted 7 minutes ago"
        self.assertEqual(extract_posted_phrase(text).lower(), "posted 7 minutes ago")

    def test_first_source_with_a_phrase_wins(self) -> None:
        self.assertEqual(extract_posted_phrase("", None, "3 days ago", "1 week ago"), "3 days ago")

    def test_no_phrase_returns_empty(self) -> None:
        self.assertEqual(extract_posted_phrase("Remote · Full-time · Mid-Senior level"), "")


class RelativePostedLabelTests(unittest.TestCase):
    """datetime -> human-readable age (display only). Derived from the authoritative posted_at
    against the run reference, so the visible age matches the newest-to-oldest ordering and a
    raw ISO string or a stale coarse badge is never shown."""

    def test_recent_window_is_precise(self) -> None:
        self.assertEqual(relative_posted_label(REF, REF), "Just now")
        self.assertEqual(relative_posted_label(REF - timedelta(seconds=30), REF), "Just now")
        self.assertEqual(relative_posted_label(REF - timedelta(minutes=1), REF), "1 minute ago")
        self.assertEqual(relative_posted_label(REF - timedelta(minutes=7), REF), "7 minutes ago")
        self.assertEqual(relative_posted_label(REF - timedelta(hours=1), REF), "1 hour ago")
        self.assertEqual(relative_posted_label(REF - timedelta(hours=2), REF), "2 hours ago")

    def test_calendar_buckets(self) -> None:
        # REF is 14:30, so a same-day 13h-old post falls past the precise window -> "Today".
        self.assertEqual(relative_posted_label(REF.replace(hour=1, minute=0), REF), "Today")
        self.assertEqual(relative_posted_label(REF - timedelta(days=1), REF), "Yesterday")
        self.assertEqual(relative_posted_label(REF - timedelta(days=2), REF), "2 days ago")
        self.assertEqual(relative_posted_label(REF - timedelta(days=3), REF), "3 days ago")
        self.assertEqual(relative_posted_label(REF - timedelta(days=9), REF), "1 week ago")
        self.assertEqual(relative_posted_label(REF - timedelta(days=15), REF), "2 weeks ago")
        self.assertEqual(relative_posted_label(REF - timedelta(days=90), REF), "3 months ago")

    def test_exact_iso_timestamp_becomes_relative_never_raw(self) -> None:
        # Dice's payload postedDate (exact ISO) -> a relative phrase, never the ISO string.
        posted_at = parse_posted_text("2026-07-17T14:23:00Z", REF)  # 7 minutes before REF
        label = relative_posted_label(posted_at, REF)
        self.assertEqual(label, "7 minutes ago")
        self.assertNotIn("T", label)
        self.assertNotIn("2026", label)

    def test_unknown_returns_empty_string(self) -> None:
        self.assertEqual(relative_posted_label(None, REF), "")

    def test_future_timestamp_clamps_to_just_now(self) -> None:
        self.assertEqual(relative_posted_label(REF + timedelta(minutes=5), REF), "Just now")


class FreshnessScoreTests(unittest.TestCase):
    def test_strict_monotonic_ordering_across_the_required_ladder(self) -> None:
        # Spec ladder: 7 min > 12 min > 1 hour > older; today > yesterday > 3 days > 1 week.
        ladder = ["7 minutes ago", "12 minutes ago", "1 hour ago", "5 hours ago",
                  "today", "yesterday", "3 days ago", "1 week ago", "2026-06-01"]
        scores = [_score(text) for text in ladder]
        for earlier, later in zip(scores, scores[1:]):
            self.assertGreater(earlier, later, msg=f"{scores} not strictly decreasing")

    def test_confirmed_always_above_unknown(self) -> None:
        self.assertEqual(_score("garbled"), UNKNOWN_FRESHNESS)
        # Even an ancient confirmed date keeps the floor above unknown.
        ancient = freshness_score_for(datetime(1990, 1, 1), REF)
        self.assertGreaterEqual(ancient, CONFIRMED_FRESHNESS_FLOOR)
        self.assertGreater(ancient, UNKNOWN_FRESHNESS)

    def test_future_or_zero_age_is_capped_at_100(self) -> None:
        self.assertEqual(freshness_score_for(REF, REF), 100.0)
        self.assertEqual(freshness_score_for(REF + timedelta(minutes=5), REF), 100.0)

    def test_bounded_composite_effect(self) -> None:
        # freshness_score stays in [0, 100]; its priority weight (0.10) is unchanged, so the
        # maximum possible composite effect remains 10 points -- an unrelated recent job can
        # never displace a strongly relevant older one (match weight is 45).
        for text in ("just now", "1 week ago", "garbage", ""):
            self.assertLessEqual(_score(text), 100.0)
            self.assertGreaterEqual(_score(text), 0.0)


if __name__ == "__main__":
    unittest.main()
