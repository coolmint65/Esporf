const BASE = '/api';

async function fetchApi(path, params = {}) {
  const url = new URL(BASE + path, window.location.origin);
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
  });
  const res = await fetch(url);
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status}: ${body}`);
  }
  return res.json();
}

export const api = {
  health:       ()            => fetchApi('/'),
  stats:        (params = {}) => fetchApi('/stats', params),
  breakdown:    ()            => fetchApi('/stats/breakdown'),
  livePicks:    ()            => fetchApi('/picks/live'),
  pickHistory:  (params = {}) => fetchApi('/picks/history', params),
  player:       (name, params = {}) => fetchApi(`/player/${encodeURIComponent(name)}`, params),
  players:      (params = {}) => fetchApi('/players', params),
  h2h:          (a, b, params = {}) => fetchApi(`/h2h/${encodeURIComponent(a)}/${encodeURIComponent(b)}`, params),
  recentMatches:(params = {}) => fetchApi('/matches/recent', params),
  leagues:      ()            => fetchApi('/leagues'),
  scanStatus:   ()            => fetchApi('/scan/status'),
  schedule:     (params = {}) => fetchApi('/schedule', params),
  matchDetail:  (matchId)     => fetchApi(`/match/${encodeURIComponent(matchId)}/detail`),
};
