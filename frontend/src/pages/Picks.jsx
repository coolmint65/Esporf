import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import Card from '../components/Card'
import Badge from '../components/Badge'
import { Table, Th, Td } from '../components/Table'
import { Loading, Empty, ErrorMsg } from '../components/Empty'

export default function Picks() {
  const [result, setResult] = useState('')
  const [leagueId, setLeagueId] = useState('')
  const [days, setDays] = useState('')

  const params = { limit: 200 }
  if (result) params.result = result
  if (leagueId) params.league_id = leagueId
  if (days) params.days = days

  const { data: picks, isLoading, error } = useQuery({
    queryKey: ['pickHistory', params],
    queryFn: () => api.pickHistory(params),
  })

  const { data: live } = useQuery({
    queryKey: ['livePicks'],
    queryFn: api.livePicks,
    refetchInterval: 30_000,
  })

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Picks</h1>
        <p className="text-sm text-muted mt-1">Live and historical bet picks</p>
      </div>

      {/* Live picks */}
      {live?.length > 0 && (
        <Card title={`Live Picks (${live.length})`} icon iconColor="bg-win">
          <Table>
            <thead>
              <tr>
                <Th>Match</Th>
                <Th>League</Th>
                <Th>Market</Th>
                <Th>Units</Th>
                <Th>Odds</Th>
                <Th>Hit Rate</Th>
                <Th>Edge</Th>
              </tr>
            </thead>
            <tbody>
              {live.map((p, i) => (
                <tr key={i}>
                  <Td className="font-medium">{p.home} vs {p.away}</Td>
                  <Td className="text-muted">{p.league}</Td>
                  <Td>{p.market}</Td>
                  <Td className="font-semibold">{p.units}u</Td>
                  <Td>{p.odds_american || '--'}</Td>
                  <Td>{p.hit_rate_pct}</Td>
                  <Td>{p.edge_pct || '--'}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </Card>
      )}

      {/* Filters */}
      <Card title="Pick History" icon iconColor="bg-push">
        <div className="flex flex-wrap items-center gap-4 mb-4">
          <label className="flex items-center gap-2 text-sm">
            <span className="text-muted">Result</span>
            <select
              value={result}
              onChange={(e) => setResult(e.target.value)}
              className="bg-surface2 border border-border rounded-lg px-3 py-1.5 text-sm text-text outline-none focus:border-accent"
            >
              <option value="">All</option>
              <option value="win">Wins</option>
              <option value="loss">Losses</option>
              <option value="push">Pushes</option>
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

          <label className="flex items-center gap-2 text-sm">
            <span className="text-muted">Last</span>
            <input
              type="number"
              value={days}
              onChange={(e) => setDays(e.target.value)}
              placeholder="all"
              min="1"
              max="365"
              className="w-20 bg-surface2 border border-border rounded-lg px-3 py-1.5 text-sm text-text outline-none focus:border-accent"
            />
            <span className="text-muted">days</span>
          </label>
        </div>

        {isLoading ? <Loading /> : error ? <ErrorMsg error={error} /> : !picks?.length ? <Empty text="No picks match these filters" /> : (
          <Table>
            <thead>
              <tr>
                <Th>Match</Th>
                <Th>League</Th>
                <Th>Market</Th>
                <Th>Units</Th>
                <Th>Odds</Th>
                <Th>Score</Th>
                <Th>Result</Th>
                <Th>P/L</Th>
              </tr>
            </thead>
            <tbody>
              {picks.map((p, i) => (
                <tr key={i}>
                  <Td className="font-medium">{p.home} vs {p.away}</Td>
                  <Td className="text-muted">{p.league}</Td>
                  <Td>{p.market}</Td>
                  <Td>{p.units}u</Td>
                  <Td>{p.odds_american || '--'}</Td>
                  <Td>{p.score || '--'}</Td>
                  <Td><Badge variant={p.result}>{p.result}</Badge></Td>
                  <Td className={p.profit >= 0 ? 'text-win font-semibold' : 'text-loss font-semibold'}>
                    {p.profit_display}
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
