![CI](https://github.com/lheimbach1/sports_betting/actions/workflows/ci.yml/badge.svg?branch=feat/project-setup-and-sporttip-scraper)

# Sports Betting Arbitrage Finder

Detect arbitrage opportunities and mispriced bets across sports betting providers, with a focus on the Swiss market.

## Supported Providers

| Provider | Status | Notes |
|----------|--------|-------|
| [Sporttip](https://www.swisslos.ch/de/sporttip) (Swisslos) | In Progress | Swiss state-regulated provider |

## Features

- Pull live odds from multiple betting providers
- Detect arbitrage opportunities (guaranteed-profit bets across providers)
- Identify mispriced lines compared to market consensus
- Provider-agnostic architecture — easy to add new bookmakers
- **Live odds monitor** with configurable threshold alerts and cross-platform notifications

## Setup

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -e ".[dev]"
```

## Usage

```bash
# Fetch current odds from Sporttip
python -m src.providers.sporttip

# Run arbitrage scanner (coming soon)
python -m src.arbitrage.scanner
```

### Odds Monitor

The odds monitor is a standalone CLI tool that streams live odds from Sporttip and notifies you when odds cross a user-defined threshold.

```bash
# Launch the interactive monitor
python -m src.cli.monitor

# Or use the installed console script
sporttip-monitor
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
├── cli/             # CLI tools
│   ├── explore.py   # Interactive odds explorer
│   └── monitor.py   # Live odds monitor with threshold alerts
├── models/          # Shared data models (events, odds, etc.)
├── providers/       # Betting provider scrapers/API clients
│   └── sporttip.py  # Sporttip (Swisslos) client
└── arbitrage/       # Arbitrage detection logic
tests/
├── cli/             # CLI tool tests
├── providers/       # Provider-specific tests
└── arbitrage/       # Arbitrage logic tests
```

## Adding a New Provider

Implement the `BaseProvider` interface in `src/providers/base.py`:

```python
from src.providers.base import BaseProvider

class MyProvider(BaseProvider):
    async def fetch_events(self, sport: str) -> list[Event]:
        ...
```

## License

All rights reserved. No part of this software may be used, copied, modified, or distributed without explicit written permission from the author.
