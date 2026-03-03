"""Tests for the fuzzy event matching module."""

from datetime import datetime, timezone

import pytest

from src.matching import _team_similarity, canonicalize, match_events, normalize_team
from src.models.events import Event, Market, Outcome, Sport


def _make_event(
    home: str,
    away: str,
    dt: datetime,
    provider: str = "sporttip",
    league: str = "Premier League",
) -> Event:
    return Event(
        id=f"{home}-{away}",
        sport=Sport.FOOTBALL,
        league=league,
        home_team=home,
        away_team=away,
        start_time=dt,
        markets=[
            Market(
                name="1X2",
                outcomes=[
                    Outcome(name="1", odds=2.0),
                    Outcome(name="X", odds=3.0),
                    Outcome(name="2", odds=4.0),
                ],
            )
        ],
        provider=provider,
    )


class TestNormalizeTeam:
    def test_strips_fc(self) -> None:
        assert normalize_team("Arsenal FC") == "arsenal"

    def test_strips_afc(self) -> None:
        assert normalize_team("AFC Bournemouth") == "bournemouth"

    def test_strips_years(self) -> None:
        assert normalize_team("Como 1907") == "como"
        assert normalize_team("Bologna FC 1909") == "bologna"
        assert normalize_team("TSG 1899 Hoffenheim") == "hoffenheim"

    def test_strips_leading_number(self) -> None:
        assert normalize_team("1. FC Köln") == "köln"

    def test_collapses_spaces(self) -> None:
        assert normalize_team("  Real   Madrid   ") == "real madrid"

    def test_lowercase(self) -> None:
        assert normalize_team("BAYERN MÜNCHEN") == "bayern münchen"


class TestCanonicalize:
    """Test the alias-based canonicalization."""

    # --- Bundesliga ---
    @pytest.mark.parametrize("name,expected", [
        ("Bayern Munich", "bayern munich"),
        ("FC Bayern München", "bayern munich"),
        ("Dortmund", "dortmund"),
        ("BV Borussia 09 Dortmund", "dortmund"),
        ("Gladbach", "gladbach"),
        ("Borussia Mönchengladbach", "gladbach"),
        ("1. FC Cologne", "cologne"),
        ("1. FC Köln", "cologne"),
        ("HSV", "hsv"),
        ("Hamburger SV", "hsv"),
        ("Leverkusen", "leverkusen"),
        ("Bayer 04 Leverkusen", "leverkusen"),
        ("Werder", "werder bremen"),
        ("SV Werder Bremen", "werder bremen"),
        ("St. Pauli", "st. pauli"),
        ("FC St. Pauli 1910", "st. pauli"),
        ("Hoffenheim", "hoffenheim"),
        ("TSG 1899 Hoffenheim", "hoffenheim"),
        ("1. FC Heidenheim", "heidenheim"),
        ("1. FC Heidenheim 1846", "heidenheim"),
        ("FSV Mainz 05", "mainz"),
        ("1. FSV Mainz 05", "mainz"),
    ])
    def test_bundesliga(self, name: str, expected: str) -> None:
        assert canonicalize(name) == expected

    # --- Premier League ---
    @pytest.mark.parametrize("name,expected", [
        ("Man City", "man city"),
        ("Manchester City FC", "man city"),
        ("Man Utd", "man utd"),
        ("Manchester United FC", "man utd"),
        ("Wolves", "wolves"),
        ("Wolverhampton Wanderers FC", "wolves"),
        ("Newcastle", "newcastle"),
        ("Newcastle United FC", "newcastle"),
        ("Bournemouth", "bournemouth"),
        ("AFC Bournemouth", "bournemouth"),
        ("Brighton", "brighton"),
        ("Brighton & Hove Albion FC", "brighton"),
        ("Tottenham", "tottenham"),
        ("Tottenham Hotspur FC", "tottenham"),
        ("Nottingham", "nottingham forest"),
        ("Nottingham Forest FC", "nottingham forest"),
        ("West Ham", "west ham"),
        ("West Ham United FC", "west ham"),
    ])
    def test_premier_league(self, name: str, expected: str) -> None:
        assert canonicalize(name) == expected

    # --- LaLiga ---
    @pytest.mark.parametrize("name,expected", [
        ("Atl. Madrid", "atl. madrid"),
        ("Club Atlético de Madrid", "atl. madrid"),
        ("Ath. Bilbao", "ath. bilbao"),
        ("Athletic Club", "ath. bilbao"),
        ("Barcelona", "barcelona"),
        ("FC Barcelona", "barcelona"),
        ("Celta Vigo", "celta vigo"),
        ("RC Celta de Vigo", "celta vigo"),
        ("Real Sociedad", "real sociedad"),
        ("Real Sociedad de Fútbol", "real sociedad"),
        ("Espanyol", "espanyol"),
        ("RCD Espanyol de Barcelona", "espanyol"),
        ("Rayo Vallecano", "rayo vallecano"),
        ("Rayo Vallecano de Madrid", "rayo vallecano"),
    ])
    def test_laliga(self, name: str, expected: str) -> None:
        assert canonicalize(name) == expected

    # --- Serie A ---
    @pytest.mark.parametrize("name,expected", [
        ("Inter Milano", "inter"),
        ("FC Internazionale Milano", "inter"),
        ("Como", "como"),
        ("Como 1907", "como"),
        ("Napoli", "napoli"),
        ("SSC Napoli", "napoli"),
        ("Lazio", "lazio"),
        ("SS Lazio", "lazio"),
        ("Fiorentina", "fiorentina"),
        ("ACF Fiorentina", "fiorentina"),
        ("Bologna", "bologna"),
        ("Bologna FC 1909", "bologna"),
        ("AS Roma", "roma"),
        ("Roma", "roma"),
        ("Genoa CFC", "genoa"),
    ])
    def test_serie_a(self, name: str, expected: str) -> None:
        assert canonicalize(name) == expected

    # --- Ligue 1 ---
    @pytest.mark.parametrize("name,expected", [
        ("Paris SG", "psg"),
        ("Paris Saint-Germain FC", "psg"),
        ("Marseille", "marseille"),
        ("Olympique de Marseille", "marseille"),
        ("Lyon", "lyon"),
        ("Olympique Lyonnais", "lyon"),
        ("OSC Lille", "lille"),
        ("Lille OSC", "lille"),
        ("Rennes", "rennes"),
        ("Stade Rennais FC 1901", "rennes"),
        ("Brest", "brest"),
        ("Stade Brestois 29", "brest"),
        ("RC Strassburg", "strasbourg"),
        ("RC Strasbourg Alsace", "strasbourg"),
        ("AC Le Havre", "le havre"),
        ("Le Havre AC", "le havre"),
        ("RC Lens", "lens"),
        ("Racing Club de Lens", "lens"),
    ])
    def test_ligue_1(self, name: str, expected: str) -> None:
        assert canonicalize(name) == expected

    def test_unknown_team_falls_back_to_normalize(self) -> None:
        """Unknown teams should get basic normalization."""
        assert canonicalize("Unknown Team FC") == "unknown team"


