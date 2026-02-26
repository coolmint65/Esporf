import { useParams, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from 'recharts'
import { api } from '../lib/api'
import Card from '../components/Card'
import Badge from '../components/Badge'
import { Loading, Empty, ErrorMsg } from '../components/Empty'
import { Table, Th, Td } from '../components/Table'

const TIER_COLORS = {
  Elite: 'text-purple',
  Solid: 'text-win',
  Watchlist: 'text-push',
  Blocked: 'text-loss',
  New: 'text-accent',
}

function StatCompare({ label, homeVal, awayVal, homeHighlight, awayHighlight, format }) {
  const fmtHome = format ? format(homeVal) : homeVal
  const fmtAway = format ? format(awayVal) : awayVal
  return (
    <div className="flex items-center py-2 border-b border-border last:border-0">
      <div className={`flex-1 text-right text-sm font-medium ${homeHighlight ? 'text-win' : ''}`}>
        {fmtHome}
      </div>
      <div className="w-32 text-center text-xs text-muted uppercase tracking-wider px-2">
        {label}
      </div>
      <div className={`flex-1 text-left text-sm font-medium ${awayHighlight ? 'text-win' : ''}`}>
        {fmtAway}
      </div>
    </div>
  )
}

function OverRateBar({ line, homeRate, awayRate, h2hRate }) {
  return (
    <div className="flex items-center gap-2 py-1.5">
      <div className="w-12 text-xs text-muted text-right">O{line}</div>
      <div className="flex-1 flex items-center gap-1">
        {/* Home bar (right-aligned) */}
        <div className="flex-1 flex justify-end">
          <div
            className="h-5 bg-accent/30 rounded-l"
            style={{ width: `${homeRate * 100}%`, minWidth: homeRate > 0 ? '2px' : 0 }}
          />
        </div>
        <div className="w-20 text-center text-xs text-muted">
          {(homeRate * 100).toFixed(0)}% / {(awayRate * 100).toFixed(0)}%
        </div>
        {/* Away bar (left-aligned) */}
        <div className="flex-1">
          <div
            className="h-5 bg-purple/30 rounded-r"
            style={{ width: `${awayRate * 100}%`, minWidth: awayRate > 0 ? '2px' : 0 }}
          />
        </div>
      </div>
      {h2hRate !== undefined && (
        <div className="w-16 text-xs text-muted text-left">
          H2H {(h2hRate * 100).toFixed(0)}%
        </div>
      )}
    </div>
  )
}

export default function MatchDetail() {
  const { matchId } = useParams()
  const navigate = useNavigate()

  const { data, isLoading, error } = useQuery({
    queryKey: ['matchDetail', matchId],
    queryFn: () => api.matchDetail(matchId),
  })

  if (isLoading) return <Loading />
  if (error) return <ErrorMsg error={error} />
  if (!data) return <Empty text="Match not found" />

  const { match, home_form, away_form, h2h, picks, recommendation } = data
  const homeHandle = home_form?.handle || match.home
  const awayHandle = away_form?.handle || match.away
  const homeWon = match.home_score > match.away_score
  const awayWon = match.away_score > match.home_score

  return (
    <div className="space-y-6">
      {/* Back button */}
      <button
        onClick={() => navigate('/schedule')}
        className="text-sm text-muted hover:text-text transition-colors"
      >
        &larr; Back to Schedule
      </button>

      {/* Match Header */}
      <div className="bg-surface border border-border rounded-xl p-6">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs text-muted uppercase tracking-wider">{match.league}</span>
          <span className="text-xs text-muted">{match.start_time_fmt}</span>
        </div>

        <div className="flex items-center justify-center gap-6 py-4">
          {/* Home */}
          <div className="flex-1 text-right">
            <div className={`text-lg font-bold ${homeWon ? 'text-win' : ''}`}>{match.home}</div>
            {home_form && (
              <div className="flex items-center justify-end gap-2 mt-1">
                <Badge variant={home_form.tier.toLowerCase()}>{home_form.tier}</Badge>
              </div>
            )}
          </div>

          {/* Score */}
          <div className="text-center px-6">
            <div className="text-4xl font-black tracking-widest">
              <span className={homeWon ? 'text-win' : ''}>{match.home_score}</span>
              <span className="text-muted mx-2">-</span>
              <span className={awayWon ? 'text-win' : ''}>{match.away_score}</span>
            </div>
            <div className="text-xs text-muted mt-1">{match.total_goals} total goals</div>
          </div>

          {/* Away */}
          <div className="flex-1 text-left">
            <div className={`text-lg font-bold ${awayWon ? 'text-win' : ''}`}>{match.away}</div>
            {away_form && (
              <div className="flex items-center gap-2 mt-1">
                <Badge variant={away_form.tier.toLowerCase()}>{away_form.tier}</Badge>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Key Stats Comparison */}
      {home_form && away_form && (
        <Card title="Key Stats" icon iconColor="bg-accent">
          <div className="flex items-center justify-between mb-3 px-1">
            <span className="text-sm font-semibold text-accent">{homeHandle}</span>
            <span className="text-sm font-semibold text-purple">{awayHandle}</span>
          </div>
          <StatCompare
            label="Win Rate"
            homeVal={home_form.win_rate}
            awayVal={away_form.win_rate}
            homeHighlight={home_form.win_rate > away_form.win_rate}
            awayHighlight={away_form.win_rate > home_form.win_rate}
            format={(v) => `${(v * 100).toFixed(1)}%`}
          />
          <StatCompare
            label="Matches"
            homeVal={home_form.matches_played}
            awayVal={away_form.matches_played}
          />
          <StatCompare
            label="Record"
            homeVal={`${home_form.wins}W-${home_form.losses}L-${home_form.draws}D`}
            awayVal={`${away_form.wins}W-${away_form.losses}L-${away_form.draws}D`}
          />
          <StatCompare
            label="Avg GF"
            homeVal={home_form.avg_goals_scored}
            awayVal={away_form.avg_goals_scored}
            homeHighlight={home_form.avg_goals_scored > away_form.avg_goals_scored}
            awayHighlight={away_form.avg_goals_scored > home_form.avg_goals_scored}
          />
          <StatCompare
            label="Avg GA"
            homeVal={home_form.avg_goals_conceded}
            awayVal={away_form.avg_goals_conceded}
            homeHighlight={home_form.avg_goals_conceded < away_form.avg_goals_conceded}
            awayHighlight={away_form.avg_goals_conceded < home_form.avg_goals_conceded}
          />
          <StatCompare
            label="Avg Total"
            homeVal={home_form.avg_total_goals}
            awayVal={away_form.avg_total_goals}
          />
          <StatCompare
            label="Recent WR"
            homeVal={home_form.recent?.win_rate}
            awayVal={away_form.recent?.win_rate}
            homeHighlight={home_form.recent?.win_rate > away_form.recent?.win_rate}
            awayHighlight={away_form.recent?.win_rate > home_form.recent?.win_rate}
            format={(v) => v != null ? `${(v * 100).toFixed(0)}%` : '--'}
          />
          <StatCompare
            label="Form"
            homeVal={home_form.form_trend}
            awayVal={away_form.form_trend}
            homeHighlight={home_form.form_trend === 'rising'}
            awayHighlight={away_form.form_trend === 'rising'}
          />
        </Card>
      )}

      {/* Over Rates Comparison */}
      {home_form && away_form && (
        <Card title="Over Rates" icon iconColor="bg-purple">
          <div className="flex items-center justify-between mb-2 px-1">
            <span className="text-xs font-semibold text-accent">{homeHandle}</span>
            <span className="text-xs font-semibold text-purple">{awayHandle}</span>
          </div>
          {['2.5', '3.5', '4.5', '5.5'].map((line) => (
            <OverRateBar
              key={line}
              line={line}
              homeRate={home_form.over_rates?.[line] || 0}
              awayRate={away_form.over_rates?.[line] || 0}
              h2hRate={h2h?.over_rates?.[line]}
            />
          ))}
        </Card>
      )}

      {/* H2H Section */}
      {h2h && (
        <Card title="Head-to-Head" icon iconColor="bg-cyan">
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-4">
            <div className="bg-surface2 rounded-lg p-3 text-center">
              <div className="text-lg font-bold">{h2h.total_matches}</div>
              <div className="text-xs text-muted">Matches</div>
            </div>
            <div className="bg-surface2 rounded-lg p-3 text-center">
              <div className="text-lg font-bold text-win">{h2h.home_wins}</div>
              <div className="text-xs text-muted">{homeHandle} wins</div>
            </div>
            <div className="bg-surface2 rounded-lg p-3 text-center">
              <div className="text-lg font-bold text-loss">{h2h.away_wins}</div>
              <div className="text-xs text-muted">{awayHandle} wins</div>
            </div>
            <div className="bg-surface2 rounded-lg p-3 text-center">
              <div className="text-lg font-bold text-muted">{h2h.draws}</div>
              <div className="text-xs text-muted">Draws</div>
            </div>
            <div className="bg-surface2 rounded-lg p-3 text-center">
              <div className="text-lg font-bold">{h2h.avg_total_goals}</div>
              <div className="text-xs text-muted">Avg Goals</div>
            </div>
          </div>

          {/* H2H Over Rates */}
          {h2h.over_rates && (
            <div className="flex gap-3 mb-4 flex-wrap">
              {Object.entries(h2h.over_rates).map(([line, rate]) => (
                <div key={line} className="bg-surface2 rounded-lg px-3 py-2 text-center text-sm">
                  <span className="text-muted">O{line}:</span>{' '}
                  <span className="font-semibold">{(rate * 100).toFixed(0)}%</span>
                </div>
              ))}
            </div>
          )}

          {/* Recent H2H matches */}
          {h2h.recent?.length > 0 && (
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
                {h2h.recent.map((m, i) => (
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
          )}
        </Card>
      )}

      {/* Picks on this match */}
      {picks?.length > 0 && (
        <Card title="Picks" icon iconColor="bg-win">
          <Table>
            <thead>
              <tr>
                <Th>Market</Th>
                <Th>Units</Th>
                <Th>Odds</Th>
                <Th>Hit Rate</Th>
                <Th>Edge</Th>
                <Th>Result</Th>
                <Th>Profit</Th>
              </tr>
            </thead>
            <tbody>
              {picks.map((p, i) => (
                <tr key={i}>
                  <Td className="font-medium">{p.market}</Td>
                  <Td>{p.units}u</Td>
                  <Td>{p.odds_american || '--'}</Td>
                  <Td>{p.hit_rate_pct}</Td>
                  <Td>{p.edge_pct || '--'}</Td>
                  <Td><Badge variant={p.result}>{p.result}</Badge></Td>
                  <Td className={p.profit >= 0 ? 'text-win' : 'text-loss'}>
                    {p.profit_display}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </Card>
      )}

      {/* Recommendation */}
      {recommendation && (
        <Card title="Recommendation" icon iconColor="bg-push">
          <div className="space-y-4">
            {/* Expected goals */}
            <div className="bg-surface2 rounded-lg p-4">
              <div className="flex items-center justify-between mb-2">
                <span className="text-sm font-semibold">Expected Total Goals</span>
                <span className="text-2xl font-black text-accent">{recommendation.expected_goals}</span>
              </div>
              <div className="flex items-center gap-4 text-xs text-muted">
                <span>
                  {homeHandle}: <span className={TIER_COLORS[recommendation.home_tier]}>{recommendation.home_tier}</span>
                  {' '}({recommendation.home_trend})
                </span>
                <span>
                  {awayHandle}: <span className={TIER_COLORS[recommendation.away_tier]}>{recommendation.away_tier}</span>
                  {' '}({recommendation.away_trend})
                </span>
              </div>
            </div>

            {/* Best lines */}
            {recommendation.best_lines?.length > 0 && (
              <div>
                <h3 className="text-sm font-semibold mb-2">Top Plays</h3>
                <div className="space-y-2">
                  {recommendation.best_lines.map((line, i) => (
                    <div
                      key={i}
                      className={`rounded-lg p-3 flex items-center justify-between ${
                        i === 0
                          ? 'bg-accent/10 border border-accent/30'
                          : 'bg-surface2'
                      }`}
                    >
                      <div className="flex items-center gap-3">
                        {i === 0 && (
                          <span className="text-xs font-bold uppercase text-accent bg-accent/20 px-2 py-0.5 rounded">
                            Best
                          </span>
                        )}
                        <span className="text-sm font-semibold">{line.market}</span>
                      </div>
                      <div className="flex items-center gap-4 text-sm">
                        <span className="text-muted">
                          Combined: <span className="font-semibold text-text">{line.combined_rate_pct}</span>
                        </span>
                        {line.h2h_rate !== null && (
                          <span className="text-muted">
                            H2H: <span className="font-semibold text-text">{(line.h2h_rate * 100).toFixed(0)}%</span>
                          </span>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Moneyline rec */}
            {recommendation.moneyline && (
              <div className="bg-surface2 rounded-lg p-3">
                <h3 className="text-sm font-semibold mb-1">Moneyline Edge</h3>
                <p className="text-sm text-muted">
                  <span className="font-semibold text-text">{recommendation.moneyline.side}</span>
                  {' '}dominates this H2H ({recommendation.moneyline.h2h_rate_pct} H2H win rate)
                  with {recommendation.moneyline.recent_form_pct} recent form
                </p>
              </div>
            )}

            {/* No plays */}
            {!recommendation.best_lines?.length && !recommendation.moneyline && (
              <div className="text-center py-4 text-muted text-sm">
                No strong plays identified for this matchup
              </div>
            )}
          </div>
        </Card>
      )}

      {/* No recommendation data */}
      {!recommendation && !home_form && !away_form && (
        <Card title="Recommendation" icon iconColor="bg-push">
          <Empty text="Insufficient player data for analysis" />
        </Card>
      )}
    </div>
  )
}
