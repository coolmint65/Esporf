import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import Card from '../components/Card'
import { Table, Th, Td } from '../components/Table'
import { Loading, Empty, ErrorMsg } from '../components/Empty'

export default function Matches() {
  const [leagueId, setLeagueId] = useState('')
  const [player, setPlayer] = useState('')
  const [h2hA, setH2hA] = useState('')
  const [h2hB, setH2hB] = useState('')
  const [h2hSubmit, setH2hSubmit] = useState(null)

  const matchParams = { limit: 100 }
  if (leagueId) matchParams.league_id = leagueId
  if (player) matchParams.player = player

  const { data: matches, isLoading, error } = useQuery({
    queryKey: ['matches', matchParams],
    queryFn: () => api.recentMatches(matchParams),
  })

  const { data: h2hData, isLoading: h2hLoading, error: h2hError } = useQuery({
    queryKey: ['h2h', h2hSubmit],
    queryFn: () => api.h2h(h2hSubmit.a, h2hSubmit.b),
    enabled: !!h2hSubmit,
  })

  const doH2H = () => {
    if (h2hA.trim() && h2hB.trim()) {
      setH2hSubmit({ a: h2hA.trim(), b: h2hB.trim() })
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Matches</h1>
        <p className="text-sm text-muted mt-1">Recent results and head-to-head lookup</p>
      </div>

      {/* H2H lookup */}
      <Card title="Head-to-Head Lookup" icon iconColor="bg-purple">
        <div className="flex flex-wrap items-end gap-3 mb-4">
          <div>
            <label className="block text-xs text-muted mb-1">Player A</label>
            <input
              type="text"
              value={h2hA}
              onChange={(e) => setH2hA(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && doH2H()}
              placeholder="e.g. Sheva"
              className="bg-surface2 border border-border rounded-lg px-3 py-2 text-sm text-text outline-none focus:border-accent w-48"
            />
          </div>
          <span className="text-muted text-sm pb-2">vs</span>
          <div>
            <label className="block text-xs text-muted mb-1">Player B</label>
            <input
              type="text"
              value={h2hB}
              onChange={(e) => setH2hB(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && doH2H()}
              placeholder="e.g. Ronaldo"
              className="bg-surface2 border border-border rounded-lg px-3 py-2 text-sm text-text outline-none focus:border-accent w-48"
            />
          </div>
          <button
            onClick={doH2H}
            className="bg-accent text-white px-5 py-2 rounded-lg text-sm font-semibold hover:opacity-90 transition-opacity"
          >
            Search
          </button>
        </div>

        {h2hLoading && <Loading />}
        {h2hError && <ErrorMsg error={h2hError} />}
        {h2hData && (
          <div>
            {/* H2H summary */}
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-4">
              <div className="bg-surface2 rounded-lg p-3 text-center">
                <div className="text-lg font-bold">{h2hData.total_matches}</div>
                <div className="text-xs text-muted">Matches</div>
              </div>
              <div className="bg-surface2 rounded-lg p-3 text-center">
                <div className="text-lg font-bold text-win">{h2hData.summary[`${h2hData.player_a}_wins`]}</div>
                <div className="text-xs text-muted">{h2hData.player_a} wins</div>
              </div>
              <div className="bg-surface2 rounded-lg p-3 text-center">
                <div className="text-lg font-bold text-loss">{h2hData.summary[`${h2hData.player_b}_wins`]}</div>
                <div className="text-xs text-muted">{h2hData.player_b} wins</div>
              </div>
              <div className="bg-surface2 rounded-lg p-3 text-center">
                <div className="text-lg font-bold text-muted">{h2hData.summary.draws}</div>
                <div className="text-xs text-muted">Draws</div>
              </div>
              <div className="bg-surface2 rounded-lg p-3 text-center">
                <div className="text-lg font-bold">{h2hData.summary.avg_total_goals}</div>
                <div className="text-xs text-muted">Avg Goals</div>
              </div>
            </div>

            {/* Over rates */}
            {h2hData.summary.over_rates && (
              <div className="flex gap-3 mb-4">
                {Object.entries(h2hData.summary.over_rates).map(([line, rate]) => (
                  <div key={line} className="bg-surface2 rounded-lg px-3 py-2 text-center text-sm">
                    <span className="text-muted">O{line}:</span>{' '}
                    <span className="font-semibold">{(rate * 100).toFixed(0)}%</span>
                  </div>
                ))}
              </div>
            )}

            {/* Match list */}
            <Table>
              <thead>
                <tr>
                  <Th>Home</Th>
                  <Th>Away</Th>
                  <Th>Score</Th>
                  <Th>Total</Th>
                  <Th>Date</Th>
                </tr>
              </thead>
              <tbody>
                {h2hData.matches.map((m, i) => (
                  <tr key={i}>
                    <Td className="font-medium">{m.home}</Td>
                    <Td className="font-medium">{m.away}</Td>
                    <Td className="font-bold">{m.score}</Td>
                    <Td>{m.total_goals}g</Td>
                    <Td className="text-xs text-muted">{m.start_time_fmt}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </div>
        )}
      </Card>

      {/* Recent matches table */}
      <Card title="Recent Matches" icon iconColor="bg-push">
        <div className="flex flex-wrap items-center gap-4 mb-4">
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
          <label className="flex items-center gap-2 text-sm">
            <span className="text-muted">Player</span>
            <input
              type="text"
              value={player}
              onChange={(e) => setPlayer(e.target.value)}
              placeholder="filter by name..."
              className="bg-surface2 border border-border rounded-lg px-3 py-1.5 text-sm text-text outline-none focus:border-accent w-44"
            />
          </label>
        </div>

        {isLoading ? <Loading /> : error ? <ErrorMsg error={error} /> : !matches?.length ? <Empty text="No matches found" /> : (
          <Table>
            <thead>
              <tr>
                <Th>Home</Th>
                <Th>Away</Th>
                <Th>Score</Th>
                <Th>Total</Th>
                <Th>Winner</Th>
                <Th>League</Th>
                <Th>Date</Th>
              </tr>
            </thead>
            <tbody>
              {matches.map((m, i) => (
                <tr key={i}>
                  <Td className="font-medium">{m.home}</Td>
                  <Td className="font-medium">{m.away}</Td>
                  <Td className="font-bold">{m.score}</Td>
                  <Td>{m.total_goals}g</Td>
                  <Td className={m.winner ? 'text-win' : 'text-muted'}>{m.winner || 'Draw'}</Td>
                  <Td className="text-muted">{m.league}</Td>
                  <Td className="text-xs text-muted">{m.start_time_fmt}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </div>
  )
}