class TestTeamSimilarity:
    def test_identical(self) -> None:
        assert _team_similarity("Arsenal", "Arsenal") == 1.0

    def test_totally_different(self) -> None:
        assert _team_similarity("Arsenal", "Xyzzy Krakow") < 0.3

    def test_with_suffix(self) -> None:
        assert _team_similarity("Arsenal", "Arsenal FC") == 1.0

    def test_alias_match_gives_perfect_score(self) -> None:
        assert _team_similarity("Inter Milano", "FC Internazionale Milano") == 1.0
        assert _team_similarity("Man City", "Manchester City FC") == 1.0
        assert _team_similarity("Wolves", "Wolverhampton Wanderers FC") == 1.0
        assert _team_similarity("Gladbach", "Borussia Mönchengladbach") == 1.0
        assert _team_similarity("HSV", "Hamburger SV") == 1.0
        assert _team_similarity("Atl. Madrid", "Club Atlético de Madrid") == 1.0
        assert _team_similarity("Paris SG", "Paris Saint-Germain FC") == 1.0
        assert _team_similarity("Lyon", "Olympique Lyonnais") == 1.0


DT = datetime(2026, 3, 15, 15, 0, tzinfo=timezone.utc)


class TestMatchEvents:
    def test_matches_similar_names_same_date(self) -> None:
        sp = [_make_event("Arsenal", "Everton", DT, provider="sporttip")]
        pm = [_make_event("Arsenal FC", "Everton FC", DT, provider="polymarket")]
        result = match_events(sp, pm)
        assert len(result) == 1
        assert result[0].sporttip.home_team == "Arsenal"
        assert result[0].polymarket.home_team == "Arsenal FC"
        assert result[0].similarity == 1.0

    def test_matches_via_alias(self) -> None:
        """Real-world case: Sporttip short names vs Polymarket full names."""
        sp = [_make_event("Como", "Inter Milano", DT, provider="sporttip")]
        pm = [_make_event("Como 1907", "FC Internazionale Milano", DT, provider="polymarket")]
        result = match_events(sp, pm)
        assert len(result) == 1
        assert result[0].similarity == 1.0

    def test_matches_german_league(self) -> None:
        sp = [_make_event("Bayern Munich", "Gladbach", DT, provider="sporttip")]
        pm = [_make_event(
            "FC Bayern München", "Borussia Mönchengladbach", DT, provider="polymarket",
        )]
        result = match_events(sp, pm)
        assert len(result) == 1
        assert result[0].similarity == 1.0

    def test_matches_laliga(self) -> None:
        sp = [_make_event("Atl. Madrid", "Real Sociedad", DT, provider="sporttip")]
        pm = [_make_event(
            "Club Atlético de Madrid", "Real Sociedad de Fútbol", DT, provider="polymarket",
        )]
        result = match_events(sp, pm)
        assert len(result) == 1

    def test_matches_ligue1(self) -> None:
        sp = [_make_event("Paris SG", "AS Monaco", DT, provider="sporttip")]
        pm = [_make_event(
            "Paris Saint-Germain FC", "AS Monaco FC", DT, provider="polymarket",
        )]
        result = match_events(sp, pm)
        assert len(result) == 1

    def test_rejects_different_dates(self) -> None:
        dt2 = datetime(2026, 3, 20, 15, 0, tzinfo=timezone.utc)
        sp = [_make_event("Arsenal", "Everton", DT, provider="sporttip")]
        pm = [_make_event("Arsenal FC", "Everton FC", dt2, provider="polymarket")]
        result = match_events(sp, pm)
        assert result == []

    def test_no_duplicates(self) -> None:
        sp = [
            _make_event("Arsenal", "Everton", DT, provider="sporttip"),
            _make_event("Arsenal", "Chelsea", DT, provider="sporttip"),
        ]
        pm = [_make_event("Arsenal FC", "Everton FC", DT, provider="polymarket")]
        result = match_events(sp, pm)
        assert len(result) == 1
        # The best match should be Arsenal-Everton, not Arsenal-Chelsea.
        assert result[0].sporttip.away_team == "Everton"

    def test_empty_lists(self) -> None:
        assert match_events([], []) == []
        sp = [_make_event("Arsenal", "Everton", DT)]
        assert match_events(sp, []) == []
        assert match_events([], sp) == []
