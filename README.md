# Esporf — eSoccer Trend Finder

Scrapes all match results from eSoccer leagues, stores them in a database, and surfaces high-confidence betting trends when players face off.

**Example output:** "Player A vs Player B — Over 5.5 goals in 15/20 matches (75%)" — anything hitting 70%+ gets sent to Discord.

## Tracked Leagues

| League | Format | BetsAPI ID |
|--------|--------|------------|
| GT Leagues | 12 min | 23114 |
| GG League | 8 min | 37298 |
| Volta | 6 min | 38439 |

## How It Works

1. **Backfills** historical match results from BetsAPI into a local SQLite database
2. **Polls** for upcoming matches every 2 minutes
3. For each upcoming matchup, **analyzes** the players' history:
   - **Head-to-Head trends** — what happens when these two specifically face each other
   - **Player overall trends** — how a player performs across all opponents
   - **Home/Away splits** — performance differences by side
4. **Filters** for trends hitting 70%+ over the last 20 matches (configurable)
5. **Sends alerts** to Discord/Telegram with the qualifying trends

## Trend Categories Checked

- Over/Under X.5 total goals (2.5, 3.5, 4.5, 5.5, 6.5, 7.5)
- Over/Under X.5 player goals scored (0.5, 1.5, 2.5, 3.5)
- Over/Under X.5 player goals conceded
- Both Teams to Score (BTTS) Yes/No
- Player win rate
- Clean sheet rate
- H2H-specific versions of all the above

## Setup

```bash
pip install -e ".[dev]"
cp .env.example .env
# Edit .env — add your BetsAPI token (get one at https://betsapi.com — $10/mo)
```

## Usage

```bash
# Backfill historical data (run once)
esporf backfill --pages 20

# Start continuous scanning bot
esporf run

# Run a single scan
esporf scan

# Look up a specific player's trends
esporf player "PlayerName"

# Check head-to-head between two players
esporf h2h "Player A" "Player B"

# View database stats
esporf db

# Debug mode
esporf -v run
```

## Configuration (.env)

| Variable | Default | Description |
|----------|---------|-------------|
| `BETSAPI_TOKEN` | — | Your BetsAPI token (required) |
| `LEAGUE_IDS` | `23114,37298,38439` | Comma-separated league IDs |
| `POLL_INTERVAL` | `120` | Seconds between scans |
| `MIN_HIT_RATE` | `0.70` | Minimum trend hit rate (70%) |
| `MIN_SAMPLE_SIZE` | `10` | Minimum matches for a valid trend |
| `LAST_N_MATCHES` | `20` | How many recent matches to analyze |
| `GOAL_LINES` | `2.5,3.5,4.5,5.5,6.5,7.5` | Goal lines to check |
| `BACKFILL_PAGES` | `10` | Pages of history to fetch on first run |
| `DISCORD_WEBHOOK_URL` | — | Discord webhook for alerts |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token |
| `TELEGRAM_CHAT_ID` | — | Telegram chat ID |

## Project Structure

```
esporf/
├── models.py              # MatchResult, UpcomingMatch, Trend, MatchupReport
├── config.py              # Settings from .env
├── database.py            # SQLite match history store
├── bot.py                 # Main polling loop
├── cli.py                 # CLI entry point
├── sources/
│   └── betsapi.py         # BetsAPI client (match results + upcoming)
├── analysis/
│   └── trends.py          # Trend analyzer (H2H, player, home/away)
└── alerts/
    ├── console.py         # Rich terminal output
    └── webhooks.py        # Discord/Telegram alerts
```

## Running Tests

```bash
pip install -e ".[dev]"
python -m pytest -v
```
