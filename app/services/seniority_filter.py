"""Senior-level role exclusion for Find Matches.

Find Matches serves junior, entry-level, associate and ordinary individual-contributor roles. This
module decides whether a posting is senior-level so every portal applies ONE policy, in every
domain -- DevOps, software, data, cloud, security, finance or anything else.

The decision is made from the job TITLE. A description is consulted only for an explicit,
narrowly worded level statement ("This is a senior-level position", "Seniority level: Director");
a description that merely mentions senior colleagues, senior stakeholders, leadership or
senior-level tools never excludes a job.

The policy, in order:

1. A title that explicitly names an entry level (Junior, Jr, Entry-Level, Graduate, Intern,
   Trainee, Apprentice) is KEPT. This deliberately covers range postings such as
   "Junior/Senior Developer" or "Senior-Junior Engineer": the posting is open to a junior
   candidate, so it is not a senior-only role.
2. An explicit level word excludes: Senior/Sr/Snr, Lead (Tech Lead, Team Lead, Lead Engineer),
   Principal, Distinguished, and Staff on a technical ladder (Staff Engineer, Staff Data
   Scientist). "Staff" on its own is NOT a level -- Staff Accountant, Staff Nurse and Member of
   Technical Staff are ordinary roles.
3. A role noun that implies seniority excludes: Architect (Solutions, Cloud, DevOps, Enterprise,
   Software ...), technical/engineering management (Engineering Manager, Development Manager,
   Technical Manager, IT Manager ...) and executive titles (Director, Head of, VP, Chief).
   "Associate ... Architect" is an entry-level architecture track and is kept.
4. Anything else is kept -- including ordinary managers (Product, Project, Program, Account,
   Business Development) unless their title states a level from rule 2.

Matching is on whole words after normalization (case, punctuation, hyphens and whitespace are
ignored, and Sr/Snr/Jr/Mgr/Eng abbreviations are expanded), so "staff" never matches "staffing"
and "lead" never matches "leadership".

Portal-independent: it reads plain strings only and has no portal, browser or database
dependency.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

# Abbreviations expanded before any rule runs, so "Sr. Engineer", "SR Engineer" and
# "Senior Engineer" are one case.
_ABBREVIATIONS = (
    (re.compile(r"\b(?:sr|snr)\b"), "senior"),
    (re.compile(r"\bjr\b"), "junior"),
    (re.compile(r"\bmgr\b"), "manager"),
    (re.compile(r"\bengg?\b"), "engineering"),
)

# Rule 1: an explicit entry level keeps the title.
_ENTRY_LEVEL_PHRASES = (
    "junior", "entry level", "graduate", "new grad", "intern", "internship",
    "trainee", "apprentice", "apprenticeship",
)

# Rule 2: explicit level words. "staff" is handled separately (see _STAFF_LADDER_NOUNS).
_LEVEL_WORDS = ("senior", "lead", "principal", "distinguished")
# "Senior" that describes the people served, not the role's level.
_SENIOR_NOT_A_LEVEL = ("senior living", "senior care", "senior citizen", "senior citizens",
                       "senior center", "senior centre", "senior housing", "senior home")
# "Lead" as a sales noun, not a level.
_LEAD_NOT_A_LEVEL = ("lead generation", "lead gen")
# Technical career-ladder nouns: only with one of these AFTER it is "Staff" a seniority level.
_STAFF_LADDER_NOUNS = ("engineer", "engineers", "engineering", "developer", "scientist",
                       "researcher", "designer", "architect", "programmer", "sre", "swe",
                       "technologist")
_MEMBER_OF_TECHNICAL_STAFF = ("member of technical staff", "member of the technical staff")

# Rule 3: role nouns that imply seniority.
_ARCHITECT_WORDS = ("architect", "architects")
_EXECUTIVE_PHRASES = ("director", "head of", "vp", "svp", "evp", "vice president", "chief",
                      "cto", "cio", "ciso")
# "<qualifier> Manager" is technical/engineering management.
_TECH_MANAGER_QUALIFIERS = ("engineering", "technical", "technology", "it", "devops",
                            "infrastructure", "platform", "software", "qa", "sre")
# "Development Manager" is engineering management only in a software context; "Business
# Development Manager" is a sales role and is kept.
_SOFTWARE_DEVELOPMENT_QUALIFIERS = ("software", "application", "applications", "web", "it",
                                    "systems", "game", "mobile", "technical", "technology")
# "Manager, Software Engineering" / "Manager of Development" word order.
_MANAGER_OF_PHRASES = ("engineering", "software engineering", "software development",
                       "development", "technology", "devops", "infrastructure",
                       "platform engineering", "technical")

# Explicit level statements in a description (normalized text). Deliberately narrow: LinkedIn's
# ubiquitous "Seniority level: Mid-Senior level" does not match, nor does any mention of senior
# colleagues, stakeholders, leadership or senior-level tools.
_DESCRIPTION_LEVEL_STATEMENTS = (
    re.compile(r"\bthis is an? (?:senior|lead|principal|staff) (?:level )?(?:role|position|opportunity)\b"),
    re.compile(r"\b(?:seniority|experience|career|job) level (?:senior|lead|principal|director|executive)\b"),
)


def normalize_title(text: str | None) -> str:
    """Lowercase, collapse punctuation/hyphens/whitespace to single spaces and expand the
    documented abbreviations. "Sr.  DevOps-Engineer" -> "senior devops engineer"."""
    normalized = re.sub(r"[^a-z0-9+#]+", " ", str(text or "").lower()).strip()
    for pattern, replacement in _ABBREVIATIONS:
        normalized = pattern.sub(replacement, normalized)
    return normalized


def senior_level_reason(title: str | None, description: str | None = "") -> str | None:
    """Why this posting is senior-level, or None when it is acceptable for Find Matches."""
    text = normalize_title(title)
    if not text:
        return None
    padded = f" {text} "
    if _has_any(padded, _ENTRY_LEVEL_PHRASES):
        return None
    reason = _title_level_reason(padded) or _title_role_reason(padded, text.split())
    if reason:
        return reason
    normalized_description = normalize_title(description)
    if any(pattern.search(normalized_description) for pattern in _DESCRIPTION_LEVEL_STATEMENTS):
        return "description states a senior-level role"
    return None


def senior_level_job_reason(job: Mapping) -> str | None:
    """senior_level_reason for a raw Find Matches job dict (title + description)."""
    return senior_level_reason(job.get("title"), job.get("description"))


def is_senior_level_job(job: Mapping) -> bool:
    return senior_level_job_reason(job) is not None


def _has_any(padded: str, phrases) -> bool:
    return any(f" {phrase} " in padded for phrase in phrases)


def _title_level_reason(padded: str) -> str | None:
    """Rule 2: an explicit level word in the title."""
    for word in _LEVEL_WORDS:
        if f" {word} " not in padded:
            continue
        if word == "senior" and _has_any(padded, _SENIOR_NOT_A_LEVEL) and padded.count(" senior ") == 1:
            continue
        if word == "lead" and _has_any(padded, _LEAD_NOT_A_LEVEL) and padded.count(" lead ") == 1:
            continue
        return f"{word} title"
    if " staff " in padded and not _has_any(padded, _MEMBER_OF_TECHNICAL_STAFF):
        after_staff = padded.split(" staff ", 1)[1]
        if _has_any(f" {after_staff} ", _STAFF_LADDER_NOUNS):
            return "staff title"
    return None


def _title_role_reason(padded: str, tokens: list[str]) -> str | None:
    """Rule 3: a role noun that implies seniority."""
    if _has_any(padded, _ARCHITECT_WORDS) and " associate " not in padded:
        return "architect title"
    for phrase in _EXECUTIVE_PHRASES:
        if f" {phrase} " in padded:
            return f"executive title ({phrase})"
    for index, token in enumerate(tokens):
        if token != "manager":
            continue
        previous = tokens[index - 1] if index > 0 else ""
        if previous in _TECH_MANAGER_QUALIFIERS:
            return f"technical management title ({previous} manager)"
        if previous == "development":
            before = tokens[index - 2] if index > 1 else ""
            if not before or before in _SOFTWARE_DEVELOPMENT_QUALIFIERS:
                return "technical management title (development manager)"
        following = " ".join(tokens[index + 1:])
        following = following[3:] if following.startswith("of ") else following
        if any(following == phrase or following.startswith(f"{phrase} ") for phrase in _MANAGER_OF_PHRASES):
            return "technical management title (manager of engineering)"
        if following in {"it", "information technology"}:
            return "technical management title (it manager)"
    return None
