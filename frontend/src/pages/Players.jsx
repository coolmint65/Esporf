import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import Card from '../components/Card'
import Badge from '../components/Badge'
import { Table, Th, Td } from '../components/Table'
import { Loading, Empty, ErrorMsg } from '../components/Empty'

const TIERS = [
  { value: '', label: 'All' },
  { value: 'elite', label: 'Elite' },
  { value: 'solid', label: 'Solid' },
  { value: 'watchlist', label: 'Watchlist' },
  { value: 'new', label: 'New' },
]

export default function Players() {
  const [tier, setTier] = useState('')
  const [minMatches, setMinMatches] = useState(10)

  const params = { min_matches: minMatches }
  if (tier) params.tier = tier

  const { data: players, isLoading, error } = useQuery({
    queryKey: ['players', params],
    queryFn: () => api.players(params),
    refetchInterval: 60_000,
  })

  const sorted = players ? [...players].sort((a, b) => b.win_rate - a.win_rate) : []

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Players</h1>
        <p className="text-sm text-muted mt-1">Player form, tiers, and performance stats</p>
      </div>

      <Card>
        {/* Tier tabs */}
        <div className="flex flex-wrap items-center gap-2 mb-4">
          {TIERS.map(({ value, label }) => (
            <button
              key={value}
              onClick={() => setTier(value)}
              className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                tier === value
                  ? 'bg-accent text-white'
                  : 'text-muted hover:text-text hover:bg-surface2'
              }`}
            >
              {label}
            </button>
          ))}

          <div className="ml-auto flex items-center gap-2 text-sm">
            <span className="text-muted">Min matches:</span>
            <input
              type="number"
              value={minMatches}
              onChange={(e) => setMinMatches(Number(e.target.value) || 1)}
              min="1"
              className="w-16 bg-surface2 border border-border rounded-lg px-2 py-1 text-sm text-text outline-none focus:border-accent"
            />
          </div>
        </div>

        {isLoading ? <Loading /> : error ? <ErrorMsg error={error} /> : !sorted.length ? <Empty text="No players found" /> : (
          <Table>
            <thead>
              <tr>
                <Th>Player</Th>
                <Th>League</Th>
                <Th>Tier</Th>
                <Th>Matches</Th>
                <Th>Win %</Th>
                <Th>Avg GF</Th>
                <Th>Avg GA</Th>
                <Th>Avg Total</Th>
                <Th>O4.5%</Th>
                <Th>Trend</Th>
              </tr>
            </thead>
            <tbody>
              {sorted.slice(0, 100).map((p, i) => (
                <tr key={i}>
                  <Td>
                    <Link
                      to={`/players/${encodeURIComponent(p.handle)}`}
                      className="font-semibold text-accent hover:underline"
                    >
                      {p.handle}
                    </Link>
                  </Td>
                  <Td className="text-muted">{p.league}</Td>
                  <Td><Badge variant={p.tier.toLowerCase()}>{p.tier}</Badge></Td>
                  <Td>{p.matches_played}</Td>
                  <Td className="font-medium">{p.win_rate_pct}</Td>
                  <Td>{p.avg_goals_scored}</Td>
                  <Td>{p.avg_goals_conceded}</Td>
                  <Td>{p.avg_total_goals}</Td>
                  <Td>{(p.over_rates['4.5'] * 100).toFixed(0)}%</Td>
                  <Td>
                    <span className={
                      p.form_trend === 'rising' ? 'text-win' :
                      p.form_trend === 'falling' ? 'text-loss' :
                      'text-muted'
                    }>
                      {p.form_trend}
                    </span>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </div>
  )
}
