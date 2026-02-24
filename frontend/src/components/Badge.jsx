const styles = {
  win:       'bg-win/15 text-win',
  loss:      'bg-loss/15 text-loss',
  push:      'bg-push/15 text-push',
  pending:   'bg-accent/15 text-accent',
  elite:     'bg-purple/15 text-purple',
  solid:     'bg-win/15 text-win',
  watchlist: 'bg-push/15 text-push',
  blocked:   'bg-loss/15 text-loss',
  new:       'bg-accent/15 text-accent',
}

export default function Badge({ variant, children }) {
  const cls = styles[variant] || styles.pending
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-semibold uppercase ${cls}`}>
      {children}
    </span>
  )
}
