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
├── models/          # Shared data models (events, odds, etc.)
├── providers/       # Betting provider scrapers/API clients
│   └── sporttip.py  # Sporttip (Swisslos) client
└── arbitrage/       # Arbitrage detection logic
tests/
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
