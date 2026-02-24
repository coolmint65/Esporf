import { useQuery } from '@tanstack/react-query'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from 'recharts'
import { api } from '../lib/api'
import StatCard from '../components/StatCard'
import Card from '../components/Card'
import Badge from '../components/Badge'
import { Table, Th, Td } from '../components/Table'
import { Loading, Empty } from '../components/Empty'

const COLORS = { win: '#22c55e', loss: '#ef4444', push: '#f59e0b' }

export default function Dashboard() {
  const { data: stats, isLoading: statsLoading } = useQuery({
    queryKey: ['stats'],
    queryFn: () => api.stats(),
    refetchInterval: 60_000,
  })

  const { data: breakdown } = useQuery({
    queryKey: ['breakdown'],
    queryFn: api.breakdown,
  })

  const { data: live } = useQuery({
    queryKey: ['livePicks'],
    queryFn: api.livePicks,
    refetchInterval: 30_000,
  })

  const { data: recent } = useQuery({
    queryKey: ['recentMatches', { limit: 8 }],
    queryFn: () => api.recentMatches({ limit: 8 }),
  })

  if (statsLoading) return <Loading />

  const hasPicks = stats?.total_decided > 0
  const profitColor = stats?.profit >= 0 ? 'green' : 'red'
  const roiColor = stats?.roi != null && stats.roi >= 0 ? 'green' : stats?.roi != null ? 'red' : undefined

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Dashboard</h1>
        <p className="text-sm text-muted mt-1">Performance overview</p>
      </div>

      {/* Database stat cards — always visible */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-4">
        <StatCard label="Matches" value={stats?.total_matches?.toLocaleString() ?? '--'} sub="in database" color="blue" />
        <StatCard label="Players" value={stats?.total_players ?? '--'} sub="tracked" />
        <StatCard label="Avg Goals" value={stats?.avg_total_goals_display ?? '--'} sub="per match" />
        {hasPicks ? (
          <>
            <StatCard label="Record" value={stats?.record ?? '--'} sub={`${stats?.total_decided ?? 0} decided`} />
            <StatCard label="Profit" value={stats?.profit_display ?? '--'} sub={`${stats?.units_wagered ?? 0}u wagered`} color={profitColor} />
          </>
        ) : (
          <>
            <StatCard label="Record" value={stats?.record ?? '0-0'} sub="no picks yet" />
            <StatCard label="Pending" value={stats?.pending ?? 0} sub="awaiting result" color="blue" />
          </>
        )}
      </div>

      {/* Pick stats row — only when there are picks */}
      {hasPicks && (
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
          <StatCard label="Win Rate" value={stats?.win_rate_pct ?? '--'} sub={`${stats?.wins ?? 0}W / ${stats?.losses ?? 0}L`} />
          <StatCard label="ROI" value={stats?.roi_pct ?? '--'} color={roiColor} />
          <StatCard label="Units Wagered" value={`${stats?.units_wagered ?? 0}u`} />
          <StatCard label="Pending" value={stats?.pending ?? 0} sub="awaiting result" color="blue" />
        </div>
      )}

      {/* Charts */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        <Card title="Matches by League" icon iconColor="bg-accent">
          {breakdown?.by_league ? (
            <div className="h-56">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={breakdown.by_league} margin={{ top: 8, right: 8, bottom: 0, left: -12 }}>
                  <XAxis dataKey="league" tick={{ fill: '#8b90a5', fontSize: 12 }} axisLine={false} tickLine={false} />
                  <YAxis tick={{ fill: '#8b90a5', fontSize: 12 }} axisLine={false} tickLine={false} />
                  <Tooltip
                    contentStyle={{ background: '#1a1d27', border: '1px solid #2e3245', borderRadius: 8, fontSize: 13 }}
                    itemStyle={{ color: '#e1e4ed' }}
                    labelStyle={{ color: '#8b90a5' }}
                    formatter={(v, name) => [v.toLocaleString(), name === 'total_matches' ? 'Matches' : name]}
                  />
                  <Bar dataKey="total_matches" radius={[6, 6, 0, 0]}>
                    {breakdown.by_league.map((entry, i) => (
                      <Cell key={i} fill={['#3b82f6', '#8b5cf6', '#06b6d4', '#22c55e', '#f59e0b'][i % 5]} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : <Loading />}
        </Card>

        {hasPicks && breakdown?.by_market?.length > 0 ? (
          <Card title="Profit by Market" icon iconColor="bg-purple">
            <div className="h-56">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={breakdown.by_market} margin={{ top: 8, right: 8, bottom: 0, left: -12 }}>
                  <XAxis dataKey="market" tick={{ fill: '#8b90a5', fontSize: 12 }} axisLine={false} tickLine={false} />
                  <YAxis tick={{ fill: '#8b90a5', fontSize: 12 }} axisLine={false} tickLine={false} tickFormatter={v => `${v}u`} />
                  <Tooltip
                    contentStyle={{ background: '#1a1d27', border: '1px solid #2e3245', borderRadius: 8, fontSize: 13 }}
                    itemStyle={{ color: '#e1e4ed' }}
                    labelStyle={{ color: '#8b90a5' }}
                    formatter={(v) => [`${v >= 0 ? '+' : ''}${v}u`, 'Profit']}
                  />
                  <Bar dataKey="profit" radius={[6, 6, 0, 0]}>
                    {breakdown.by_market.map((entry, i) => (
                      <Cell key={i} fill={entry.profit >= 0 ? '#22c55e' : '#ef4444'} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          </Card>
        ) : (
          <Card title="League Details" icon iconColor="bg-purple">
            {breakdown?.by_league ? (
              <Table>
                <thead>
                  <tr>
                    <Th>League</Th>
                    <Th>Matches</Th>
                    <Th>Record</Th>
                  </tr>
                </thead>
                <tbody>
                  {breakdown.by_league.map((l, i) => (
                    <tr key={i}>
                      <Td className="font-medium">{l.league}</Td>
                      <Td>{l.total_matches.toLocaleString()}</Td>
                      <Td className="text-muted">{l.record || '--'}</Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            ) : <Loading />}
          </Card>
        )}
      </div>

      {/* Live picks + Recent matches */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        <Card title="Live Picks" icon iconColor="bg-win">
          {!live ? <Loading /> : live.length === 0 ? <Empty text="No pending picks" /> : (
            <Table>
              <thead>
                <tr>
                  <Th>Match</Th>
                  <Th>Market</Th>
                  <Th>Units</Th>
                  <Th>Edge</Th>
                </tr>
              </thead>
              <tbody>
                {live.map((p, i) => (
                  <tr key={i}>
                    <Td>
                      <div className="font-medium">{p.home} vs {p.away}</div>
                      <div className="text-xs text-muted">{p.league}</div>
                    </Td>
                    <Td>{p.market}</Td>
                    <Td className="font-semibold">{p.units}u</Td>
                    <Td>{p.edge_pct || '--'}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
        </Card>

        <Card title="Recent Results" icon iconColor="bg-push">
          {!recent ? <Loading /> : recent.length === 0 ? <Empty text="No matches yet" /> : (
            <Table>
              <thead>
                <tr>
                  <Th>Match</Th>
                  <Th>Score</Th>
                  <Th>League</Th>
                </tr>
              </thead>
              <tbody>
                {recent.map((m, i) => (
                  <tr key={i}>
                    <Td>
                      <span className="font-medium">{m.home}</span>
                      <span className="text-muted mx-1">vs</span>
                      <span className="font-medium">{m.away}</span>
                    </Td>
                    <Td>
                      <span className="font-bold">{m.score}</span>
                      <span className="text-muted ml-1 text-xs">({m.total_goals}g)</span>
                    </Td>
                    <Td className="text-muted">{m.league}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
        </Card>
      </div>

      {/* Confidence breakdown — only when picks exist */}
      {breakdown?.by_confidence?.length > 0 && (
        <Card title="By Confidence Tier" icon iconColor="bg-cyan">
          <Table>
            <thead>
              <tr>
                <Th>Tier</Th>
                <Th>Record</Th>
                <Th>Win %</Th>
                <Th>Profit</Th>
              </tr>
            </thead>
            <tbody>
              {breakdown.by_confidence.map((c, i) => (
                <tr key={i}>
                  <Td className="font-medium">{c.tier}</Td>
                  <Td>{c.record}</Td>
                  <Td>{c.win_rate_pct}</Td>
                  <Td className={c.profit >= 0 ? 'text-win' : 'text-loss'}>{c.profit_display}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </Card>
      )}
    </div>
  )
}
