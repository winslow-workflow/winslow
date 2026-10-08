import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { LaneFrame } from '@/protocol/frames'
import type { Ack, SessionSnapshot } from '@/protocol/model'
import * as wireModule from '@/protocol/wire'
import type { LaneListener } from '@/protocol/wire'
import { useConnectionStore } from '@/stores/connection'
import { useNoticesStore } from '@/stores/notices'
import { MAX_OPEN_LANES, useSessionStore } from '@/stores/session'

// The wire behind the stores: canned results per read kind, canned acks, and
// the lane listeners the stores subscribe, so a test feeds frames directly.
vi.mock('@/protocol/wire', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/protocol/wire')>()
  const instances: FakeWire[] = []

  class FakeWire {
    url = 'ws://fake/ws'
    state = 'idle'
    user: string | null = null
    scopes: string[] = []
    refusal = null
    listeners = new Set<(wire: FakeWire) => void>()
    lanes = new Map<string, LaneListener>()
    results: Record<string, unknown> = {
      sessions: [],
      manifests: [],
      descriptors: { workflows: [], overrides: [] },
    }
    acks: Ack[] = []
    requests: { kind: string; sessionId?: string }[] = []
    actions: { action: string; fields: Record<string, unknown> }[] = []

    constructor() {
      instances.push(this)
    }

    onState(listener: (wire: FakeWire) => void) {
      this.listeners.add(listener)
      return () => this.listeners.delete(listener)
    }

    connect() {
      this.state = 'connected'
      this.user = 'can'
      for (const listener of this.listeners) listener(this)
    }

    async request(kind: string, _fields: Record<string, unknown>, sessionId?: string) {
      this.requests.push({ kind, sessionId })
      if (kind in this.results) return this.results[kind]
      throw new actual.RequestError(`no ${kind}`)
    }

    async action(_sessionId: string, action: string, fields: Record<string, unknown>) {
      this.actions.push({ action, fields })
      return this.acks.shift() ?? { accepted: true, reason: null }
    }

    subscribe(sessionId: string, listener: LaneListener) {
      this.lanes.set(sessionId, listener)
    }

    unsubscribe(sessionId: string) {
      this.lanes.delete(sessionId)
    }

    subscribeTaskLog() {
      return Promise.resolve([])
    }

    unsubscribeTaskLog() {}
  }

  return { ...actual, Wire: FakeWire, __wires: instances }
})

interface FakeWireLike {
  results: Record<string, unknown>
  acks: Ack[]
  requests: { kind: string; sessionId?: string }[]
  actions: { action: string; fields: Record<string, unknown> }[]
  lanes: Map<string, LaneListener>
}

function wire(): FakeWireLike {
  const wires = (wireModule as unknown as { __wires: FakeWireLike[] }).__wires
  return wires[wires.length - 1]!
}

const snapshot: SessionSnapshot = {
  session_id: 's1',
  workflow: 'etl',
  status: 'ACTIVE',
  tasks: { a: 'INITIALIZED', b: 'COMPLETED' },
  session_log_backlog: ['init'],
  batches: [],
  cache_names: ['c'],
}

// Omit over each member of the union, so a frame body keeps its own fields.
type DistributiveOmit<T, K extends PropertyKey> = T extends unknown ? Omit<T, K> : never

function frame(fields: DistributiveOmit<LaneFrame, 'session_id' | 'seq'>): LaneFrame {
  return { session_id: 's1', seq: 1, ...fields } as LaneFrame
}

function batch(uuid: string, completed = false) {
  return {
    uuid,
    action: 'RUN',
    status: completed ? 'FINISHED' : 'RUNNING',
    task_count: 2,
    tasks: { a: 'A', b: 'B' },
    options: null,
    created_at: 1,
    started_at: 1,
    completed_at: completed ? 2 : null,
    error: null,
    user: null,
  } as const
}

async function openStore(id = 's1') {
  const connection = useConnectionStore()
  connection.connect()
  wire().results.snapshot = { ...snapshot, session_id: id }
  wire().results.roster = []
  wire().results.batch_options = { force_run: false }
  wire().results.history = []
  const store = useSessionStore(id)
  await store.open()
  return store
}

function lane(id = 's1'): LaneListener {
  return wire().lanes.get(id)!
}

