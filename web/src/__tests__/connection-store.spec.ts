import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import * as wireModule from '@/protocol/wire'
import { useConnectionStore } from '@/stores/connection'

// The wire behind the connection store: the state and the refusal a test sets,
// the token the store writes, and the retries it asks for.
vi.mock('@/protocol/wire', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/protocol/wire')>()
  const instances: FakeWire[] = []

  class FakeWire {
    url = 'ws://fake/ws'
    credential: string | null
    state = 'idle'
    user: string | null = null
    service = null
    env = null
    envColor = null
    scopes: string[] = []
    refusal: { code: number; reason: string; detail: null } | null = null
    retries = 0
    listeners = new Set<(wire: FakeWire) => void>()

    constructor(options: { credential?: string | null }) {
      this.credential = options.credential ?? null
      instances.push(this)
    }

    onState(listener: (wire: FakeWire) => void) {
      this.listeners.add(listener)
      return () => this.listeners.delete(listener)
    }

    connect() {}

    retry() {
      this.retries += 1
    }

    emit(state: string, fields: Record<string, unknown> = {}) {
      Object.assign(this, fields, { state })
      for (const listener of this.listeners) listener(this)
    }

    subscribe() {}
    unsubscribe() {}
  }

  return { ...actual, Wire: FakeWire, __instances: instances }
})

interface FakeWire {
  credential: string | null
  retries: number
  emit(state: string, fields?: Record<string, unknown>): void
}

function wires(): FakeWire[] {
  return (wireModule as unknown as { __instances: FakeWire[] }).__instances
}

describe('the connection store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    wires().length = 0
    window.sessionStorage.clear()
  })

  it('denies before the hello and answers from the scopes after it', () => {
    const connection = useConnectionStore()
    const wire = wires()[0]!
    expect(connection.allows('etl', 'view_task')).toBe(false)
    wire.emit('connected', { user: 'can', scopes: ['etl:check_task', 'etl:view_task', '*:view_task'] })
    expect(connection.allows('etl', 'check_task')).toBe(true)
    expect(connection.allows('etl', 'run_task')).toBe(false)
    expect(connection.allows('reports', 'view_task')).toBe(true)
    expect(connection.allows(null, 'view_task')).toBe(false)
    wire.emit('reconnecting', { scopes: [] })
    expect(connection.allows('etl', 'view_task')).toBe(false)
  })

  it('asks for a token on a credential refusal and sends it with a retry', () => {
    const connection = useConnectionStore()
    const wire = wires()[0]!
    expect(connection.needsToken).toBe(false)
    wire.emit('refused', { refusal: { code: wireModule.CREDENTIAL_REFUSED, reason: 'no credential', detail: null } })
    expect(connection.needsToken).toBe(true)
    connection.setToken('wsl_secret')
    expect(wire.credential).toBe('wsl_secret')
    expect(wire.retries).toBe(1)
    expect(window.sessionStorage.getItem('winslow.token')).toBe('wsl_secret')
  })

  it('keeps the field away from an origin refusal', () => {
    const connection = useConnectionStore()
    const wire = wires()[0]!
    wire.emit('refused', { refusal: { code: wireModule.ORIGIN_REFUSED, reason: 'origin', detail: null } })
    expect(connection.needsToken).toBe(false)
  })

  it('forgets a stored token the server refuses', () => {
    window.sessionStorage.setItem('winslow.token', 'stale')
    useConnectionStore()
    const wire = wires()[0]!
    expect(wire.credential).toBe('stale')
    wire.emit('refused', { refusal: { code: wireModule.CREDENTIAL_REFUSED, reason: 'bad bearer token', detail: null } })
    expect(wire.credential).toBeNull()
    expect(window.sessionStorage.getItem('winslow.token')).toBeNull()
  })
})
