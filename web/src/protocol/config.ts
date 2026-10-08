// The page configuration the host injects as window.WINSLOW. The served
// index.html sets nothing, so the defaults point at the serving origin.

export interface WinslowConfig {
  // The websocket URL. Defaults to /ws on the page origin.
  wsUrl?: string
  // The credential of a browser client on a remote bind: a ticket the front
  // application minted (see winslow.serve.mint_ticket).
  credential?: string | null
  // A URL that answers a fresh credential as text, for the reconnect after an expiry.
  credentialUrl?: string | null
}

declare global {
  interface Window {
    WINSLOW?: WinslowConfig
  }
}

export interface ResolvedConfig {
  wsUrl: string
  credential: string | null
  credentialUrl: string | null
  // True when a front application injected window.WINSLOW. The application
  // owns the credential then, so the page shows no token field.
  embedded: boolean
}

export function defaultWsUrl(location: Location = window.location): string {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${scheme}://${location.host}/ws`
}

export function readConfig(): ResolvedConfig {
  const injected = window.WINSLOW ?? {}
  return {
    wsUrl: injected.wsUrl || defaultWsUrl(),
    credential: injected.credential ?? null,
    credentialUrl: injected.credentialUrl ?? null,
    embedded: window.WINSLOW !== undefined,
  }
}
