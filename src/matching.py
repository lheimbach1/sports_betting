"""Fuzzy event matching across providers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from src.models.events import Event

# ---------------------------------------------------------------------------
# Team-name alias map
# ---------------------------------------------------------------------------
# Each entry: (canonical_key, [known_name_variants]).
# Variants are matched case-insensitively after stripping common suffixes.
# This covers all teams in the five overlapping leagues (EPL, LaLiga,
# Bundesliga, Serie A, Ligue 1) with their Sporttip short names and
# Polymarket full official names.
_KNOWN_TEAMS: list[tuple[str, list[str]]] = [
    # --- Bundesliga ---
    ("bayern munich", [
        "bayern munich", "bayern münchen", "fc bayern münchen", "fc bayern munchen",
    ]),
    ("dortmund", [
        "dortmund", "borussia dortmund", "bv borussia 09 dortmund", "bv borussia dortmund",
    ]),
    ("gladbach", [
        "gladbach", "borussia mönchengladbach", "borussia monchengladbach",
        "mönchengladbach", "monchengladbach",
    ]),
    ("cologne", [
        "1. fc cologne", "1. fc köln", "1. fc koln", "köln", "koln", "cologne",
    ]),
    ("leverkusen", [
        "leverkusen", "bayer 04 leverkusen", "bayer leverkusen",
    ]),
    ("freiburg", ["freiburg", "sc freiburg"]),
    ("union berlin", ["union berlin", "1. fc union berlin"]),
    ("werder bremen", [
        "werder", "werder bremen", "sv werder bremen",
    ]),
    ("st. pauli", [
        "st. pauli", "fc st. pauli", "fc st. pauli 1910",
    ]),
    ("hoffenheim", [
        "hoffenheim", "tsg hoffenheim", "tsg 1899 hoffenheim",
    ]),
    ("hsv", ["hsv", "hamburger sv", "hamburger"]),
    ("mainz", [
        "mainz", "fsv mainz 05", "1. fsv mainz 05", "mainz 05",
    ]),
    ("augsburg", ["augsburg", "fc augsburg"]),
    ("heidenheim", [
        "heidenheim", "1. fc heidenheim", "1. fc heidenheim 1846",
    ]),
    ("rb leipzig", ["rb leipzig", "leipzig"]),
    ("vfb stuttgart", ["vfb stuttgart", "stuttgart"]),
    ("vfl wolfsburg", ["vfl wolfsburg", "wolfsburg"]),
    ("eintracht frankfurt", ["eintracht frankfurt", "frankfurt"]),

    # --- Premier League ---
    ("man city", ["man city", "manchester city"]),
    ("man utd", ["man utd", "manchester united"]),
    ("newcastle", ["newcastle", "newcastle united"]),
    ("wolves", ["wolves", "wolverhampton wanderers", "wolverhampton"]),
    ("bournemouth", ["bournemouth", "afc bournemouth"]),
    ("arsenal", ["arsenal"]),
    ("aston villa", ["aston villa"]),
    ("brighton", ["brighton", "brighton & hove albion", "brighton and hove albion"]),
    ("chelsea", ["chelsea"]),
    ("crystal palace", ["crystal palace"]),
    ("everton", ["everton"]),
    ("fulham", ["fulham"]),
    ("liverpool", ["liverpool"]),
    ("tottenham", ["tottenham", "tottenham hotspur", "spurs"]),
    ("brentford", ["brentford"]),
    ("burnley", ["burnley"]),
    ("west ham", ["west ham", "west ham united"]),
    ("nottingham forest", ["nottingham", "nottingham forest"]),
    ("leeds", ["leeds", "leeds united"]),
    ("sunderland", ["sunderland"]),

    # --- LaLiga ---
    ("barcelona", ["barcelona", "fc barcelona", "barça", "barca"]),
    ("atl. madrid", [
        "atl. madrid", "atletico madrid", "atlético madrid",
        "club atletico de madrid", "club atlético de madrid",
    ]),
    ("real madrid", ["real madrid"]),
    ("real sociedad", [
        "real sociedad", "real sociedad de futbol", "real sociedad de fútbol",
    ]),
    ("ath. bilbao", [
        "ath. bilbao", "athletic bilbao", "athletic club",
    ]),
    ("villarreal", ["villarreal"]),
    ("real betis", ["real betis", "real betis balompie", "real betis balompié"]),
    ("celta vigo", ["celta vigo", "rc celta de vigo", "celta de vigo", "celta"]),
    ("sevilla", ["sevilla"]),
    ("osasuna", ["osasuna", "ca osasuna"]),
    ("getafe", ["getafe"]),
    ("girona", ["girona"]),
    ("valencia", ["valencia"]),
    ("alaves", [
        "alaves", "alavés", "cd alaves", "cd alavés",
        "deportivo alaves", "deportivo alavés",
    ]),
    ("espanyol", [
        "espanyol", "rcd espanyol", "rcd espanyol de barcelona",
    ]),
    ("rayo vallecano", ["rayo vallecano", "rayo vallecano de madrid"]),
    ("levante", ["levante"]),
    ("elche", ["elche"]),
    ("mallorca", ["mallorca", "rcd mallorca"]),
    ("real oviedo", ["real oviedo", "oviedo"]),
    ("leganes", ["leganes", "leganés", "cd leganes", "cd leganés"]),
    ("valladolid", ["valladolid", "real valladolid"]),
    ("las palmas", ["las palmas", "ud las palmas"]),

    # --- Serie A ---
    ("inter", [
        "inter", "inter milano", "inter milan",
        "fc internazionale milano", "internazionale",
    ]),
    ("ac milan", ["ac milan", "milan"]),
    ("fiorentina", ["fiorentina", "acf fiorentina"]),
    ("atalanta", ["atalanta"]),
    ("bologna", ["bologna", "bologna fc 1909"]),
    ("cagliari", ["cagliari", "cagliari calcio"]),
    ("como", ["como", "como 1907"]),
    ("juventus", ["juventus"]),
    ("lazio", ["lazio", "ss lazio"]),
    ("lecce", ["lecce", "us lecce"]),
    ("napoli", ["napoli", "ssc napoli"]),
    ("parma", ["parma", "parma calcio", "parma calcio 1913"]),
    ("torino", ["torino"]),
    ("udinese", ["udinese", "udinese calcio"]),
    ("sassuolo", ["sassuolo", "us sassuolo calcio", "us sassuolo"]),
    ("hellas verona", ["hellas verona"]),
    ("genoa", ["genoa", "genoa cfc"]),
    ("roma", ["roma", "as roma"]),
    ("pisa", ["pisa"]),
    ("cremonese", ["cremonese", "us cremonese"]),
    ("monza", ["monza"]),
    ("empoli", ["empoli"]),
    ("venezia", ["venezia"]),

    # --- Ligue 1 ---
    ("psg", ["paris sg", "paris saint-germain", "psg"]),
    ("marseille", ["marseille", "olympique de marseille", "om"]),
    ("lyon", ["lyon", "olympique lyonnais", "ol"]),
    ("monaco", ["monaco", "as monaco"]),
    ("lille", ["lille", "osc lille", "lille osc"]),
    ("rennes", ["rennes", "stade rennais", "stade rennais fc 1901"]),
    ("brest", ["brest", "stade brestois 29", "stade brestois"]),
    ("strasbourg", [
        "strasbourg", "strassburg",
        "rc strassburg", "rc strasbourg alsace", "rc strasbourg",
    ]),
    ("lens", ["lens", "rc lens", "racing club de lens"]),
    ("auxerre", ["auxerre", "aj auxerre"]),
    ("nantes", ["nantes", "fc nantes"]),
    ("angers", ["angers", "angers sco"]),
    ("lorient", ["lorient", "fc lorient"]),
    ("metz", ["metz", "fc metz"]),
    ("toulouse", ["toulouse"]),
    ("le havre", ["le havre", "ac le havre", "le havre ac"]),
    ("nice", ["nice", "ogc nice"]),
    ("paris fc", ["paris fc"]),
    ("montpellier", ["montpellier", "montpellier hsc"]),
    ("reims", ["reims", "stade de reims"]),
    ("saint-etienne", ["saint-etienne", "st. etienne", "as saint-etienne", "as saint-étienne"]),
]

# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

# Suffixes/prefixes to strip (order matters: longer first to avoid partial).
_STRIP_RE = re.compile(
    r"\b("
    r"FC|AFC|CF|SC|FK|SV|BSC|SSC|AS|AC|US|RC|CD|UD|CA|RCD|SD|SE|SL|"
    r"VfB|VfL|TSG|SpVgg|BV|ACF|CFC|BC|OGC|AJ|OSC|SCO|HSC"
    r")\b",
    re.IGNORECASE,
)
# Year numbers (2-4 digits standing alone, e.g. "1907", "05", "29").
_YEAR_RE = re.compile(r"\b\d{2,4}\b")
# Leading "1." prefix (e.g. "1. FC Köln").
_LEADING_NUM_RE = re.compile(r"^\d+\.\s*")
_MULTI_SPACE_RE = re.compile(r"\s+")


@dataclass
class MatchedEvent:
    """A pair of matched events from two providers."""

    sporttip: Event
    polymarket: Event
    similarity: float


def normalize_team(name: str) -> str:
    """Normalize a team name for comparison.

    Lowercases, strips common suffixes/prefixes, year numbers,
    and collapses whitespace.
    """
    result = name.lower()
    result = _STRIP_RE.sub("", result)
    result = _YEAR_RE.sub("", result)
    result = _LEADING_NUM_RE.sub("", result)
    result = _MULTI_SPACE_RE.sub(" ", result)
    return result.strip()


# ---------------------------------------------------------------------------
# Alias lookup (built once at import time)
# ---------------------------------------------------------------------------

def _build_alias_lookup() -> dict[str, str]:
    """Build a dict mapping normalized variant → canonical key."""
    lookup: dict[str, str] = {}
    for canonical, variants in _KNOWN_TEAMS:
        for variant in variants:
            # Store both the raw lowercase and the normalized form.
            lookup[variant.lower()] = canonical
            lookup[normalize_team(variant)] = canonical
    return lookup


_ALIAS_LOOKUP: dict[str, str] = _build_alias_lookup()


def canonicalize(name: str) -> str:
    """Map a team name to its canonical form via alias lookup.

    Falls back to normalize_team() if no alias is found.
    """
    lower = name.lower().strip()
    if lower in _ALIAS_LOOKUP:
        return _ALIAS_LOOKUP[lower]
    normalized = normalize_team(name)
    if normalized in _ALIAS_LOOKUP:
        return _ALIAS_LOOKUP[normalized]
    return normalized


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------

def _team_similarity(a: str, b: str) -> float:
    """Compute similarity between two team names.

    Uses canonical alias lookup first (exact match = 1.0),
    then falls back to fuzzy SequenceMatcher on normalized forms.
    """
    ca = canonicalize(a)
    cb = canonicalize(b)
    if ca == cb:
        return 1.0
    return SequenceMatcher(None, ca, cb).ratio()


# ---------------------------------------------------------------------------
# Event matching
# ---------------------------------------------------------------------------

def match_events(
    events_a: list[Event],
    events_b: list[Event],
) -> list[MatchedEvent]:
    """Match events across two providers using team-name aliases and date proximity.

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
