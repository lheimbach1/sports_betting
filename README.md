![CI](https://github.com/lheimbach1/sports_betting/actions/workflows/ci.yml/badge.svg)

# Sports Betting Arbitrage Finder

Detect arbitrage opportunities and mispriced bets across sports betting providers, with a focus on the Swiss market.

## Supported Providers

| Provider | Type | Notes |
|----------|------|-------|
| [Sporttip](https://www.swisslos.ch/de/sporttip) (Swisslos) | Sportsbook | Swiss state-regulated provider; WebSocket streaming with Playwright scraping |
| [Polymarket](https://polymarket.com) | Prediction market | Decentralized prediction market via Gamma REST API |

## Features

- Pull live odds from multiple betting providers
- Detect arbitrage opportunities (guaranteed-profit bets across providers)
- Compute overround and implied probability margins per provider
- Optimal stake allocation for arb opportunities
- Fuzzy event matching across providers (team-name normalization, alias lookup, date proximity)
- Provider-agnostic architecture — easy to add new bookmakers
- **Interactive odds explorer** with drill-down navigation (sport → league → event → market)
- **Live odds monitor** with configurable threshold alerts and cross-platform desktop notifications
- **Cross-provider comparison** table with arbitrage profit margin and overround columns
- **Continuous arbitrage scanner** — live monitoring with desktop notifications when arb opportunities appear

## Setup

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies (includes Playwright for Sporttip scraping)
pip install -e ".[dev]"

# Install Playwright browsers
playwright install chromium
```

## CLI Tools

### `sporttip-explore` — Interactive Odds Explorer

Browse Sporttip odds interactively with drill-down navigation through sports, leagues, events, and markets. Supports arbitrage scanning and overround analysis.

```bash
sporttip-explore
sporttip-explore --debug
```

**Features:**

- Drill-down navigation: sport → league → event → market
- Overround analysis per market
- Arbitrage opportunity scanning across events
- Text filtering at every selection step for quick navigation

### `sporttip-monitor` — Live Odds Monitor

Stream live odds from Sporttip and receive desktop notifications when odds cross a user-defined threshold.

```bash
sporttip-monitor
sporttip-monitor --debug
```

**Setup flow:**

1. Pick a sport (or choose **Live** to browse in-play events)
2. Select a category / sub-category
3. Choose an event, market, and outcome
4. Set a direction (`>=` or `<=`) and threshold odds value
5. Optionally add more alerts — all alerts share one WebSocket stream
6. The monitor begins streaming and redraws a live summary table in-place

**Features:**

- Threshold alerts with `>=` (odds rise) or `<=` (odds drop) direction
- Cooldown state machine (WATCHING → TRIGGERED → COOLDOWN → WATCHING) prevents duplicate alerts
- Cross-platform persistent notifications — macOS (`display alert` + Glass sound) and Windows (PowerShell `MessageBox` + system sound)
- Approximate live match clock (e.g. `1H ~23'`, `2H ~67'`, `HT`, `FT`)
- Full market odds display showing all outcomes alongside the monitored one
- In-place terminal rendering with ANSI colors (no flicker)
- Text filtering at every selection step for quick navigation

### `sporttip-compare` — Cross-Provider Comparison

Compare match odds between Sporttip and Polymarket, ranked by arbitrage profit margin. Supports both 3-way (1X2) and 2-way (moneyline) markets.

```bash
# Compare all available sports and leagues
sporttip-compare

# Filter to a single sport
sporttip-compare --sport football

# Filter to a single league
sporttip-compare --league "Premier League"

# Only show matches above a minimum arbitrage margin
sporttip-compare --min-margin 0
```

| Flag | Default | Description |
|------|---------|-------------|
| `--sport` | `all` | Sport to compare (`football`, `ice_hockey`, `basketball`, `tennis`, `handball`) |
| `--league` | all | Filter to a single league (e.g. `Premier League`) |
| `--min-margin` | `-100` | Only show matches with arb margin ≥ this threshold (%) |

**How it works:**

The tool fetches odds from both providers in parallel, fuzzy-matches events by team name and kickoff date, then computes the **arbitrage profit margin** for each match. For every outcome, it picks the best odds across both providers and calculates:

```
margin = (1 / (1/best_1 + 1/best_X + 1/best_2) - 1) × 100%
```

A **positive margin** means a guaranteed-profit arbitrage exists by placing bets on different outcomes at different providers.

**Table output** includes per-provider odds with stake allocations embedded inline (e.g. `1.50 34%` means bet 34% of your budget at odds 1.50), overround percentages, and Polymarket volume. Results are sorted by margin descending.

### `sporttip-scan` — Continuous Arbitrage Scanner

Polls Sporttip and Polymarket continuously to detect cross-provider arbitrage opportunities in real time, with desktop notifications.

```bash
# Start scanning football (default settings)
sporttip-scan

# Scan a different sport with custom settings
sporttip-scan --sport ice_hockey --interval 15 --min-margin 1.0
```

| Flag | Default | Description |
|------|---------|-------------|
| `--sport` | `football` | Sport to scan (`football`, `ice_hockey`, `basketball`, `tennis`, `handball`) |
| `--interval` | `30` | Seconds between Polymarket polls |
| `--min-margin` | `0.0` | Minimum arbitrage margin (%) for notifications; arb must be strictly above this value |

**How it works:**

1. Fetches Sporttip and Polymarket events in parallel every `--interval` seconds
2. Fuzzy-matches events across providers and computes arbitrage margins
3. When a new arb crosses strictly above `--min-margin`, sends a desktop notification with odds and recommended stake allocation
4. Displays a live-updating terminal dashboard showing all matched events, sorted by margin

**Notification rules:**

- **New arb** — margin crosses strictly above the threshold for the first time: notifies immediately
- **Improved arb** — margin increased by >1 percentage point since last notification AND 5-minute cooldown expired: re-notifies
- **Expired arb** — margin drops to or below threshold, or match disappears: removed from active set

Stop the scanner at any time with **Ctrl+C**.

## Supported Sports

| Sport | Sporttip | Polymarket | Cross-Provider Matching |
|-------|----------|------------|------------------------|
| Football | All leagues (dynamic discovery) | Per-league tags + Games tag | Yes |
| Basketball | NBA | NBA tag + Games tag | Yes |
| Ice Hockey | NHL | NHL tag + Games tag | Yes |
| Tennis | ATP Singles, WTA Singles | Games tag | Yes |
| Handball | All leagues | Games tag | Yes |
| Motor Sports | F1, etc. | — | No |

## Development

```bash
# Run tests
pytest

# Run linter
ruff check src/ tests/

# Run type checker
mypy src/
```

## Project Structure

```
src/
├── cli/                   # CLI tools
│   ├── explore.py         # Interactive Sporttip odds explorer
│   ├── monitor.py         # Live odds monitor with threshold alerts
│   ├── compare.py         # Cross-provider odds comparison with arb margins
│   └── scanner.py         # Continuous arbitrage scanner with notifications
├── matching.py            # Fuzzy event matching across providers
├── models/
│   └── events.py          # Shared data models (Sport, Event, Market, Outcome)
├── providers/
│   ├── base.py            # BaseProvider abstract interface
│   ├── sporttip.py        # Sporttip (Swisslos) WebSocket + Playwright client
│   └── polymarket.py      # Polymarket Gamma REST API client
└── arbitrage/
    └── calculator.py      # Arbitrage detection, overround, stake allocation
tests/
├── cli/                   # CLI tool tests
├── providers/             # Provider-specific tests
├── arbitrage/             # Arbitrage logic tests
├── test_matching.py       # Matching logic tests
└── test_models.py         # Data model tests
```

## Adding a New Provider

Implement the `BaseProvider` interface in `src/providers/base.py`:

```python
from src.providers.base import BaseProvider

class MyProvider(BaseProvider):
    async def fetch_events(self, sport: Sport, **kwargs) -> list[Event]:
        ...
```

## License

All rights reserved. No part of this software may be used, copied, modified, or distributed without explicit written permission from the author.