describe('open', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('waits for the hello, then reads and subscribes', async () => {
    const connection = useConnectionStore()
    const store = useSessionStore('s1')
    const pending = store.open()
    await Promise.resolve()
    expect(wire().requests.filter((request) => request.sessionId === 's1')).toHaveLength(0)
    wire().results.snapshot = snapshot
    wire().results.roster = []
    wire().results.batch_options = { dry_run: true }
    wire().results.history = []
    connection.connect()
    await pending
    expect(store.status).toBe('ACTIVE')
    expect(store.statuses).toEqual(snapshot.tasks)
    expect(store.sessionLog).toEqual(['init'])
    expect(store.batchOptions).toEqual({ dry_run: true })
    expect(store.opened).toBe(true)
    expect(wire().lanes.has('s1')).toBe(true)
  })

  it('parks the least recently opened lane beyond the cap', async () => {
    const stores = []
    for (let index = 0; index <= MAX_OPEN_LANES + 1; index += 1) {
      stores.push(await openStore(`cap-${index}`))
    }
    expect(stores[0]!.opened).toBe(false)
    expect(wire().lanes.has('cap-0')).toBe(false)
    expect(stores[stores.length - 1]!.opened).toBe(true)
  })
})

describe('the lane reducer', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('mirrors statuses, execution statuses and log lines', async () => {
    const store = await openStore()
    lane().onFrame(frame({ type: 'log_batch', task_key: 'a', batch_uuid: 'b1', lines: ['one', 'two'] }))
    expect(store.taskLogs.a).toBe('two')
    expect(store.recordLogs['b1|a']).toBe('two')
    lane().onFrame(
      frame({ type: 'execution_status', task_key: 'a', batch_uuid: 'b1', status: 'RUNNING', origin: 'run' }),
    )
    expect(store.executionStatuses['b1|a']).toBe('RUNNING')
    expect(store.recordStarted['b1|a']).toBeTypeOf('number')
    lane().onFrame(frame({ type: 'task_status', key: 'a', status: 'COMPLETED', origin: 'run' }))
    expect(store.statuses.a).toBe('COMPLETED')
    expect(store.taskLogs.a).toBe('')
    lane().onFrame(frame({ type: 'session_log_batch', lines: ['more'] }))
    expect(store.sessionLog).toEqual(['init', 'more'])
    lane().onFrame(frame({ type: 'cache_updated', cache_name: 'c' }))
    expect(store.cacheVersion).toBe(1)
    lane().onFrame(frame({ type: 'session_ended' }))
    expect(store.ended).toBe(true)
  })

  it('toasts a bulk submit once its batch_created event arrives', async () => {
    const store = await openStore()
    const notices = useNoticesStore()
    wire().acks.push({ accepted: true, reason: null, batch_uuid: 'b1' })
    await store.runBulk('Running', ['a', 'b'])
    expect(wire().actions[0]).toMatchObject({ action: 'run_tasks', fields: { keys: ['a', 'b'] } })
    expect(notices.queue).toHaveLength(0)
    lane().onFrame(frame({ type: 'batch_created', info: batch('b1') }))
    expect(notices.queue.map((notice) => notice.body)).toEqual(['Running 2 tasks'])
    expect(store.batches.map((row) => row.uuid)).toEqual(['b1'])
  })

  it('toasts at once when the event beat the ack', async () => {
    const store = await openStore()
    const notices = useNoticesStore()
    lane().onFrame(frame({ type: 'batch_created', info: batch('b2') }))
    wire().acks.push({ accepted: true, reason: null, batch_uuid: 'b2' })
    await store.runBulk('Checking', ['a', 'b'], true)
    expect(notices.queue.map((notice) => notice.body)).toEqual(['Checking 2 tasks'])
  })

  it('refreshes the history on a completed batch and on a healed snapshot', async () => {
    const store = await openStore()
    const before = wire().requests.filter((request) => request.kind === 'history').length
    lane().onFrame(frame({ type: 'batch_completed', info: batch('b1', true) }))
    await Promise.resolve()
    expect(wire().requests.filter((request) => request.kind === 'history')).toHaveLength(before + 1)
    lane().onSnapshot({ ...snapshot, session_log_backlog: ['other'] }, true)
    await Promise.resolve()
    expect(wire().requests.filter((request) => request.kind === 'history')).toHaveLength(before + 2)
    // A healed snapshot keeps the log the lane already streamed.
    expect(store.sessionLog).toEqual(['init'])
  })

  it('ends the session when the subscribe is refused', async () => {
    const store = await openStore()
    lane().onRefused('s1 has ended')
    expect(store.ended).toBe(true)
    expect(store.laneRefusal).toBe('s1 has ended')
  })

  it('reports a refused action as a warning', async () => {
    const store = await openStore()
    const notices = useNoticesStore()
    wire().acks.push({ accepted: false, reason: 'no such batch' })
    await store.stopBatch('nope')
    expect(notices.queue[0]).toMatchObject({ variant: 'warning', body: 'no such batch' })
  })
})
