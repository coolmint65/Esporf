# Esporf — eSoccer Odds Bot

Automated esports betting analysis tool. Scrapes match history, detects trends, and generates picks with sportsbook odds validation.

## Quick Start

```bash
# Backend (Python 3.10+)
pip install -e ".[dev]"
esporf              # CLI entry point — starts the bot

# Frontend (React 19 + Vite 7)
cd frontend
npm install
npm run dev         # dev server on localhost:5173

# Both together (production)
docker compose up
```

## Project Structure

```
esporf/
  bot.py            # Main bot loop — scan_once(), backfill, resolve picks
  models.py         # Core dataclasses: Match, MatchupReport, BetPick, best_bet()
  database.py       # SQLite schema + MatchDatabase (all tables, indexes)
  api.py            # FastAPI REST API serving the frontend
  config.py         # pydantic-settings config from .env
  discord_bot.py    # Discord alert delivery
  cli.py            # CLI entry point
  sources/          # External data clients (each wraps one API)
    betsapi.py      # Primary match data ($10/mo API)
    kambi.py        # Kambi odds
    bwin.py         # bwin odds (Volta)
    fanduel.py      # FanDuel odds
    totalcorner.py  # TotalCorner stats
    forebet.py      # Forebet predictions
    hudstats.py     # HUDstats — GG League schedules + live scores
    esportsbattle.py # ESportsBattle tournament API
  analysis/
    trends.py       # TrendAnalyzer — pattern detection from match history
    feedback.py     # FeedbackAnalyzer — learns from wins/losses, adjusts confidence
  alerts/
    console.py      # Rich console display for matchup reports
frontend/
  src/pages/        # React pages: Dashboard, Schedule, Picks, Players, etc.
  src/components/   # Shared UI: Layout, Table, Card, Kickoff timer, etc.
tests/
data/               # SQLite DB + runtime data (gitignored)
```

## Key Concepts

- **MatchupReport** (`models.py`) — the central analysis object. Wraps a Match with trends, odds, form data. `best_bet()` is a `@cached_property` that scores all markets and returns the top BetPick.
- **Feedback engine** (`analysis/feedback.py`) — after picks resolve, computes penalty/bonus multipliers across 5 dimensions (market, player, league, market type, edge bucket). Applied in `best_bet()`.
- **Pick resolution** — `resolve_pending_picks()` in `bot.py` checks final scores against pick lines. Results stored in `picks` table with profit tracking.
- **Scan cycle** — `scan_once()` runs every `POLL_INTERVAL` seconds: fetch results → resolve picks → recompute feedback → fetch external data → analyze matchups → alert.

## Conventions

- Python: ruff for linting (`ruff check`), line length 100, target Python 3.10
- Frontend: Tailwind CSS v4, React Query for data fetching, React Router v7
- All timestamps stored as UTC epoch integers in SQLite
- Database schema changes go in `database.py` as `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`
- External APIs wrapped in dedicated clients under `sources/` — never call APIs directly from bot.py
- Config via `.env` file, loaded through pydantic-settings in `config.py`

## Testing

```bash
pytest                    # run all tests
pytest tests/ -x          # stop on first failure
ruff check esporf/        # lint
```

## Tracked Leagues

- 42648 = Esoccer Battle 8min (GG League)
- 42649 = GT Leagues 12min
- 38439 = Volta 6min
