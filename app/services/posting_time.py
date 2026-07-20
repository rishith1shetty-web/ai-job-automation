"""Deterministic posting-time normalization shared by every portal.

Portals expose a job's age as loose text ("7 minutes ago", "Posted today", "Reposted 1 week
ago", an explicit date) or not at all. Ranking needs one comparable representation, so this
module turns that text into a naive-UTC datetime and a bounded freshness score. It is pure and
deterministic: the SAME reference timestamp must be captured once per Find Matches run and
passed to every conversion in that run, so "7 minutes ago" and "1 hour ago" are measured
against the same clock instant.

Semantics (documented, not minute-precise beyond what the portal text provides):
  - "just now" / "moments ago" / "N seconds ago"  -> the reference instant
  - "N minutes|hours|days|weeks ago"              -> reference minus that duration
  - "N months ago"                                -> reference minus N*30 days (approximation)
  - "30+ days ago"                                -> the MINIMUM age: reference minus 30 days
  - "today"                                       -> 00:00 UTC of the reference day
  - "yesterday"                                   -> 00:00 UTC of the previous day
  - explicit dates (ISO "2026-07-15", "July 15, 2026", "15 July 2026")
                                                  -> 00:00 UTC of that date
  - anything else (missing, malformed)            -> None (unknown stays unknown; nothing is
                                                    invented and discovery time is never used)

Freshness score (feeds the existing `freshness_score` column, whose ranking weight is
unchanged -- maximum composite effect stays 0.10 * 100 = 10 points):
  - unknown posted_at             -> 0.0                (always below any confirmed date)
  - confirmed posted_at           -> max(5.0, 100 - 12*log10(1 + age_minutes))
    Strictly monotone in age, so 7 min > 12 min > 1 h > today's boundary > yesterday >
    3 days > 1 week > older explicit dates, and the 5.0 floor keeps even ancient confirmed
    dates above unknown. A job is never REJECTED for age here -- this only orders it.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta

UNKNOWN_FRESHNESS = 0.0
CONFIRMED_FRESHNESS_FLOOR = 5.0

_UNIT_MINUTES = {
    "sec": 1 / 60,
    "second": 1 / 60,
    "min": 1,
    "minute": 1,
    "hr": 60,
    "hour": 60,
    "day": 1440,
    "week": 10080,
    "month": 43200,  # 30 days -- documented approximation
}

# One phrase vocabulary for every portal format the app has actually encountered:
# Dice cards ("Posted today", "just now", "N hours/days/weeks/months ago") and LinkedIn
# top-cards ("Reposted 2 weeks ago", "3 days ago", "30+ days ago"). Localization is not
# broadened beyond these English forms.
_RELATIVE_RE = re.compile(
    r"(?:(?:re)?posted\s+)?"
    r"(?P<phrase>just\s+now|moments?\s+ago|today|yesterday|"
    r"(?P<count>\d+)\s*\+?\s*(?P<unit>sec(?:ond)?s?|min(?:ute)?s?|hrs?|hours?|days?|weeks?|months?)\s+ago)",
    re.IGNORECASE,
)
# Dice's embedded search payload carries exact per-job timestamps ("2026-07-17T10:16:41Z",
# sometimes with millis). Full precision is kept -- checked before the date-only form so the
# time part is never truncated to midnight.
_ISO_DATETIME_RE = re.compile(
    r"\b(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|[+-]00:00)?\b"
)
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTH_NAME = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_MDY_RE = re.compile(rf"\b(?P<month>{_MONTH_NAME})\s+(?P<day>\d{{1,2}}),?\s+(?P<year>\d{{4}})\b", re.I)
_DMY_RE = re.compile(rf"\b(?P<day>\d{{1,2}})\s+(?P<month>{_MONTH_NAME})\s+(?P<year>\d{{4}})\b", re.I)
_MONTH_INDEX = {name: index for index, name in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1
)}


def extract_posted_phrase(*texts: str | None) -> str:
    """The first recognisable posting-age/date phrase found in the given texts, in order.
    Returns '' when none of the texts contains one. Used both to read a dedicated posted-date
    element and to rescue the phrase from longer card text."""
    for text in texts:
        candidate = str(text or "")
        if not candidate.strip():
            continue
        match = _RELATIVE_RE.search(candidate)
        if match:
            return re.sub(r"\s+", " ", match.group(0)).strip()
        for pattern in (_ISO_DATE_RE, _MDY_RE, _DMY_RE):
            date_match = pattern.search(candidate)
            if date_match:
                return date_match.group(0).strip()
    return ""


def parse_posted_text(text: str | None, reference: datetime) -> datetime | None:
    """Normalize a posting phrase to one comparable naive-UTC datetime, measured against the
    single per-run `reference` timestamp. Unknown or malformed text returns None -- never a
    guessed or discovery-based date."""
    raw = str(text or "").strip()
    if not raw:
        return None
    match = _RELATIVE_RE.search(raw)
    if match:
        phrase = match.group("phrase").lower()
        if phrase in {"just now"} or phrase.startswith("moment"):
            return reference
        if phrase == "today":
            return reference.replace(hour=0, minute=0, second=0, microsecond=0)
        if phrase == "yesterday":
            return (reference - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        count = int(match.group("count"))
        unit_key = re.sub(r"s$", "", match.group("unit").lower())
        unit_key = {"hrs": "hr", "mins": "min", "secs": "sec"}.get(unit_key, unit_key)
        minutes = _UNIT_MINUTES.get(unit_key)
        if minutes is None:
            return None
        return reference - timedelta(minutes=count * minutes)
    iso_dt = _ISO_DATETIME_RE.search(raw)
    if iso_dt:
        try:
            return datetime(*(int(iso_dt.group(i)) for i in range(1, 7)))
        except ValueError:
            return None
    iso = _ISO_DATE_RE.search(raw)
    if iso:
        try:
            return datetime(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    for pattern in (_MDY_RE, _DMY_RE):
        named = pattern.search(raw)
        if named:
            month = _MONTH_INDEX.get(named.group("month").lower()[:3])
            try:
                return datetime(int(named.group("year")), month, int(named.group("day")))
            except (TypeError, ValueError):
                return None
    return None


def relative_posted_label(posted_at: datetime | None, reference: datetime) -> str:
    """Human-readable relative age for the authoritative naive-UTC `posted_at`, measured
    against the single per-run `reference` timestamp -- the inverse of `parse_posted_text`.

    Used for DISPLAY only; ordering always uses `posted_at`/`freshness_score`. The graduated
    rule emits the portals' own vocabulary and stays readable at every age:

        age < 60s                 -> "Just now"
        age < 60m                 -> "N minutes ago"
        age < 12h                 -> "N hours ago"      (precise recent window)
        same calendar day, >=12h  -> "Today"
        previous calendar day     -> "Yesterday"
        2..6 calendar days        -> "N days ago"
        1..4 calendar weeks       -> "N weeks ago"
        older                     -> "N months ago"

    Returns '' when `posted_at` is unknown, so the caller shows "Unknown" -- a value is never
    invented. Deriving the label from `posted_at` (not the portal's coarse badge) is what keeps
    the visible age consistent with the newest-to-oldest ordering.
    """
    if posted_at is None:
        return ""
    seconds = max(0.0, (reference - posted_at).total_seconds())
    minutes = seconds / 60.0
    if seconds < 60:
        return "Just now"
    if minutes < 60:
        value = int(minutes)
        return f"{value} minute{'s' if value != 1 else ''} ago"
    hours = minutes / 60.0
    if hours < 12:
        value = int(hours)
        return f"{value} hour{'s' if value != 1 else ''} ago"
    day_diff = (reference.date() - posted_at.date()).days
    if day_diff <= 0:
        return "Today"
    if day_diff == 1:
        return "Yesterday"
    if day_diff < 7:
        return f"{day_diff} days ago"
    if day_diff < 35:
        weeks = day_diff // 7
        return f"{weeks} week{'s' if weeks != 1 else ''} ago"
    months = day_diff // 30
    return f"{months} month{'s' if months != 1 else ''} ago"


def freshness_score_for(posted_at: datetime | None, reference: datetime) -> float:
    """Deterministic, strictly age-monotone freshness in [floor, 100] for confirmed dates and
    exactly UNKNOWN_FRESHNESS for unknown ones -- so within any equal-relevance comparison a
    confirmed date always orders above unknown, and newer always orders above older."""
    if posted_at is None:
        return UNKNOWN_FRESHNESS
    age_minutes = max(0.0, (reference - posted_at).total_seconds() / 60.0)
    score = 100.0 - 12.0 * math.log10(1.0 + age_minutes)
    return round(max(CONFIRMED_FRESHNESS_FLOOR, score), 4)
