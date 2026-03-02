from abc import ABC, abstractmethod

from src.models.events import Event, Sport


class BaseProvider(ABC):
    """Base class for all betting provider integrations."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider display name."""

    @abstractmethod
    async def fetch_events(self, sport: Sport) -> list[Event]:
        """Fetch all available events with odds for a given sport."""

    async def fetch_all_sports(self) -> list[Event]:
        """Fetch events across all supported sports."""
        events: list[Event] = []
        for sport in Sport:
            events.extend(await self.fetch_events(sport))
        return events
