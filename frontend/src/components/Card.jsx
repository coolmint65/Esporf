export default function Card({ title, icon, iconColor, children, className = '' }) {
  return (
    <div className={`bg-surface border border-border rounded-xl p-5 ${className}`}>
      {title && (
        <div className="flex items-center gap-2 mb-4">
          {icon && (
            <div className={`w-1.5 h-1.5 rounded-full ${iconColor || 'bg-accent'}`} />
          )}
          <h2 className="text-sm font-semibold">{title}</h2>
        </div>
      )}
      {children}
    </div>
  )
}
