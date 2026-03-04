"""Tests for the Sporttip provider's snapshot parsing logic."""

from src.models.events import Sport
from src.providers.sporttip import (
    DEFAULT_LEAGUES,
    LEAGUE_URLS,
    Snapshot,
    SporttipProvider,
    _deflate_decode,
    _get_translated_name,
    _process_ws_message,
    _resolve_league_url,
    _slugify_league,
)


class TestDeflateCode:
    def test_roundtrip(self) -> None:
        import zlib

        original = b'{"ping":{}}'
        compressed = zlib.compress(original)[2:-4]  # raw deflate (strip header/checksum)
        result = _deflate_decode(compressed)
        assert result == {"ping": {}}


class TestGetTranslatedName:
    def test_returns_english(self) -> None:
        entity = {"name": "Fussball", "translations": {"en": "Football", "de": "Fussball"}}
        assert _get_translated_name(entity) == "Football"

    def test_falls_back_to_name(self) -> None:
        entity = {"name": "Soccer", "translations": {}}
        assert _get_translated_name(entity) == "Soccer"

    def test_empty_entity(self) -> None:
        assert _get_translated_name({}) == ""


class TestSnapshot:
    def _make_snapshot_with_match(self) -> Snapshot:
        """Create a snapshot with Real Madrid vs Getafe and a 1X2 market."""
        snapshot = Snapshot()
        snapshot.apply_update([
            {
                "type": "Competition",
                "kind": 0,
                "entity": {
                    "urn": "asw:competition:1",
                    "name": "LaLiga",
                    "translations": {"en": "LaLiga"},
                },
            },
            {
                "type": "Competitor",
                "kind": 0,
                "entity": {
                    "urn": "asw:competitor:100",
                    "name": "Real Madrid",
                    "translations": {"en": "Real Madrid"},
                },
            },
            {
                "type": "Competitor",
                "kind": 0,
                "entity": {
                    "urn": "asw:competitor:200",
                    "name": "Getafe CF",
                    "translations": {"en": "Getafe CF"},
                },
            },
            {
                "type": "MarketType",
                "kind": 0,
                "entity": {
                    "urn": "asw:markettype:1",
                    "name": "1x2",
                    "translations": {"en": "Final Result"},
                },
            },
            {
                "type": "SelectionType",
                "kind": 0,
                "entity": {
                    "urn": "asw:selectiontype:1",
                    "name": "1",
                    "translations": {"en": "1"},
                },
            },
            {
                "type": "SelectionType",
                "kind": 0,
                "entity": {
                    "urn": "asw:selectiontype:2",
                    "name": "X",
                    "translations": {"en": "X"},
                },
            },
            {
                "type": "SelectionType",
                "kind": 0,
                "entity": {
                    "urn": "asw:selectiontype:3",
                    "name": "2",
                    "translations": {"en": "2"},
                },
            },
            {
                "type": "Selection",
                "kind": 0,
                "entity": {
                    "urn": "sel:1",
                    "type": "asw:selectiontype:1",
                    "odds": 1.30,
                    "state": 1,
                },
            },
            {
                "type": "Selection",
                "kind": 0,
                "entity": {
                    "urn": "sel:2",
                    "type": "asw:selectiontype:2",
                    "odds": 5.20,
                    "state": 1,
                },
            },
            {
                "type": "Selection",
                "kind": 0,
                "entity": {
                    "urn": "sel:3",
                    "type": "asw:selectiontype:3",
                    "odds": 10.0,
                    "state": 1,
                },
            },
            {
                "type": "Market",
                "kind": 0,
                "entity": {
                    "urn": "market:1",
                    "type": "asw:markettype:1",
                    "state": 1,
                    "selections": ["sel:1", "sel:2", "sel:3"],
                },
            },
            {
                "type": "Event",
                "kind": 0,
                "entity": {
                    "urn": "asw:event:12345",
                    "name": "Real Madrid : Getafe CF",
                    "startTime": "2026-03-02T20:00:00Z",
                    "competition": "asw:competition:1",
                    "eventCompetitors": [
                        {"qualifier": "home", "competitor": "asw:competitor:100"},
                        {"qualifier": "away", "competitor": "asw:competitor:200"},
                    ],
                    "markets": ["market:1"],
                },
            },
        ])
        return snapshot

    def test_apply_update_populates_entities(self) -> None:
        snapshot = self._make_snapshot_with_match()
        assert len(snapshot.events) == 1
        assert len(snapshot.markets) == 1
        assert len(snapshot.selections) == 3
        assert len(snapshot.competitors) == 2

    def test_build_events_returns_correct_structure(self) -> None:
        snapshot = self._make_snapshot_with_match()
        events = snapshot.build_events(Sport.FOOTBALL)

        assert len(events) == 1
        event = events[0]
        assert event.home_team == "Real Madrid"
        assert event.away_team == "Getafe CF"
        assert event.league == "LaLiga"
        assert event.provider == "sporttip"
        assert event.sport == Sport.FOOTBALL

    def test_build_events_has_odds(self) -> None:
        snapshot = self._make_snapshot_with_match()
        events = snapshot.build_events(Sport.FOOTBALL)
        event = events[0]

        assert len(event.markets) == 1
        market = event.markets[0]
        assert market.name == "Final Result"
        assert len(market.outcomes) == 3
        assert market.outcomes[0].name == "1"
        assert market.outcomes[0].odds == 1.30
        assert market.outcomes[1].name == "X"
        assert market.outcomes[1].odds == 5.20
        assert market.outcomes[2].name == "2"
        assert market.outcomes[2].odds == 10.0

    def test_odds_update_detected(self) -> None:
        snapshot = self._make_snapshot_with_match()

        changed = snapshot.apply_update([
            {
                "type": "Selection",
                "kind": 0,
                "entity": {
                    "urn": "sel:1",
                    "type": "asw:selectiontype:1",
                    "odds": 1.45,
                    "state": 1,
                },
            },
        ])

        assert changed == ["sel:1"]
        assert snapshot.selections["sel:1"]["odds"] == 1.45

    def test_no_change_when_odds_same(self) -> None:
        snapshot = self._make_snapshot_with_match()

        changed = snapshot.apply_update([
            {
                "type": "Selection",
                "kind": 0,
                "entity": {
                    "urn": "sel:1",
                    "type": "asw:selectiontype:1",
                    "odds": 1.30,
                    "state": 1,
                },
            },
        ])

        assert changed == []

    def test_entity_removal(self) -> None:
        snapshot = self._make_snapshot_with_match()
        assert "asw:event:12345" in snapshot.events

        snapshot.apply_update([
            {
                "type": "Event",
                "kind": 1,
                "entity": {"urn": "asw:event:12345"},
            },
        ])

        assert "asw:event:12345" not in snapshot.events

    def test_suspended_selection_excluded(self) -> None:
        snapshot = self._make_snapshot_with_match()

        # Suspend one selection (state != 1)
        snapshot.apply_update([
            {
                "type": "Selection",
                "kind": 0,
                "entity": {
                    "urn": "sel:3",
                    "type": "asw:selectiontype:3",
                    "odds": 10.0,
                    "state": 0,
                },
            },
        ])

        events = snapshot.build_events(Sport.FOOTBALL)
        market = events[0].markets[0]
        assert len(market.outcomes) == 2  # only 1 and X remain


