export function Table({ children }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">{children}</table>
    </div>
  )
}

export function Th({ children, className = '' }) {
  return (
    <th className={`text-left px-3 py-2.5 text-xs font-semibold uppercase tracking-wider text-muted border-b border-border ${className}`}>
      {children}
    </th>
  )
}

export function Td({ children, className = '' }) {
  return (
    <td className={`px-3 py-3 border-b border-border ${className}`}>
      {children}
    </td>
  )
}
