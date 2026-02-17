# Esporf — eSoccer Betting Edge Finder

Aggregates odds data from multiple sportsbooks for eSoccer leagues and identifies sharp betting edges.

## Tracked Leagues

| League | Format | BetsAPI ID |
|--------|--------|------------|
| eSoccer GT Leagues | 12 min | 23114 |
| eSoccer GG League | 8 min | 37298 |
| eSoccer Volta | 6 min | 38439 |

## Edge Detection Strategies

1. **Line Discrepancy** — Finds outlier odds at soft books compared to the sharp market consensus (Pinnacle/Bet365 devigged)
2. **Steam Moves** — Detects rapid line movements at sharp books and flags soft books that haven't adjusted
3. **CLV (Closing Line Value)** — Tracks odds movements over time and identifies books lagging behind the market trend
4. **Stats Model** — Uses historical player stats (goals scored/conceded, win rate) to estimate fair match probabilities and flags mispriced odds

## Setup

```bash
# Clone and install
git clone <repo-url> && cd Esporf
pip install -e ".[dev]"

# Configure
cp .env.example .env
# Edit .env with your BetsAPI token (get one at https://betsapi.com — $10/mo)
```

## Usage

```bash
# Run continuous scanning bot
esporf run

# Run a single scan
esporf scan

# View current odds for all tracked matches
esporf odds

# View cached player statistics
esporf stats

# With debug logging
esporf -v run

# Custom poll interval (seconds)
esporf run --interval 60
```

## Configuration (.env)

| Variable | Default | Description |
|----------|---------|-------------|
| `BETSAPI_TOKEN` | — | Your BetsAPI token (required) |
| `LEAGUE_IDS` | `23114,37298,38439` | Comma-separated BetsAPI league IDs |
| `POLL_INTERVAL` | `120` | Seconds between scans |
| `MIN_EDGE_PERCENT` | `3.0` | Minimum EV% to alert |
| `MIN_ODDS_DIFFERENCE` | `0.10` | Minimum decimal odds gap |
| `CLV_LOOKBACK_HOURS` | `4` | Hours of history for CLV analysis |
| `DISCORD_WEBHOOK_URL` | — | Discord webhook for alerts |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token for alerts |
| `TELEGRAM_CHAT_ID` | — | Telegram chat ID for alerts |

## Data Sources

- **BetsAPI** ($10/mo) — Primary source for odds from Bet365, Pinnacle, and other books
- **Sofascore** (free) — Supplemental live scores and match data
- **TotalCorner** (free) — Player statistics, over/under rates, corner stats

## Project Structure

```
esporf/
├── models.py              # Core data models (Match, OddsLine, Edge)
├── config.py              # Settings from .env
├── bot.py                 # Main polling loop
├── cli.py                 # CLI entry point
├── sources/
│   ├── betsapi.py         # BetsAPI client (odds)
│   ├── scrapers.py        # Sofascore + TotalCorner scrapers (stats)
│   └── aggregator.py      # Combines all sources
├── analysis/
│   ├── odds_math.py       # Vig removal, EV calc, Kelly criterion
│   └── edge_detector.py   # Edge detection engine
└── alerts/
    ├── console.py         # Rich terminal output
    └── webhooks.py        # Discord/Telegram alerts
```

## Running Tests

```bash
pip install -e ".[dev]"
pytest
```
