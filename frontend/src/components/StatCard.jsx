export default function StatCard({ label, value, sub, color }) {
  const colorClass =
    color === 'green' ? 'text-win' :
    color === 'red'   ? 'text-loss' :
    color === 'blue'  ? 'text-accent' :
    color === 'amber' ? 'text-push' :
    'text-text'

  return (
    <div className="bg-surface border border-border rounded-xl p-5">
      <div className="text-xs uppercase tracking-wider text-muted mb-1">{label}</div>
      <div className={`text-2xl font-bold tracking-tight ${colorClass}`}>{value}</div>
      {sub && <div className="text-xs text-muted mt-1">{sub}</div>}
    </div>
  )
}
