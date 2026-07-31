"""Top-five search-role selection: rank -> normalize -> filter -> limit.

Find Matches used to search up to twelve ranked roles, and the Find 20/50/100 preset decided how
many of them (4/8/12) were actually searched. That made the preset change *what* was searched, not
just how much came back, and it pushed weak "Related"/"Stretch" roles into the search plan on the
larger presets.

This module owns the single rule that replaces all of that: **at most five roles are ever
searched** -- the detected primary role plus up to four genuinely strong related roles -- and the
same ordered list is used for every preset and every portal.

The limit is applied LAST. A candidate must first survive filtering:

    * it must sit in the SAME professional family as the primary role (adjacent-domain and
      cross-family roles are rejected outright, never used as padding);
    * it must be reachable at the candidate's seniority (a lead/manager role for a mid-level
      resume is rejected, not merely penalised);
    * SOMETHING in the resume must actually point at it -- a title, a responsibility, a project, a
      tool or a qualification. A role the resume says nothing about is filler, and is rejected
      however plausible it looks next to the primary role. (This is deliberately not the stricter
      "professional evidence only" bar: skills and projects are legitimate evidence, and gating on
      responsibilities alone would cut a skills-and-projects resume down to its primary role.
      Weakly-supported roles are handled by ranking below four related slots, not by rejection.);
    * it must not be a near-identical restatement of a role already selected.

Only then are the survivors ordered and cut to :data:`MAX_SEARCH_ROLES`. Fewer than five is a
normal, honest outcome; nothing is ever invented to reach five.

Ordering reuses the confidence the existing ranking pipeline already computed and adds a bounded
*relatedness* term, so the two signals the product cares most about -- similarity to the detected
primary role and professional-family compatibility -- carry the highest influence over which four
related roles are kept. It is a preference between already-qualified candidates, never a new
global acceptance threshold.

The primary role is never filtered, never re-ordered and never deduplicated away: it is selected
before any rule runs.

Portal-independent. Nothing here knows that LinkedIn or Dice exist.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# The whole point of this module. One number, one meaning: the maximum number of roles that may
# reach a portal search, for every preset (Find 20 / 50 / 100) and every portal.
MAX_SEARCH_ROLES = 5

# Relatedness weights. Deliberately small next to the 0-99 confidence scale: they reorder roles
# that already qualified, they never rescue a role that failed a filter.
FAMILY_MATCH_BONUS = 6.0        # same job family as the primary role
TITLE_SIMILARITY_WEIGHT = 8.0   # scaled by the 0-1 token overlap with the primary title

# Synonym folds applied after punctuation is stripped, so titles that would produce the same
# portal search collapse to one entry ("ML Engineer" / "Machine Learning Engineer",
# "Finance Data Analyst" / "Financial Data Analyst", "HR Coordinator" / "Human Resources
# Coordinator"). Longest phrases first -- "business intelligence" must fold before "intelligence"
# could ever be touched by a shorter rule.
_TITLE_FOLDS: tuple[tuple[str, str], ...] = (
    ("natural language processing", "nlp"),
    ("business intelligence", "bi"),
    ("artificial intelligence", "ai"),
    ("information technology", "it"),
    ("human resources", "hr"),
    ("quality assurance", "qa"),
    ("machine learning", "ml"),
    ("full stack", "fullstack"),
    ("front end", "frontend"),
    ("back end", "backend"),
    ("financial", "finance"),
)
# Level words carry no search meaning of their own -- "Senior Data Analyst" and "Data Analyst"
# are the same query to a portal, and seniority is handled by the seniority filter instead.
_LEVEL_WORDS = frozenset({"senior", "sr", "junior", "jr", "lead", "principal", "staff", "associate"})


def normalize_title(title: str) -> str:
    """Fold a role title to its comparison key: lowercase, punctuation-free, synonym-folded and
    stripped of level words. Two titles with the same key would produce the same portal search."""
    collapsed = re.sub(r"[^a-z0-9 ]", " ", str(title or "").lower())
    collapsed = re.sub(r"\s+", " ", collapsed).strip()
    for phrase, folded in _TITLE_FOLDS:
        collapsed = collapsed.replace(phrase, folded)
    words = [word for word in collapsed.split() if word not in _LEVEL_WORDS]
    return " ".join(words) or collapsed


def title_similarity(left: str, right: str) -> float:
    """Token overlap (Jaccard) between two normalized titles, 0.0 - 1.0."""
    left_words = set(normalize_title(left).split())
    right_words = set(normalize_title(right).split())
    if not left_words or not right_words:
        return 0.0
    return len(left_words & right_words) / len(left_words | right_words)


@dataclass(frozen=True)
class RoleCandidate:
    """One ranked role, reduced to exactly what selection needs.

    Built either from the rich Phase-3 scoring objects (see ``role_ranking``) or from a role dict
    read back out of a saved ``role_profile``, so the same rules apply to a resume analysed today
    and to a record saved before this feature existed."""

    title: str
    domain: str = ""
    job_family: str = ""
    confidence: float = 0.0
    # Position in the incoming ranked list. The final tie-break, so output is deterministic.
    order_index: int = 0
    # Family band, lowest first. The caller uses this when it has ALREADY established that one
    # group of roles is closer to the candidate's professional family than another -- most
    # importantly, roles from a detected specialization versus roles from the broad parent domain.
    # Relatedness ordering happens strictly inside a band, so it can never promote a broad role
    # over the specialization the resume actually demonstrates. Default 0: one flat band.
    family_rank: int = 0
    in_primary_family: bool = True
    has_supporting_evidence: bool = True
    seniority_compatible: bool = True
    # Whatever the caller wants handed back for a selected role (a _Scored, a role dict, ...).
    payload: object = field(default=None, compare=False)


@dataclass(frozen=True)
class Selection:
    selected: list[RoleCandidate]
    rejected: list[dict]


def relatedness(primary: RoleCandidate, candidate: RoleCandidate) -> float:
    """How close this role is to the detected primary role, 0.0 - 14.0.

    Professional-family compatibility and primary-role title similarity, and nothing else -- these
    are the two signals that must dominate which four related roles are kept."""
    score = 0.0
    if candidate.job_family and candidate.job_family == primary.job_family:
        score += FAMILY_MATCH_BONUS
    score += TITLE_SIMILARITY_WEIGHT * title_similarity(primary.title, candidate.title)
    return round(score, 4)


def selection_score(primary: RoleCandidate, candidate: RoleCandidate) -> float:
    """The ordering key for related roles: the evidence-based confidence the ranking pipeline
    already produced, plus the bounded relatedness term. Never persisted as a confidence -- a role
    is not more *credible* because it is closer to the primary, only more worth searching."""
    return round(candidate.confidence + relatedness(primary, candidate), 4)


def select_roles(candidates: list[RoleCandidate], max_roles: int = MAX_SEARCH_ROLES) -> Selection:
    """Select the primary role plus the strongest related roles, up to ``max_roles`` in total.

    ``candidates`` must already be in ranked order; ``candidates[0]`` is the primary role and is
    selected unconditionally -- no filter, no dedup and no reordering can remove or displace it.
    Returns the ordered selection and a rejection record for every role that did not make it."""
    rejected: list[dict] = []
    if not candidates:
        return Selection([], rejected)

    def reject(candidate: RoleCandidate, reason: str) -> None:
        rejected.append({"title": candidate.title, "domain": candidate.domain, "reason": reason})

    primary = candidates[0]
    selected = [primary]
    seen = {normalize_title(primary.title)}
    if max_roles <= 1:
        for candidate in candidates[1:]:
            reject(candidate, f"ranked below the top {max_roles} searched roles")
        return Selection(selected, rejected)

    # 1. Filter. Order matters only for which reason is recorded; a role rejected here can never
    #    be brought back by ranking.
    survivors: list[RoleCandidate] = []
    for candidate in candidates[1:]:
        if not candidate.in_primary_family:
            reject(candidate, f"outside the primary professional family ({primary.domain or 'primary domain'})")
        elif not candidate.seniority_compatible:
            reject(candidate, "seniority-incompatible with this resume's demonstrated level")
        elif not candidate.has_supporting_evidence:
            reject(candidate, "nothing in this resume supports this role")
        else:
            survivors.append(candidate)

    # 2. Rank the survivors: closest professional family band first, then closeness to the primary
    #    role, then the pipeline's own confidence, then incoming order -- fully deterministic,
    #    ties included.
    survivors.sort(
        key=lambda item: (item.family_rank, -selection_score(primary, item), -item.confidence, item.order_index)
    )

    # 3. Deduplicate near-identical titles, then limit. A duplicate is reported as a duplicate even
    #    when it would have been cut by the limit anyway -- the reason should say what happened.
    for candidate in survivors:
        key = normalize_title(candidate.title)
        if key in seen:
            reject(candidate, "near-identical to an already selected role")
            continue
        if len(selected) >= max_roles:
            reject(candidate, f"ranked below the top {max_roles} searched roles")
            continue
        seen.add(key)
        selected.append(candidate)
    return Selection(selected, rejected)


def _has_seniority_penalty(role: dict) -> bool:
    return any(str(note).lower().startswith("seniority") for note in role.get("penalties") or [])


def select_role_dicts(roles: list[dict], max_roles: int = MAX_SEARCH_ROLES) -> list[dict]:
    """Apply the same selection to a stored ``role_profile['roles']`` list.

    This is the read path. A resume analysed today already holds at most five roles, so this is a
    no-op for it; a record saved before this feature still holds twelve, and must stay readable --
    so the same filtering and limiting is applied on the way to the search plan instead of the
    record being rewritten or rejected.

    The returned dicts are renumbered copies (rank 1..n, Primary Role / Strong Fit), so the search
    plan is self-consistent even when a legacy record's stored ranks no longer match. The stored
    record itself is never modified."""
    usable = [role for role in roles or [] if str(role.get("title") or "").strip()]
    if not usable:
        return []
    primary_domain = str(usable[0].get("domain") or "")
    candidates = [
        RoleCandidate(
            title=str(role.get("title") or "").strip(),
            domain=str(role.get("domain") or ""),
            job_family=str(role.get("job_family") or ""),
            confidence=float(role.get("confidence") or 0),
            order_index=index,
            # The primary role is exempt from every rule; for the rest, judge only on what a saved
            # record actually carries.
            in_primary_family=index == 0 or not primary_domain or str(role.get("domain") or "") == primary_domain,
            has_supporting_evidence=index == 0 or bool(role.get("evidence")),
            seniority_compatible=index == 0 or not _has_seniority_penalty(role),
            payload=role,
        )
        for index, role in enumerate(usable)
    ]
    selection = select_roles(candidates, max_roles)
    return [
        {**candidate.payload, "rank": position + 1, "category": "Primary Role" if position == 0 else "Strong Fit"}
        for position, candidate in enumerate(selection.selected)
    ]
