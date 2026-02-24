export function Loading({ text = 'Loading...' }) {
  return <div className="text-center py-10 text-muted text-sm">{text}</div>
}

export function Empty({ text = 'No data' }) {
  return <div className="text-center py-10 text-muted text-sm">{text}</div>
}

export function ErrorMsg({ error }) {
  return (
    <div className="text-center py-10 text-loss text-sm">
      {error?.message || 'Something went wrong'}
    </div>
  )
}
