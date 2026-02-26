import { NavLink, Outlet } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'

const nav = [
  { to: '/',         label: 'Dashboard', icon: '◫' },
  { to: '/schedule', label: 'Schedule',  icon: '▦' },
  { to: '/picks',    label: 'Picks',     icon: '◉' },
  { to: '/players',  label: 'Players',   icon: '⬡' },
  { to: '/matches',  label: 'Matches',   icon: '⬢' },
  { to: '/leagues',  label: 'Leagues',   icon: '◈' },
]

export default function Layout() {
  const { data: scan } = useQuery({
    queryKey: ['scan'],
    queryFn: api.scanStatus,
    refetchInterval: 60_000,
  })

  return (
    <div className="flex h-screen overflow-hidden">
      {/* Sidebar */}
      <aside className="w-56 shrink-0 bg-surface border-r border-border flex flex-col">
        <div className="p-5 border-b border-border">
          <h1 className="text-lg font-bold tracking-tight">
            <span className="text-accent">Esporf</span>
          </h1>
          <p className="text-xs text-muted mt-0.5">eSoccer Analytics</p>
        </div>

        <nav className="flex-1 p-3 space-y-1">
          {nav.map(({ to, label, icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                `flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${
                  isActive
                    ? 'bg-accent/10 text-accent'
                    : 'text-muted hover:text-text hover:bg-surface2'
                }`
              }
            >
              <span className="text-base">{icon}</span>
              {label}
            </NavLink>
          ))}
        </nav>

        {/* Scan status footer */}
        <div className="p-4 border-t border-border">
          <div className="flex items-center gap-2">
            <div className={`w-2 h-2 rounded-full ${scan?.last_scan ? 'bg-win animate-pulse' : 'bg-push'}`} />
            <span className="text-xs text-muted truncate">
              {scan?.last_scan
                ? `Scan #${scan.last_scan.scan_number}`
                : `${scan?.database?.total_matches ?? '...'} matches`}
            </span>
          </div>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-y-auto">
        <div className="max-w-7xl mx-auto p-6">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
