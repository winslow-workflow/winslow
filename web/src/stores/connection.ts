// The one wire of the page and its state for the status bar. Components and
// stores reach the wire through the reads and the lane methods here alone.

import { defineStore } from 'pinia'
import { computed, markRaw, ref, watch } from 'vue'

import { readConfig } from '@/protocol/config'
import { AppReads, SessionReads } from '@/protocol/reads'
import { Wire, type LaneListener, type Refusal, type WireState } from '@/protocol/wire'
import { useNoticesStore } from './notices'

export const useConnectionStore = defineStore('connection', () => {
  const config = readConfig()
  const wire = markRaw(new Wire({ url: config.wsUrl, ticket: config.ticket, ticketUrl: config.ticketUrl }))
  const app = markRaw(new AppReads(wire))
  const notices = useNoticesStore()

  const state = ref<WireState>('idle')
  const user = ref<string | null>(null)
  const env = ref<string | null>(null)
  const envColor = ref<string | null>(null)
  const refusal = ref<Refusal | null>(null)
  const connected = computed(() => state.value === 'connected')

  wire.onState((w) => {
    const previous = state.value
    state.value = w.state
    user.value = w.user
    env.value = w.env
    envColor.value = w.envColor
    refusal.value = w.refusal
    if (w.state === 'connected' && previous !== 'connected') {
      notices.record('info', `connected to ${w.url} as ${w.user}`)
    } else if (w.state === 'reconnecting' && previous === 'connected') {
      notices.record('warning', 'the connection dropped - reconnecting')
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
    env,
    envColor,
    refusal,
    connected,
    connect,
    retry,
    whenConnected,
    session,
    subscribeLane,
    unsubscribeLane,
  }
})
