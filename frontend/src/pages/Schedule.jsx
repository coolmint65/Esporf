import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import Card from '../components/Card'
import Badge from '../components/Badge'
import { Loading, Empty, ErrorMsg } from '../components/Empty'

const TIER_COLORS = {
  Elite: 'text-purple',
  Solid: 'text-win',
  Watchlist: 'text-push',
  Blocked: 'text-loss',
  New: 'text-accent',
}

const TREND_ICONS = {
  rising: '\u25B2',
  falling: '\u25BC',
  stable: '\u25CF',
  insufficient: '\u25CB',
}

const TREND_COLORS = {
  rising: 'text-win',
  falling: 'text-loss',
  stable: 'text-muted',
  insufficient: 'text-muted',
}

function classifyPick(market) {
  const m = market.toLowerCase()
  if (m.includes('over')) return 'over'
  if (m.includes('under')) return 'under'
  return 'split' // ML, spread, draw
}

function BestBetCard({ label, pick, accentColor }) {
  if (!pick) {
    return (
      <div className="bg-surface border border-border rounded-xl p-5 flex flex-col items-center justify-center min-h-[120px]">
        <div className="text-xs uppercase tracking-wider text-muted mb-1">{label}</div>
        <div className="text-sm text-muted">No picks</div>
      </div>
    )
  }

  const profitColor = pick.profit >= 0 ? 'text-win' : 'text-loss'
  const startTime = pick.start_time
    ? new Date(pick.start_time * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : null

  return (
    <div className={`bg-surface border border-border rounded-xl p-5 relative overflow-hidden`}>
      <div className={`absolute top-0 left-0 w-1 h-full ${accentColor}`} />
      <div className="text-xs uppercase tracking-wider text-muted mb-2">{label}</div>
      <div className="text-sm font-bold mb-1">{pick.market}</div>
      <div className="text-xs text-muted mb-2">
        {pick.home} vs {pick.away}
        {startTime && <span className="ml-2 text-accent">{startTime}</span>}
      </div>
      <div className="flex items-center gap-3 text-xs">
        <span className="font-semibold">{pick.units}u</span>
        <span>{pick.odds_american || '--'}</span>
        {pick.edge_pct && <span className="text-win">{pick.edge_pct} edge</span>}
        <span className="text-muted">{pick.hit_rate_pct} HR</span>
      </div>
    </div>
  )
}

function PlayerStat({ stats, side }) {
  if (!stats) return <div className="text-xs text-muted">No data</div>

  return (
    <div className={`flex flex-col gap-0.5 ${side === 'away' ? 'items-end' : 'items-start'}`}>
      <div className="flex items-center gap-1.5">
        <span className={`text-xs font-semibold ${TIER_COLORS[stats.tier] || 'text-muted'}`}>
          {stats.tier}
        </span>
        <span className={`text-[10px] ${TREND_COLORS[stats.form_trend]}`}>
          {TREND_ICONS[stats.form_trend]}
        </span>
      </div>
      <div className="flex gap-2 text-[11px] text-muted">
        <span>WR {stats.win_rate_pct}</span>
        <span>{stats.avg_goals}g</span>
        <span>O4.5 {stats.over_4_5_pct}</span>
      </div>
    </div>
  )
}

function MatchRow({ match, onClick }) {
  const isUpcoming = match.status === 'upcoming'
  const isLive = match.status === 'live'
  const homeWon = !isUpcoming && !isLive && match.home_score > match.away_score
  const awayWon = !isUpcoming && !isLive && match.away_score > match.home_score
  const time = new Date(match.start_time * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

  const borderClass = isLive
    ? 'border-accent/50'
    : isUpcoming
    ? 'border-border/50'
    : 'border-border'

  return (
    <div
      onClick={() => onClick(match.match_id)}
      className={`group bg-surface2/50 hover:bg-surface2 border ${borderClass} rounded-lg px-4 py-3 cursor-pointer transition-colors`}
    >
      <div className="flex items-center gap-3">
        {/* Time */}
        <div className="w-14 shrink-0 text-center">
          <div className="text-xs text-muted">{time}</div>
          {isLive && <div className="text-[10px] text-accent font-semibold">LIVE</div>}
          {isUpcoming && <div className="text-[10px] text-muted">Upcoming</div>}
        </div>

        {/* Home side */}
        <div className="flex-1 min-w-0">
          <div className={`text-sm font-medium truncate ${homeWon ? 'text-win' : ''} ${isUpcoming ? 'text-muted' : ''}`}>
            {match.home}
          </div>
          <PlayerStat stats={match.home_stats} side="home" />
        </div>

        {/* Score or VS */}
        <div className="w-20 shrink-0 text-center">
          {isUpcoming || isLive ? (
            <div className={`text-lg font-bold ${isLive ? 'text-accent' : 'text-muted'}`}>vs</div>
          ) : (
            <>
              <div className="text-lg font-bold tracking-wider">
                <span className={homeWon ? 'text-win' : ''}>{match.home_score}</span>
                <span className="text-muted mx-1">-</span>
                <span className={awayWon ? 'text-win' : ''}>{match.away_score}</span>
              </div>
              <div className="text-[10px] text-muted">{match.total_goals} goals</div>
            </>
          )}
        </div>

        {/* Away side */}
        <div className="flex-1 min-w-0">
          <div className={`text-sm font-medium truncate text-right ${awayWon ? 'text-win' : ''} ${isUpcoming ? 'text-muted' : ''}`}>
            {match.away}
          </div>
          <PlayerStat stats={match.away_stats} side="away" />
        </div>

        {/* League + Pick badge */}
        <div className="w-24 shrink-0 flex flex-col items-end gap-1">
          <span className="text-[10px] text-muted uppercase tracking-wider">{match.league}</span>
          {match.has_pick && <Badge variant="pending">Pick</Badge>}
        </div>

        {/* Arrow */}
        <div className="w-4 text-muted opacity-0 group-hover:opacity-100 transition-opacity">
          ›
        </div>
      </div>
    </div>
  )
}

export default function Schedule() {
  const navigate = useNavigate()
  const [days, setDays] = useState(1)
  const [leagueId, setLeagueId] = useState('')

  const params = { days }
  if (leagueId) params.league_id = leagueId

  const { data: schedule, isLoading, error } = useQuery({
    queryKey: ['schedule', params],
    queryFn: () => api.schedule(params),
    refetchInterval: 10_000,
  })

  const { data: livePicks } = useQuery({
    queryKey: ['livePicks'],
    queryFn: api.livePicks,
    refetchInterval: 30_000,
  })

  const handleMatchClick = (matchId) => {
    navigate(`/schedule/${encodeURIComponent(matchId)}`)
  }

  const totalMatches = schedule?.reduce((sum, g) => sum + g.total, 0) ?? 0

  // Derive best bets by category from live picks
  const bestOver = livePicks?.filter(p => classifyPick(p.market) === 'over')
    .sort((a, b) => (b.edge ?? 0) - (a.edge ?? 0))[0] ?? null
  const bestUnder = livePicks?.filter(p => classifyPick(p.market) === 'under')
    .sort((a, b) => (b.edge ?? 0) - (a.edge ?? 0))[0] ?? null
  const bestSplit = livePicks?.filter(p => classifyPick(p.market) === 'split')
    .sort((a, b) => (b.edge ?? 0) - (a.edge ?? 0))[0] ?? null
  const hasBestBets = bestOver || bestUnder || bestSplit

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Schedule</h1>
        <p className="text-sm text-muted mt-1">
          Match schedule with player stats {totalMatches > 0 && `\u2014 ${totalMatches} matches`}
        </p>
      </div>

      {/* Best Bets Cards */}
      {hasBestBets && (
        <div>
          <h2 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3">Best Bets</h2>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <BestBetCard label="Best Over Bet" pick={bestOver} accentColor="bg-win" />
            <BestBetCard label="Best Under Bet" pick={bestUnder} accentColor="bg-accent" />
            <BestBetCard label="Best ML / Spread" pick={bestSplit} accentColor="bg-purple" />
          </div>
        </div>
      )}

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-4">
        <label className="flex items-center gap-2 text-sm">
          <span className="text-muted">Period</span>
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="bg-surface2 border border-border rounded-lg px-3 py-1.5 text-sm text-text outline-none focus:border-accent"
          >
            <option value={1}>Today</option>
            <option value={2}>Last 2 days</option>
            <option value={3}>Last 3 days</option>
            <option value={7}>Last week</option>
          </select>
        </label>
        <label className="flex items-center gap-2 text-sm">
          <span className="text-muted">League</span>
          <select
            value={leagueId}
            onChange={(e) => setLeagueId(e.target.value)}
            className="bg-surface2 border border-border rounded-lg px-3 py-1.5 text-sm text-text outline-none focus:border-accent"
          >
            <option value="">All</option>
            <option value="42648">GG League</option>
            <option value="42649">GT Leagues</option>
            <option value="38439">Volta</option>
          </select>
        </label>
      </div>

      {isLoading ? <Loading /> : error ? <ErrorMsg error={error} /> :
        !schedule?.length ? <Empty text="No matches found for this period" /> :
          schedule.map((group) => (
            <Card key={group.date} title={formatDate(group.date)} icon iconColor="bg-accent">
              <div className="text-xs text-muted mb-3">{group.total} matches</div>
              <div className="space-y-2">
                {group.matches.map((match) => (
                  <MatchRow
                    key={match.match_id}
                    match={match}
                    onClick={handleMatchClick}
                  />
                ))}
              </div>
            </Card>
          ))
      }
    </div>
  )
}

function formatDate(dateStr) {
  // dateStr is YYYY-MM-DD in the server's display timezone (US/Eastern)
  // Parse as local date parts to avoid UTC offset issues
  const [year, month, day] = dateStr.split('-').map(Number)
  const matchDate = new Date(year, month - 1, day)

  const today = new Date()
  today.setHours(0, 0, 0, 0)

  const diffDays = Math.round((today - matchDate) / 86400000)

  if (diffDays === 0) return 'Today'
  if (diffDays === 1) return 'Yesterday'

  return matchDate.toLocaleDateString('en-US', { weekday: 'long', month: 'short', day: 'numeric' })
}
