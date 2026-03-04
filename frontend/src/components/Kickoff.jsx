import { useState, useEffect } from 'react'

/**
 * Live-updating kickoff countdown + local time display.
 * Ticks every 10s for countdowns > 1 minute, every 1s when under 1 minute.
 */
export default function Kickoff({ startTime }) {
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000))

  useEffect(() => {
    const diff = startTime - Math.floor(Date.now() / 1000)
    // Tick every second when under 60s, otherwise every 10s
    const interval = diff > 0 && diff <= 60 ? 1000 : 10_000

    const id = setInterval(() => {
      setNow(Math.floor(Date.now() / 1000))
    }, interval)

    return () => clearInterval(id)
  }, [startTime, now])

  const diff = startTime - now
  const localTime = new Date(startTime * 1000).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  })

  if (diff <= 0) {
    return (
      <div className="text-right">
        <span className="text-win text-xs font-medium">LIVE</span>
        <div className="text-[10px] text-muted">{localTime}</div>
      </div>
    )
  }

  const mins = Math.floor(diff / 60)
  const secs = diff % 60
  let countdown
  if (mins < 1) {
    countdown = `${secs}s`
  } else if (mins < 60) {
    countdown = `${mins}m`
  } else {
    const hrs = Math.floor(mins / 60)
    const rem = mins % 60
    countdown = `${hrs}h${rem > 0 ? ` ${rem}m` : ''}`
  }

  return (
    <div className="text-right">
      <span className="text-xs text-muted">{countdown}</span>
      <div className="text-[10px] text-muted/60">{localTime}</div>
    </div>
  )
}
