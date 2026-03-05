from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class Sport(str, Enum):
    FOOTBALL = "football"
    ICE_HOCKEY = "ice_hockey"
    TENNIS = "tennis"
    BASKETBALL = "basketball"
    HANDBALL = "handball"
    MOTOR_SPORTS = "motor_sports"


class Outcome(BaseModel):
    """A single selectable outcome within a market (e.g. 'Home Win' at 2.10)."""

    name: str
    odds: float


class Market(BaseModel):
    """A betting market for an event (e.g. '1X2', 'Over/Under 2.5')."""

    name: str
    outcomes: list[Outcome]


class Event(BaseModel):
    """A sporting event with its associated betting markets."""

    id: str
    sport: Sport
    league: str
    home_team: str = ""
    away_team: str = ""
    start_time: datetime
    markets: list[Market]
    provider: str
    volume: float | None = None


# ---------------------------------------------------------------------------
# Multi-book / Kelly data models
# ---------------------------------------------------------------------------


@dataclass
class BookmakerOdds:
    """A single bookmaker's odds for one outcome."""

    bookmaker: str
    odds: float


@dataclass
class MultiBookMarket:
    """Aggregated multi-bookmaker odds for one market."""

    event_id: str
    sport: Sport
    league: str
    home_team: str
    away_team: str
    start_time: datetime
    outcome_names: tuple[str, ...]
    odds_by_outcome: dict[str, list[BookmakerOdds]]


@dataclass
class FairValue:
    """Consensus fair probabilities derived from multi-book odds."""

    outcome_names: tuple[str, ...]
    fair_probs: tuple[float, ...]
    sharp_odds: tuple[float, ...]


@dataclass
class KellyRecommendation:
    """Per-outcome Kelly recommendation."""

    outcome_name: str
    provider: str
    offered_odds: float
    fair_prob: float
    edge: float
    full_kelly_fraction: float
    fractional_kelly: float
    expected_value: float


@dataclass
class KellyAnalysis:
    """Full Kelly analysis for one event."""

    home: str
    away: str
    league: str
    kickoff: str
    fair_value: FairValue
    recommendations: list[KellyRecommendation]
    bankroll: float
    total_stake_pct: float = 0.0
    source: str = ""
    n_bookmakers: int | None = None
    multi_book: MultiBookMarket | None = None
    sp_odds_by_outcome: dict[str, float] | None = None
