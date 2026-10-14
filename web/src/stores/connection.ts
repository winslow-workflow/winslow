// The one wire of the page and its state for the status bar. Components and
// stores reach the wire through the reads and the lane methods here alone.

import { defineStore } from 'pinia'
import { computed, markRaw, ref, watch } from 'vue'

import { readConfig } from '@/protocol/config'
import { AppReads, SessionReads } from '@/protocol/reads'
import {
  CREDENTIAL_REFUSED,
  PLANNED_CLOSES,
  Wire,
  type LaneListener,
  type Refusal,
  type WireState,
} from '@/protocol/wire'
import { useNoticesStore } from './notices'

// The token a person typed into the standalone page, kept for the tab.
const TOKEN_KEY = 'winslow.token'

function storedToken(): string | null {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

function storeToken(token: string | null): void {
  try {
    if (token === null) window.sessionStorage.removeItem(TOKEN_KEY)
    else window.sessionStorage.setItem(TOKEN_KEY, token)
  } catch {
    // A private window or blocked storage: the token lives for this page load.
  }
}

export const useConnectionStore = defineStore('connection', () => {
  const config = readConfig()
  const wire = markRaw(
    new Wire({
      url: config.wsUrl,
      credential: config.embedded ? config.credential : storedToken(),
      credentialUrl: config.credentialUrl,
    }),
  )
  const app = markRaw(new AppReads(wire))
  const notices = useNoticesStore()

  const state = ref<WireState>('idle')
  const user = ref<string | null>(null)
  const service = ref<string | null>(null)
  const env = ref<string | null>(null)
  const envColor = ref<string | null>(null)
  const scopes = ref<string[]>([])
  const refusal = ref<Refusal | null>(null)
  const connected = computed(() => state.value === 'connected')
  // The standalone page asks for a token once the server refuses the credential.
  const needsToken = computed(() => !config.embedded && refusal.value?.code === CREDENTIAL_REFUSED)

  wire.onState((w) => {
    const previous = state.value
    state.value = w.state
    user.value = w.user
    service.value = w.service
    env.value = w.env
    envColor.value = w.envColor
    scopes.value = [...w.scopes]
    refusal.value = w.refusal
    if (w.state === 'refused' && w.refusal?.code === CREDENTIAL_REFUSED && wire.credential !== null) {
      // The stored token is bad: forget it, so the field shows again.
      wire.credential = null
      storeToken(null)
    }
    if (w.state === 'connected' && previous !== 'connected') {
      notices.record('info', `connected to ${w.url} as ${w.user}`)
    } else if (w.state === 'reconnecting' && previous === 'connected') {
      if (w.closeCode !== null && PLANNED_CLOSES.has(w.closeCode)) {
        notices.record('info', 'credential renewed - reconnecting')
      } else {
        notices.record('warning', 'the connection dropped - reconnecting')
      }
    } else if (w.state === 'refused' && w.refusal) {
      notices.record('error', `the serve process refused the hello - ${w.refusal.reason}`)
    }
  })

  function connect() {
    wire.connect()
  }

  function retry() {
    wire.retry()
  }

  // The token of the standalone page: kept for the tab, sent in the next hello.
  function setToken(token: string) {
    wire.credential = token
    storeToken(token)
    wire.retry()
  }

  // Membership alone: the server expands the implications. The wire clears
  // its scopes on close, so a control is disabled before the hello and after a drop.
  function allows(scopeName: string | null, permission: string): boolean {
    if (scopeName === null) return false
    return scopes.value.includes(`${scopeName}:${permission}`) || scopes.value.includes(`*:${permission}`)
  }

  // Resolves once the hello is done, at once when it already is.
  function whenConnected(): Promise<void> {
    if (connected.value) return Promise.resolve()
    return new Promise((resolve) => {
      const stop = watch(connected, (value) => {
        if (!value) return
        stop()
        resolve()
      })
    })
  }

  function session(sessionId: string): SessionReads {
    return new SessionReads(wire, sessionId)
  }

  function subscribeLane(sessionId: string, listener: LaneListener) {
    wire.subscribe(sessionId, listener)
  }

  function unsubscribeLane(sessionId: string, listener: LaneListener) {
    wire.unsubscribe(sessionId, listener)
  }

  return {
    url: config.wsUrl,
    app,
    state,
    user,
    service,
    env,
    envColor,
    scopes,
    refusal,
    needsToken,
    setToken,
    allows,
    connected,
    connect,
    retry,
    whenConnected,
    session,
    subscribeLane,
    unsubscribeLane,
  }
})
