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
