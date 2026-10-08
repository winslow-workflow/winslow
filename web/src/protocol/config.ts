// The page configuration the host injects as window.WINSLOW. The served
// index.html sets nothing, so the defaults point at the serving origin.

export interface WinslowConfig {
  // The websocket URL. Defaults to /ws on the page origin.
  wsUrl?: string
  // The ticket of a browser client on a remote bind (see winslow.serve.mint_ticket).
  ticket?: string | null
  // A URL that answers a fresh ticket as text, for the reconnect after an expiry.
  ticketUrl?: string | null
}

declare global {
  interface Window {
    WINSLOW?: WinslowConfig
  }
}

export interface ResolvedConfig {
  wsUrl: string
  ticket: string | null
  ticketUrl: string | null
}

export function defaultWsUrl(location: Location = window.location): string {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${scheme}://${location.host}/ws`
}

export function readConfig(): ResolvedConfig {
  const injected = window.WINSLOW ?? {}
  return {
    wsUrl: injected.wsUrl || defaultWsUrl(),
    ticket: injected.ticket ?? null,
    ticketUrl: injected.ticketUrl ?? null,
  }
}