class TestProcessWsMessage:
    def test_processes_snapshot_update(self) -> None:
        import json
        import zlib

        inner_payload = json.dumps([{
            "type": "SportsbookSnapshotUpdated",
            "body": {
                "snapshotUpdate": {
                    "snapshotUpdateItems": [
                        {
                            "type": "Selection",
                            "kind": 0,
                            "entity": {
                                "urn": "sel:99",
                                "type": "asw:selectiontype:1",
                                "odds": 2.5,
                                "state": 1,
                            },
                        }
                    ]
                }
            },
        }])
        msg = json.dumps({"payload": inner_payload})
        # Compress with raw deflate
        compressed = zlib.compress(msg.encode())[2:-4]

        snapshot = Snapshot()
        result = _process_ws_message(compressed, snapshot)

        assert result is None  # no prior selection, so no "change" detected
        assert "sel:99" in snapshot.selections
        assert snapshot.selections["sel:99"]["odds"] == 2.5

    def test_ignores_non_snapshot_messages(self) -> None:
        import json
        import zlib

        msg = json.dumps({"pong": {}})
        compressed = zlib.compress(msg.encode())[2:-4]

        snapshot = Snapshot()
        result = _process_ws_message(compressed, snapshot)
        assert result is None


class TestLeagueUrlResolution:
    def test_known_league_returns_url(self) -> None:
        assert _resolve_league_url("Bundesliga", Sport.FOOTBALL) == "/football/germany/bundesliga"
        result = _resolve_league_url("Premier League", Sport.FOOTBALL)
        assert result == "/football/england/premier-league"

    def test_unknown_league_uses_slugified_fallback(self) -> None:
        url = _resolve_league_url("K League 1", Sport.FOOTBALL)
        assert url == "/football/k-league-1"

    def test_slugify_simple(self) -> None:
        assert _slugify_league("Premier League") == "premier-league"

    def test_slugify_dots_removed(self) -> None:
        assert _slugify_league("2. Bundesliga") == "2-bundesliga"

    def test_slugify_special_chars(self) -> None:
        assert _slugify_league("Süper Lig") == "s-per-lig"

    def test_default_leagues_are_known(self) -> None:
        for league in DEFAULT_LEAGUES:
            assert league in LEAGUE_URLS

    def test_all_known_leagues_start_with_sport_prefix(self) -> None:
        valid_prefixes = ("/football/", "/basketball/", "/ice-hockey/", "/tennis/")
        for league, url in LEAGUE_URLS.items():
            assert url.startswith(valid_prefixes), f"{league}: {url}"


class TestSporttipProvider:
    def test_provider_name(self) -> None:
        provider = SporttipProvider()
        assert provider.name == "Sporttip"
