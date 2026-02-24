import { useQuery } from '@tanstack/react-query'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from 'recharts'
import { api } from '../lib/api'
import Card from '../components/Card'
import StatCard from '../components/StatCard'
import { Table, Th, Td } from '../components/Table'
import { Loading, ErrorMsg } from '../components/Empty'

export default function Leagues() {
  const { data: leagues, isLoading, error } = useQuery({
    queryKey: ['leagues'],
    queryFn: api.leagues,
  })

  const { data: scan } = useQuery({
    queryKey: ['scan'],
    queryFn: api.scanStatus,
  })

  if (isLoading) return <Loading />
  if (error) return <ErrorMsg error={error} />

  const totalMatches = leagues.reduce((a, l) => a + l.total_matches, 0)
  const totalProfit = leagues.reduce((a, l) => a + l.profit, 0)

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Leagues</h1>
        <p className="text-sm text-muted mt-1">Per-league stats and database info</p>
      </div>

      {/* Top-level stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard label="Total Matches" value={totalMatches.toLocaleString()} />
        <StatCard label="Total Profit" value={`${totalProfit >= 0 ? '+' : ''}${totalProfit.toFixed(1)}u`} color={totalProfit >= 0 ? 'green' : 'red'} />
        <StatCard label="Leagues Tracked" value={leagues.length} />
        <StatCard label="Total Scans" value={scan?.total_scans ?? '--'} />
      </div>

      {/* Profit chart */}
      <Card title="Profit by League" icon>
        <div className="h-64">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={leagues} margin={{ top: 8, right: 8, bottom: 0, left: -12 }}>
              <XAxis dataKey="league" tick={{ fill: '#8b90a5', fontSize: 12 }} axisLine={false} tickLine={false} />
              <YAxis tick={{ fill: '#8b90a5', fontSize: 12 }} axisLine={false} tickLine={false} tickFormatter={v => `${v}u`} />
              <Tooltip
                contentStyle={{ background: '#1a1d27', border: '1px solid #2e3245', borderRadius: 8, fontSize: 13 }}
                itemStyle={{ color: '#e1e4ed' }}
                labelStyle={{ color: '#8b90a5' }}
                formatter={(v) => [`${v >= 0 ? '+' : ''}${v}u`, 'Profit']}
              />
              <Bar dataKey="profit" radius={[6, 6, 0, 0]}>
                {leagues.map((entry, i) => (
                  <Cell key={i} fill={entry.profit >= 0 ? '#3b82f6' : '#ef4444'} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </Card>

      {/* League cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-5">
        {leagues.map((l) => (
          <Card key={l.league_id} title={l.league} icon>
            <div className="space-y-3">
              <div className="flex justify-between items-center">
                <span className="text-sm text-muted">Matches in DB</span>
                <span className="text-sm font-semibold">{l.total_matches.toLocaleString()}</span>
              </div>
              <div className="flex justify-between items-center">
                <span className="text-sm text-muted">Record</span>
                <span className="text-sm font-semibold">{l.record || '0-0'}</span>
              </div>
              <div className="flex justify-between items-center">
                <span className="text-sm text-muted">Win Rate</span>
                <span className="text-sm font-semibold">{l.win_rate_pct}</span>
              </div>
              <div className="flex justify-between items-center">
                <span className="text-sm text-muted">Profit</span>
                <span className={`text-sm font-bold ${l.profit >= 0 ? 'text-win' : 'text-loss'}`}>
                  {l.profit_display}
                </span>
              </div>
            </div>
          </Card>
        ))}
      </div>

      {/* Scan info */}
      {scan?.last_scan && (
        <Card title="Last Scan" icon iconColor="bg-win">
          <Table>
            <tbody>
              <tr><Td className="text-muted">Scan #</Td><Td className="font-medium">{scan.last_scan.scan_number}</Td></tr>
              <tr><Td className="text-muted">Started</Td><Td>{scan.last_scan.started_at_fmt}</Td></tr>
              <tr><Td className="text-muted">Duration</Td><Td>{scan.last_scan.duration_sec}s</Td></tr>
              <tr><Td className="text-muted">Matches Found</Td><Td>{scan.last_scan.matches_found}</Td></tr>
              <tr><Td className="text-muted">With Odds</Td><Td>{scan.last_scan.matches_with_odds}</Td></tr>
              <tr><Td className="text-muted">Picks Generated</Td><Td>{scan.last_scan.picks_generated}</Td></tr>
              <tr><Td className="text-muted">Picks Alerted</Td><Td>{scan.last_scan.picks_alerted}</Td></tr>
            </tbody>
          </Table>
        </Card>
      )}
    </div>
  )
}
