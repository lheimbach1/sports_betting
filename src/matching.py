"""Fuzzy event matching across providers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from src.models.events import Event

# Common suffixes stripped during normalization.
_SUFFIX_RE = re.compile(
    r"\b(FC|AFC|CF|SC|FK|SV|BSC|SSC|AS|AC|US|RC|CD|UD|CA|RCD|SD|SE|SL|VfB|VfL|TSG|SpVgg)\b",
    re.IGNORECASE,
)
_MULTI_SPACE_RE = re.compile(r"\s+")


@dataclass
class MatchedEvent:
    """A pair of matched events from two providers."""

    sporttip: Event
    polymarket: Event
    similarity: float


def normalize_team(name: str) -> str:
    """Normalize a team name for comparison.

    Lowercases, strips common suffixes (FC, AFC, …), collapses whitespace.
    """
    result = name.lower()
    result = _SUFFIX_RE.sub("", result)
    result = result.strip()
    result = _MULTI_SPACE_RE.sub(" ", result)
    return result


def _team_similarity(a: str, b: str) -> float:
    """Compute similarity between two team names (normalized)."""
    return SequenceMatcher(None, normalize_team(a), normalize_team(b)).ratio()


def match_events(
    events_a: list[Event],
    events_b: list[Event],
) -> list[MatchedEvent]:
    """Match events across two providers using fuzzy team names and date proximity.

    Greedy best-first matching: each event is matched at most once.
    Accepts pairs with combined similarity >= 0.6.
    """
    if not events_a or not events_b:
        return []

    # Score all candidate pairs.
    candidates: list[tuple[float, int, int]] = []
    for i, ea in enumerate(events_a):
        for j, eb in enumerate(events_b):
            # Dates must be within ±1 day.
            delta = abs((ea.start_time - eb.start_time).total_seconds())
            if delta > 86400:
                continue
            home_sim = _team_similarity(ea.home_team, eb.home_team)
            away_sim = _team_similarity(ea.away_team, eb.away_team)
            score = (home_sim + away_sim) / 2.0
            if score >= 0.6:
                candidates.append((score, i, j))

    # Greedy: best score first, no duplicates.
    candidates.sort(key=lambda x: x[0], reverse=True)
    used_a: set[int] = set()
    used_b: set[int] = set()
    matched: list[MatchedEvent] = []

    for score, i, j in candidates:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        matched.append(MatchedEvent(
            sporttip=events_a[i],
            polymarket=events_b[j],
            similarity=score,
        ))

    return matched
