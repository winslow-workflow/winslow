import { describe, expect, it, vi } from 'vitest'

import {
  CREDENTIAL_EXPIRED,
  CREDENTIAL_REFUSED,
  RECONNECT_DELAY,
  REFUSAL_CODES,
  ConnectionDown,
  RequestError,
  Wire,
  type LaneListener,
  type SocketLike,
} from '@/protocol/wire'
import type { SessionSnapshot } from '@/protocol/model'

// A fake socket: the test opens it, feeds frames, and reads what the wire sent.
class FakeSocket implements SocketLike {
  static instances: FakeSocket[] = []
  readyState = 0
  sent: Record<string, unknown>[] = []
  closed: { code?: number; reason?: string } | null = null
  onopen: ((event: unknown) => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null
  onclose: ((event: { code: number; reason: string }) => void) | null = null
  onerror: ((event: unknown) => void) | null = null

  constructor(public url: string) {
    FakeSocket.instances.push(this)
  }

  send(data: string) {
    this.sent.push(JSON.parse(data))
  }

  close(code?: number, reason?: string) {
    this.closed = { code, reason }
    this.readyState = 3
    this.onclose?.({ code: code ?? 1000, reason: reason ?? '' })
  }

  open() {
    this.readyState = 1
    this.onopen?.({})
  }

  feed(frame: object) {
    this.onmessage?.({ data: JSON.stringify(frame) })
  }

  drop(code = 1006) {
    this.readyState = 3
    this.onclose?.({ code, reason: '' })
  }

  get last() {
    return this.sent[this.sent.length - 1]
  }
}

function connect(options: { credentialUrl?: string; fetchCredential?: (url: string) => Promise<string> } = {}) {
  FakeSocket.instances = []
  const wire = new Wire({
    url: 'ws://test/ws',
    credential: 'tok',
    socketFactory: (url) => new FakeSocket(url),
    ...options,
  })
  wire.connect()
  const socket = FakeSocket.instances[0]!
  return { wire, socket }
}

function handshake(socket: FakeSocket) {
  socket.open()
  socket.feed({
    type: 'hello_ok',
    user: 'can',
    env: 'local',
    env_color: 'royalblue',
    scopes: [],
    credential: 'ticket',
    expires_at: null,
  })
}

const snapshot: SessionSnapshot = {
  session_id: 's1',
  workflow: 'etl',
  status: 'ACTIVE',
  tasks: { a: 'INITIALIZED' },
  session_log_backlog: [],
  batches: [],
  cache_names: [],
}

function listener() {
  return {
    onSnapshot: vi.fn(),
    onFrame: vi.fn(),
    onRefused: vi.fn(),
  } satisfies LaneListener
}

describe('the handshake', () => {
  it('sends the hello on open and reports the user after hello_ok', () => {
    const { wire, socket } = connect()
    expect(wire.state).toBe('connecting')
    socket.open()
    expect(socket.last).toEqual({ type: 'hello', token: 'tok' })
    socket.feed({
      type: 'hello_ok',
      user: 'can',
      env: 'uat',
      env_color: 'gold',
      scopes: [],
      credential: 'ticket',
      expires_at: null,
    })
    expect(wire.state).toBe('connected')
    expect(wire.user).toBe('can')
    expect(wire.env).toBe('uat')
    expect(wire.envColor).toBe('gold')
  })

  it('holds the scopes of the hello and drops them with the socket', () => {
    const { wire, socket } = connect()
    expect(wire.allows('etl', 'run_task')).toBe(false)
    socket.open()
    socket.feed({
      type: 'hello_ok',
      user: 'can',
      env: 'local',
      env_color: 'royalblue',
      scopes: ['etl:check_task', 'etl:view_task', '*:view_task'],
      credential: 'ticket',
      expires_at: 1757400000,
    })
    expect(wire.allows('etl', 'check_task')).toBe(true)
    expect(wire.allows('etl', 'run_task')).toBe(false)
    expect(wire.allows('reports', 'view_task')).toBe(true)
    expect(wire.allows(null, 'view_task')).toBe(false)
    expect(wire.expiresAt).toBe(1757400000)
    expect(wire.credentialKind).toBe('ticket')
    socket.close(1006, 'dropped')
    expect(wire.scopes).toEqual([])
    expect(wire.allows('etl', 'view_task')).toBe(false)
  })

  it('keeps the hello_error reason when the refusal close follows', () => {
    const { wire, socket } = connect()
    socket.open()
    socket.feed({ type: 'hello_error', reason: 'bad bearer token', detail: null })
    socket.close(CREDENTIAL_REFUSED, 'bad bearer token')
    expect(wire.state).toBe('refused')
    expect(wire.refusal).toEqual({ code: null, reason: 'bad bearer token', detail: null })
    // No reconnect timer: a refusal waits for retry.
    expect(FakeSocket.instances).toHaveLength(1)
    wire.retry()
    expect(FakeSocket.instances).toHaveLength(2)
  })

  it('fetches a fresh credential on a refusal when a credential URL exists', async () => {
    vi.useFakeTimers()
    const fetchCredential = vi.fn(async () => 'fresh-ticket')
    const { wire, socket } = connect({ credentialUrl: 'https://front/ticket', fetchCredential })
    socket.open()
    socket.close(CREDENTIAL_REFUSED, 'ticket expired')
    await Promise.resolve()
    await Promise.resolve()
    expect(fetchCredential).toHaveBeenCalledWith('https://front/ticket')
    expect(wire.credential).toBe('fresh-ticket')
    vi.runOnlyPendingTimers()
    expect(FakeSocket.instances).toHaveLength(2)
    FakeSocket.instances[1]!.open()
    expect(FakeSocket.instances[1]!.last).toMatchObject({ type: 'hello', token: 'fresh-ticket' })
    vi.useRealTimers()
  })
})

describe('the requests', () => {
  it('answers a result frame by request id and rejects an error frame', async () => {
    const { wire, socket } = connect()
    handshake(socket)
    const pending = wire.request('sessions')
    const sent = socket.last as { request_id: string; kind: string; type: string }
    expect(sent).toMatchObject({ type: 'request', kind: 'sessions' })
    socket.feed({ type: 'result', request_id: sent.request_id, kind: 'sessions', result: [1, 2] })
    await expect(pending).resolves.toEqual([1, 2])

    const refused = wire.request('snapshot', {}, 'nope')
    const sent2 = socket.last as { request_id: string; session_id: string }
    expect(sent2.session_id).toBe('nope')
    socket.feed({ type: 'error', request_id: sent2.request_id, reason: 'no session', detail: 'tb' })
    await expect(refused).rejects.toBeInstanceOf(RequestError)
  })

  it('turns a refused action into a refused ack and a drop into ConnectionDown', async () => {
    const { wire, socket } = connect()
    handshake(socket)
    const ack = wire.action('s1', 'run_tasks', { keys: ['a'] })
    const sent = socket.last as { request_id: string }
    socket.feed({ type: 'error', request_id: sent.request_id, reason: 'nope', detail: null })
    await expect(ack).resolves.toEqual({ accepted: false, reason: 'nope' })

    const pending = wire.request('sessions')
    socket.drop()
    await expect(pending).rejects.toBeInstanceOf(ConnectionDown)
    expect(wire.state).toBe('reconnecting')
  })

  it('refuses a send before the hello completes', async () => {
    const { wire, socket } = connect()
    socket.open()
    await expect(wire.request('sessions')).rejects.toBeInstanceOf(ConnectionDown)
  })
})

describe('the lanes', () => {
  it('subscribes once, delivers the snapshot, then the frames in sequence', () => {
    const { wire, socket } = connect()
    handshake(socket)
    const first = listener()
    const second = listener()
    wire.subscribe('s1', first)
    wire.subscribe('s1', second)
    const subscribes = socket.sent.filter((frame) => frame.type === 'subscribe')
    expect(subscribes).toHaveLength(1)
    socket.feed({ type: 'snapshot', session_id: 's1', seq: 10, snapshot })
    expect(first.onSnapshot).toHaveBeenCalledWith(snapshot, false)
    expect(second.onSnapshot).toHaveBeenCalledWith(snapshot, false)
    socket.feed({ type: 'task_status', session_id: 's1', seq: 11, key: 'a', status: 'RUNNING', origin: 'run' })
    expect(first.onFrame).toHaveBeenCalledTimes(1)
  })

  it('drops frames before the snapshot and heals a sequence gap with a resubscribe', () => {
    const { wire, socket } = connect()
    handshake(socket)
    const lane = listener()
    wire.subscribe('s1', lane)
    socket.feed({ type: 'task_status', session_id: 's1', seq: 3, key: 'a', status: 'RUNNING', origin: 'run' })
    expect(lane.onFrame).not.toHaveBeenCalled()
    socket.feed({ type: 'snapshot', session_id: 's1', seq: 3, snapshot })
    socket.feed({ type: 'task_status', session_id: 's1', seq: 4, key: 'a', status: 'RUNNING', origin: 'run' })
    expect(lane.onFrame).toHaveBeenCalledTimes(1)
    // seq 6 skips 5: the lane resubscribes and drops until the fresh snapshot.
    socket.feed({ type: 'task_status', session_id: 's1', seq: 6, key: 'a', status: 'COMPLETED', origin: 'run' })
    expect(lane.onFrame).toHaveBeenCalledTimes(1)
    expect(socket.sent.filter((frame) => frame.type === 'subscribe')).toHaveLength(2)
    socket.feed({ type: 'snapshot', session_id: 's1', seq: 7, snapshot })
    expect(lane.onSnapshot).toHaveBeenLastCalledWith(snapshot, true)
  })

  it('routes a refused subscribe to the lane', () => {
    const { wire, socket } = connect()
    handshake(socket)
    const lane = listener()
    wire.subscribe('gone', lane)
    const sent = socket.last as { request_id: string }
    socket.feed({ type: 'error', request_id: sent.request_id, reason: 'gone has ended', detail: null })
    expect(lane.onRefused).toHaveBeenCalledWith('gone has ended')
  })

  it('fetches a fresh credential when it expires, before the reconnect', async () => {
    const fetchCredential = vi.fn().mockResolvedValue('fresh')
    const { wire, socket } = connect({ credentialUrl: '/ticket', fetchCredential })
    handshake(socket)
    socket.close(CREDENTIAL_EXPIRED, 'credential expired - reconnect with a fresh one')
    await Promise.resolve()
    expect(fetchCredential).toHaveBeenCalledWith('/ticket')
    expect(wire.state).toBe('reconnecting')
    expect(wire.refusal).toBeNull()
  })

  it('reopens at once on an expiry without a credential URL, as a plain drop', () => {
    vi.useFakeTimers()
    const { wire, socket } = connect()
    handshake(socket)
    socket.close(CREDENTIAL_EXPIRED, 'credential expired - reconnect with a fresh one')
    expect(REFUSAL_CODES.has(CREDENTIAL_EXPIRED)).toBe(false)
    expect(wire.state).toBe('reconnecting')
    expect(wire.refusal).toBeNull()
    expect(wire.closeCode).toBe(CREDENTIAL_EXPIRED)
    vi.advanceTimersByTime(0)
    expect(FakeSocket.instances).toHaveLength(2)
    // A failed reopen after the planned close backs off as usual.
    FakeSocket.instances[1]!.drop()
    vi.advanceTimersByTime(RECONNECT_DELAY - 1)
    expect(FakeSocket.instances).toHaveLength(2)
    vi.advanceTimersByTime(1)
    expect(FakeSocket.instances).toHaveLength(3)
    vi.useRealTimers()
  })

  it('resubscribes every lane and task log after a reconnect', () => {
    vi.useFakeTimers()
    const { wire, socket } = connect()
    handshake(socket)
    const lane = listener()
    wire.subscribe('s1', lane)
    socket.feed({ type: 'snapshot', session_id: 's1', seq: 1, snapshot })
    const lines: string[] = []
    wire.subscribeTaskLog('s1', 'a', (line) => lines.push(line)).catch(() => undefined)
    socket.drop()
    vi.runOnlyPendingTimers()
    const next = FakeSocket.instances[1]!
    handshake(next)
    expect(next.sent.map((frame) => frame.type)).toEqual(['hello', 'subscribe', 'task_log_subscribe'])
    next.feed({ type: 'snapshot', session_id: 's1', seq: 40, snapshot })
    expect(lane.onSnapshot).toHaveBeenLastCalledWith(snapshot, true)
    next.feed({ type: 'task_log_batch', session_id: 's1', seq: 41, task_key: 'a', lines: ['x', 'y'] })
    expect(lines).toEqual(['x', 'y'])
    vi.useRealTimers()
  })

  it('answers the task log backlog and unsubscribes when the last handler leaves', async () => {
    const { wire, socket } = connect()
    handshake(socket)
    const handler = vi.fn()
    const backlog = wire.subscribeTaskLog('s1', 'a', handler)
    const sent = socket.last as { request_id: string }
    socket.feed({
      type: 'task_log_backlog',
      request_id: sent.request_id,
      session_id: 's1',
      task_key: 'a',
      lines: ['1'],
    })
    await expect(backlog).resolves.toEqual(['1'])
    wire.unsubscribeTaskLog('s1', 'a', handler)
    expect(socket.sent.map((frame) => frame.type).slice(-2)).toEqual(['task_log_unsubscribe', 'unsubscribe'])
  })
})
