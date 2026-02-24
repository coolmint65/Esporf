import { useParams, Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from 'recharts'
import { api } from '../lib/api'
import StatCard from '../components/StatCard'
import Card from '../components/Card'
import Badge from '../components/Badge'
import { Loading, ErrorMsg } from '../components/Empty'

export default function PlayerDetail() {
  const { name } = useParams()

  const { data: player, isLoading, error } = useQuery({
    queryKey: ['player', name],
    queryFn: () => api.player(name),
  })

  if (isLoading) return <Loading />
  if (error) return (
    <div className="space-y-4">
      <Link to="/players" className="text-accent hover:underline text-sm">&larr; Back to players</Link>
      <ErrorMsg error={error} />
    </div>
  )

  const p = player
  const overData = Object.entries(p.over_rates).map(([line, rate]) => ({
    line: `O${line}`,
    rate: Math.round(rate * 100),
  }))

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4">
        <Link to="/players" className="text-accent hover:underline text-sm">&larr; Back</Link>
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold tracking-tight">{p.handle}</h1>
            <Badge variant={p.tier.toLowerCase()}>{p.tier}</Badge>
          </div>
          <p className="text-sm text-muted mt-0.5">{p.league} &middot; {p.form_trend} form</p>
        </div>
      </div>

      {/* Summary stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-4">
        <StatCard label="Matches" value={p.matches_played} />
        <StatCard label="Win Rate" value={p.win_rate_pct} />
        <StatCard label="Avg GF" value={p.avg_goals_scored} />
        <StatCard label="Avg GA" value={p.avg_goals_conceded} />
        <StatCard label="Avg Total" value={p.avg_total_goals} />
        <StatCard label="Form Modifier" value={`${p.form_modifier}x`} color={p.form_modifier >= 1 ? 'green' : p.form_modifier > 0 ? 'amber' : 'red'} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        {/* Over rates chart */}
        <Card title="Over Hit Rates" icon>
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={overData} margin={{ top: 8, right: 8, bottom: 0, left: -12 }}>
                <XAxis dataKey="line" tick={{ fill: '#8b90a5', fontSize: 12 }} axisLine={false} tickLine={false} />
                <YAxis tick={{ fill: '#8b90a5', fontSize: 12 }} axisLine={false} tickLine={false} tickFormatter={v => `${v}%`} domain={[0, 100]} />
                <Tooltip
                  contentStyle={{ background: '#1a1d27', border: '1px solid #2e3245', borderRadius: 8, fontSize: 13 }}
                  formatter={(v) => [`${v}%`, 'Hit Rate']}
                />
                <Bar dataKey="rate" radius={[6, 6, 0, 0]}>
                  {overData.map((entry, i) => (
                    <Cell key={i} fill={entry.rate >= 70 ? '#22c55e' : entry.rate >= 50 ? '#3b82f6' : '#8b90a5'} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>

        {/* Recent form */}
        <Card title="Recent Form (Last 10)" icon iconColor="bg-cyan">
          <div className="grid grid-cols-2 gap-3">
            <div className="bg-surface2 rounded-lg p-4 text-center">
              <div className="text-xl font-bold">{p.recent.win_rate_pct}</div>
              <div className="text-xs text-muted uppercase">Win Rate</div>
            </div>
            <div className="bg-surface2 rounded-lg p-4 text-center">
              <div className="text-xl font-bold">{p.recent.wins}W-{p.recent.losses}L-{p.recent.draws}D</div>
              <div className="text-xs text-muted uppercase">Record</div>
            </div>
            <div className="bg-surface2 rounded-lg p-4 text-center">
              <div className="text-xl font-bold">{p.recent.avg_goals_scored}</div>
              <div className="text-xs text-muted uppercase">Avg GF</div>
            </div>
            <div className="bg-surface2 rounded-lg p-4 text-center">
              <div className="text-xl font-bold">{p.recent.avg_goals_conceded}</div>
              <div className="text-xs text-muted uppercase">Avg GA</div>
            </div>
          </div>

          <div className="mt-4 p-3 bg-surface2 rounded-lg">
            <div className="flex items-center justify-between">
              <span className="text-sm text-muted">Overall Record</span>
              <span className="text-sm font-medium">{p.wins}W - {p.losses}L - {p.draws}D</span>
            </div>
            <div className="flex items-center justify-between mt-2">
              <span className="text-sm text-muted">Total Goals</span>
              <span className="text-sm font-medium">{p.goals_scored} scored / {p.goals_conceded} conceded</span>
            </div>
          </div>
        </Card>
      </div>
    </div>
  )
}
