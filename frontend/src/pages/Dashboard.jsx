import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell, CartesianGrid, AreaChart, Area, ReferenceLine } from 'recharts'
import { api } from '../lib/api'
import StatCard from '../components/StatCard'
import Card from '../components/Card'
import Badge from '../components/Badge'
import { Table, Th, Td } from '../components/Table'
import { Loading, Empty } from '../components/Empty'
import Kickoff from '../components/Kickoff'

const COLORS = { win: '#22c55e', loss: '#ef4444', push: '#f59e0b' }

export default function Dashboard() {
  const [chartDays, setChartDays] = useState(7)

  const { data: daily, isLoading: dailyLoading } = useQuery({
    queryKey: ['dailyStats'],
    queryFn: api.dailyStats,
    refetchInterval: 60_000,
  })

  const { data: breakdown } = useQuery({
    queryKey: ['breakdown'],
    queryFn: api.breakdown,
  })

  const { data: chartData } = useQuery({
    queryKey: ['statsChart', chartDays],
    queryFn: () => api.statsChart({ days: chartDays }),
    refetchInterval: 120_000,
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

  if (dailyLoading) return <Loading />

  const today = daily?.today
  const allTime = daily?.all_time
  const hasPicks = allTime?.total_decided > 0
  const hasToday = today?.total_decided > 0 || today?.pending > 0
  const todayProfitColor = today?.profit >= 0 ? 'green' : 'red'
  const allProfitColor = allTime?.profit >= 0 ? 'green' : 'red'
  const todayRoiColor = today?.roi != null && today.roi >= 0 ? 'green' : today?.roi != null ? 'red' : undefined
  const allRoiColor = allTime?.roi != null && allTime.roi >= 0 ? 'green' : allTime?.roi != null ? 'red' : undefined

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Dashboard</h1>
        <p className="text-sm text-muted mt-1">Performance overview</p>
      </div>

      {/* ── Today's Performance ───────────────────────────── */}
      <div>
        <h2 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3">Today</h2>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-4">
          <StatCard
            label="Record"
            value={hasToday ? (today?.record ?? '0-0') : '0-0'}
            sub={today?.total_decided > 0 ? `${today.total_decided} decided` : 'no picks decided'}
          />
          <StatCard
            label="Profit / Loss"
            value={hasToday && today?.total_decided > 0 ? today?.profit_display : '--'}
            sub={today?.units_wagered > 0 ? `${today.units_wagered}u wagered` : ''}
            color={today?.total_decided > 0 ? todayProfitColor : undefined}
          />
          <StatCard
            label="Win Rate"
            value={today?.total_decided > 0 ? today?.win_rate_pct : '--'}
            sub={today?.total_decided > 0 ? `${today.wins}W / ${today.losses}L` : ''}
          />
          <StatCard
            label="ROI"
            value={today?.total_decided > 0 ? today?.roi_pct : '--'}
            color={today?.total_decided > 0 ? todayRoiColor : undefined}
          />
          <StatCard
            label="Pending"
            value={today?.pending ?? 0}
            sub="awaiting result"
            color="blue"
          />
        </div>
      </div>

      {/* ── All-Time Performance ──────────────────────────── */}
      {hasPicks && (
        <div>
          <h2 className="text-sm font-semibold text-muted uppercase tracking-wider mb-3">All Time</h2>
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
            <StatCard label="Record" value={allTime?.record ?? '--'} sub={`${allTime?.total_decided ?? 0} decided`} />
            <StatCard label="Profit" value={allTime?.profit_display ?? '--'} sub={`${allTime?.units_wagered ?? 0}u wagered`} color={allProfitColor} />
            <StatCard label="Win Rate" value={allTime?.win_rate_pct ?? '--'} sub={`${allTime?.wins ?? 0}W / ${allTime?.losses ?? 0}L`} />
            <StatCard label="ROI" value={allTime?.roi_pct ?? '--'} color={allRoiColor} />
            <StatCard label="Matches" value={allTime?.total_matches?.toLocaleString() ?? '--'} sub="in database" color="blue" />
            <StatCard label="Avg Goals" value={allTime?.avg_total_goals_display ?? '--'} sub="per match" />
          </div>
        </div>
      )}

      {/* ── Performance Chart ──────────────────────────────── */}
      {hasPicks && (
        <Card title="Daily Performance" icon iconColor="bg-win">
          <div className="flex items-center gap-2 mb-4">
            {[7, 14, 30].map((d) => (
              <button
                key={d}
                onClick={() => setChartDays(d)}
                className={`px-3 py-1 text-xs rounded-lg border transition-colors ${
                  chartDays === d
                    ? 'bg-accent text-white border-accent'
                    : 'bg-surface2 text-muted border-border hover:border-accent/50'
                }`}
              >
                {d}d
              </button>
            ))}
          </div>
          {chartData?.length > 0 ? (
            <div className="h-64">
              <ResponsiveContainer width="100%" height="100%">
                {(() => {
                  const vals = chartData.map((d) => d.cumulative)
                  const max = Math.max(...vals, 0)
                  const min = Math.min(...vals, 0)
                  const range = max - min || 1
                  const zeroOffset = max / range
                  return (
                    <AreaChart data={chartData} margin={{ top: 8, right: 8, bottom: 0, left: -12 }}>
                      <defs>
                        <linearGradient id="splitColor" x1="0" y1="0" x2="0" y2="1">
                          <stop offset={0} stopColor="#22c55e" stopOpacity={0.5} />
                          <stop offset={zeroOffset} stopColor="#22c55e" stopOpacity={0.05} />
                          <stop offset={zeroOffset} stopColor="#ef4444" stopOpacity={0.05} />
                          <stop offset={1} stopColor="#ef4444" stopOpacity={0.5} />
                        </linearGradient>
                        <linearGradient id="splitStroke" x1="0" y1="0" x2="0" y2="1">
                          <stop offset={zeroOffset} stopColor="#22c55e" stopOpacity={1} />
                          <stop offset={zeroOffset} stopColor="#ef4444" stopOpacity={1} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid strokeDasharray="3 3" stroke="#2e3245" />
                      <XAxis
                        dataKey="date"
                        tick={{ fill: '#8b90a5', fontSize: 11 }}
                        axisLine={false}
                        tickLine={false}
                        tickFormatter={(d) => {
                          const [, m, day] = d.split('-')
                          return `${parseInt(m)}/${parseInt(day)}`
                        }}
                      />
                      <YAxis
                        tick={{ fill: '#8b90a5', fontSize: 11 }}
                        axisLine={false}
                        tickLine={false}
                        tickFormatter={(v) => `${v >= 0 ? '+' : ''}${v}u`}
                        domain={[min, max]}
                      />
                      <ReferenceLine y={0} stroke="#8b90a5" strokeDasharray="3 3" strokeOpacity={0.6} />
                      <Tooltip
                        contentStyle={{ background: '#1a1d27', border: '1px solid #2e3245', borderRadius: 8, fontSize: 13 }}
                        itemStyle={{ color: '#e1e4ed' }}
                        labelStyle={{ color: '#8b90a5' }}
                        content={({ active, payload, label }) => {
                          if (!active || !payload?.length) return null
                          const entry = payload[0]?.payload
                          if (!entry) return null
                          return (
                            <div style={{ background: '#1a1d27', border: '1px solid #2e3245', borderRadius: 8, padding: '8px 12px', fontSize: 13 }}>
                              <div style={{ color: '#8b90a5', marginBottom: 4 }}>{label}</div>
                              <div style={{ color: entry.profit >= 0 ? '#22c55e' : '#ef4444' }}>
                                Daily P&L : {entry.profit >= 0 ? '+' : ''}{entry.profit}u
                              </div>
                              <div style={{ color: entry.cumulative >= 0 ? '#22c55e' : '#ef4444' }}>
                                Cumulative : {entry.cumulative >= 0 ? '+' : ''}{entry.cumulative}u
                              </div>
                            </div>
                          )
                        }}
                      />
                      <Area
                        type="monotone"
                        dataKey="cumulative"
                        stroke="url(#splitStroke)"
                        strokeWidth={2}
                        fill="url(#splitColor)"
                      />
                    </AreaChart>
                  )
                })()}
              </ResponsiveContainer>
            </div>
          ) : (
            <Empty text="No data for this period" />
          )}
        </Card>
      )}

      {/* ── Charts Row ─────────────────────────────────────── */}
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

      {/* ── Live Picks + Recent Results ────────────────────── */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        <Card title="Live Picks" icon iconColor="bg-win">
          {!live ? <Loading /> : live.length === 0 ? <Empty text="No pending picks" /> : (
            <Table>
              <thead>
                <tr>
                  <Th>Match</Th>
                  <Th>Market</Th>
                  <Th>Units</Th>
                  <Th>Odds</Th>
                  <Th>Edge</Th>
                  <Th>Kickoff</Th>
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
                    <Td>{p.odds_american || '--'}</Td>
                    <Td>{p.edge_pct || '--'}</Td>
                    <Td><Kickoff startTime={p.start_time} /></Td>
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

      {/* ── Confidence Breakdown ───────────────────────────── */}
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
