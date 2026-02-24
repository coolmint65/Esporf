import { Routes, Route, Navigate } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import Picks from './pages/Picks'
import Players from './pages/Players'
import PlayerDetail from './pages/PlayerDetail'
import Matches from './pages/Matches'
import Leagues from './pages/Leagues'

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/picks" element={<Picks />} />
        <Route path="/players" element={<Players />} />
        <Route path="/players/:name" element={<PlayerDetail />} />
        <Route path="/matches" element={<Matches />} />
        <Route path="/leagues" element={<Leagues />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
