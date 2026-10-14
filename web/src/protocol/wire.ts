// The browser side of the wire: one websocket to a serve process, the hello,
// the request table, the session lanes, and the reconnect with backoff. It
// mirrors Wire and RemoteSessionClient of src/winslow/client/websocket.py.

import {
  isLaneFrame,
  type ActionFrame,
  type HelloFrame,
  type LaneFrame,
  type ReplyFrame,
  type RequestFrame,
  type ServerFrame,
  type SnapshotFrame,
  type SubscribeFrame,
  type TaskLogSubscribeFrame,
} from './frames'
import type { Ack, SessionSnapshot } from './model'

export const OPEN_TIMEOUT = 5000
export const REQUEST_TIMEOUT = 60000
export const RECONNECT_DELAY = 500
export const RECONNECT_DELAY_MAX = 5000

// The close codes of a refused hello (see src/winslow/serve/app.py).
export const MALFORMED_HELLO = 4400
export const CREDENTIAL_REFUSED = 4401
export const ORIGIN_REFUSED = 4403
// The server closed the socket because the credential expired (see winslow.serve.app.until_expiry).
export const CREDENTIAL_EXPIRED = 4402
// The closes the server plans: the client reopens at once and shows no warning.
export const PLANNED_CLOSES: ReadonlySet<number> = new Set([CREDENTIAL_EXPIRED])
export const HELLO_TIMEOUT = 4408
export const REFUSAL_CODES: ReadonlySet<number> = new Set([
  MALFORMED_HELLO,
  CREDENTIAL_REFUSED,
  ORIGIN_REFUSED,
  HELLO_TIMEOUT,
])

export const CONNECTION_DOWN =
  'the connection to the serve process is down - the client reconnects in the background; retry once it is back.'

export class RequestError extends Error {
  detail: string | null

  constructor(reason: string, detail: string | null = null) {
    super(reason)
    this.name = 'RequestError'
    this.detail = detail
  }
}

export class ConnectionDown extends Error {
  constructor(message = CONNECTION_DOWN) {
    super(message)
    this.name = 'ConnectionDown'
  }
}

export type WireState = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'refused' | 'closed'

export interface Refusal {
  code: number | null
  reason: string
  detail: string | null
}

export interface LaneListener {
  onSnapshot(snapshot: SessionSnapshot, healed: boolean): void
  onFrame(frame: LaneFrame): void
  onRefused(reason: string): void
}

export type TaskLogHandler = (line: string) => void

// The minimal socket surface the wire uses, so a test can hand in a fake.
export interface SocketLike {
  readyState: number
  send(data: string): void
  close(code?: number, reason?: string): void
  onopen: ((event: unknown) => void) | null
  onmessage: ((event: { data: unknown }) => void) | null
  onclose: ((event: { code: number; reason: string }) => void) | null
  onerror: ((event: unknown) => void) | null
}

export type SocketFactory = (url: string) => SocketLike

export interface WireOptions {
  url: string
  credential?: string | null
  credentialUrl?: string | null
  socketFactory?: SocketFactory
  fetchCredential?: (url: string) => Promise<string>
}

type LaneState = 'idle' | 'subscribing' | 'healing' | 'streaming'

interface Pending {
  resolve(frame: ReplyFrame): void
  reject(error: Error): void
  timer: ReturnType<typeof setTimeout>
}

class Lane {
  state: LaneState = 'idle'
  lastSeq: number | null = null
  subscribeId: string | null = null
  listeners = new Set<LaneListener>()
  taskLogs = new Map<string, Set<TaskLogHandler>>()

  constructor(public readonly sessionId: string) {}

  get idle(): boolean {
    return this.listeners.size === 0 && this.taskLogs.size === 0
  }
}

const OPEN = 1

async function fetchCredentialText(url: string): Promise<string> {
  const response = await fetch(url, { credentials: 'include' })
  if (!response.ok) {
    throw new Error(`${url} answered ${response.status}`)
  }
  return (await response.text()).trim()
}

export class Wire {
  readonly url: string
  credential: string | null
  credentialUrl: string | null
  state: WireState = 'idle'
  user: string | null = null
  service: string | null = null
  env: string | null = null
  envColor: string | null = null
  // The expanded permission set of the live connection. Empty before the
  // hello and after a drop, so a page disables its controls by default (see allows).
  scopes: string[] = []
  // The kind and the id of the credential behind the connection (see HelloOkFrame).
  credentialKind: string | null = null
  credentialId: string | null = null
  expiresAt: number | null = null
  refusal: Refusal | null = null
  // The close code of the last drop, for the notice a page shows (see PLANNED_CLOSES).
  closeCode: number | null = null

  private readonly socketFactory: SocketFactory
  private readonly fetchCredential: (url: string) => Promise<string>
  private socket: SocketLike | null = null
  private generation = 0
  private ids = 0
  private delay = RECONNECT_DELAY
  private closing = false
  private everConnected = false
  private helloPending = false
  private helloRefusal: Refusal | null = null
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private readonly pending = new Map<string, Pending>()
  private readonly subscribes = new Map<string, Lane>()
  private readonly lanes = new Map<string, Lane>()
  private readonly stateListeners = new Set<(wire: Wire) => void>()

  constructor(options: WireOptions) {
    this.url = options.url
    this.credential = options.credential ?? null
    this.credentialUrl = options.credentialUrl ?? null
    this.socketFactory = options.socketFactory ?? ((url) => new WebSocket(url) as unknown as SocketLike)
    this.fetchCredential = options.fetchCredential ?? fetchCredentialText
  }

  // --- lifecycle ---------------------------------------------------------------

  connect(): void {
    this.closing = false
    this.open()
  }

  close(): void {
    this.closing = true
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    this.socket?.close(1000, 'the client is closed')
    this.failPending(new ConnectionDown('the client is closed.'))
    this.setState('closed')
  }

  // A refused hello stops the reconnect; retry opens a fresh socket.
  retry(): void {
    if (this.state !== 'refused') return
    this.refusal = null
    this.delay = RECONNECT_DELAY
    this.open()
  }

  // Membership alone: the server expands the implications (see winslow.access.permissions).
  allows(scopeName: string | null, permission: string): boolean {
    if (scopeName === null) return false
    return this.scopes.includes(`${scopeName}:${permission}`) || this.scopes.includes(`*:${permission}`)
  }

  onState(listener: (wire: Wire) => void): () => void {
    this.stateListeners.add(listener)
    return () => this.stateListeners.delete(listener)
  }

  private setState(state: WireState): void {
    this.state = state
    for (const listener of this.stateListeners) listener(this)
  }

  private open(): void {
    this.setState(this.everConnected ? 'reconnecting' : 'connecting')
    const generation = ++this.generation
    const socket = this.socketFactory(this.url)
    this.socket = socket
    this.helloPending = true
    this.helloRefusal = null
    const openTimer = setTimeout(() => {
      if (generation === this.generation && this.helloPending) {
        socket.close(4000, 'no answer within the open timeout')
      }
    }, OPEN_TIMEOUT)
    socket.onopen = () => {
      const hello: HelloFrame = { type: 'hello', token: this.credential }
      socket.send(JSON.stringify(hello))
    }
    socket.onmessage = (event) => {
      if (generation !== this.generation) return
      let frame: ServerFrame
      try {
        frame = JSON.parse(String(event.data)) as ServerFrame
      } catch {
        console.error('a frame is dropped - not JSON', event.data)
        return
      }
      this.receive(frame)
    }
    socket.onerror = () => {
      // The close event that follows carries the outcome.
    }
    socket.onclose = (event) => {
      clearTimeout(openTimer)
      if (generation !== this.generation) return
      this.onClose(event.code, event.reason)
    }
  }

  private onClose(code: number, reason: string): void {
    this.socket = null
    this.closeCode = code
    if (PLANNED_CLOSES.has(code)) this.delay = 0
    this.scopes = []
    this.credentialKind = null
    this.credentialId = null
    this.expiresAt = null
    this.failPending(new ConnectionDown())
    if (this.closing) {
      this.setState('closed')
      return
    }
    if (code === CREDENTIAL_EXPIRED && this.credentialUrl) {
      // The credential is spent: fetch a fresh one before the reconnect, instead
      // of one refused hello first.
      void this.refreshCredentialAndReconnect()
      return
    }
    if (REFUSAL_CODES.has(code)) {
      // The hello_error carries the reason and the detail, the close the code.
      this.refusal = { ...(this.helloRefusal ?? { reason, detail: null }), code }
      if (code === CREDENTIAL_REFUSED && this.credentialUrl) {
        void this.refreshCredentialAndReconnect()
        return
      }
      this.setState('refused')
      return
    }
    this.scheduleReconnect()
  }

  private async refreshCredentialAndReconnect(): Promise<void> {
    try {
      this.credential = await this.fetchCredential(this.credentialUrl as string)
    } catch (error) {
      this.refusal = {
        code: CREDENTIAL_REFUSED,
        reason: `the credential refresh failed - ${(error as Error).message}`,
        detail: null,
      }
      this.setState('refused')
      return
    }
    this.scheduleReconnect()
  }

  private scheduleReconnect(): void {
    this.setState('reconnecting')
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      if (!this.closing) this.open()
    }, this.delay)
    // The floor keeps a planned close's zero from doubling into a zero: the
    // first reopen is immediate, a failed one backs off as usual.
    this.delay = Math.min(Math.max(this.delay * 2, RECONNECT_DELAY), RECONNECT_DELAY_MAX)
  }

  private failPending(error: Error): void {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer)
      pending.reject(error)
    }
    this.pending.clear()
    this.subscribes.clear()
  }

  // --- the outgoing side -------------------------------------------------------

  private nextId(): string {
    return `w-${++this.ids}`
  }

  private send(frame: object): void {
    const socket = this.socket
    if (socket === null || socket.readyState !== OPEN || this.helloPending) {
      throw new ConnectionDown()
    }
    socket.send(JSON.stringify(frame))
  }

  private sendQuietly(frame: object): void {
    try {
      this.send(frame)
    } catch {
      // The reconnect replays the subscriptions.
    }
  }

  private exchange(requestId: string, frame: object): Promise<ReplyFrame> {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(requestId)
        reject(
          new Error(
            `no answer from the serve process within ${REQUEST_TIMEOUT / 1000}s - ` +
              'the server may be overloaded or unreachable.',
          ),
        )
      }, REQUEST_TIMEOUT)
      this.pending.set(requestId, { resolve, reject, timer })
      try {
        this.send(frame)
      } catch (error) {
        clearTimeout(timer)
        this.pending.delete(requestId)
        reject(error as Error)
      }
    })
  }

  // One port read. A served refusal rejects with RequestError, an outage with ConnectionDown.
  async request(kind: string, fields: Record<string, unknown> = {}, sessionId?: string): Promise<unknown> {
    const requestId = this.nextId()
    const frame: RequestFrame = { type: 'request', kind, request_id: requestId, ...fields }
    if (sessionId !== undefined) frame.session_id = sessionId
    const reply = await this.exchange(requestId, frame)
    if (reply.type === 'error') throw new RequestError(reply.reason, reply.detail)
    if (reply.type !== 'result') throw new Error(`a ${reply.type} frame answered the ${kind} request`)
    return reply.result
  }

  // One action. A transport failure answers a refused ack, like the handler.
  // An app action, a session start, names no session.
  async action(sessionId: string | null, action: string, fields: Record<string, unknown>): Promise<Ack> {
    const requestId = this.nextId()
    const frame: ActionFrame = { type: 'action', action, request_id: requestId, fields }
    if (sessionId !== null) frame.session_id = sessionId
    let reply: ReplyFrame
    try {
      reply = await this.exchange(requestId, frame)
    } catch (error) {
      return { accepted: false, reason: (error as Error).message }
    }
    if (reply.type === 'error') return { accepted: false, reason: reply.reason }
    if (reply.type !== 'ack') return { accepted: false, reason: `a ${reply.type} frame answered the action` }
    return reply.ack
  }

  // --- the lanes ------------------------------------------------------------------

  private lane(sessionId: string): Lane {
    let lane = this.lanes.get(sessionId)
    if (lane === undefined) {
      lane = new Lane(sessionId)
      this.lanes.set(sessionId, lane)
    }
    return lane
  }

  subscribe(sessionId: string, listener: LaneListener): void {
    const lane = this.lane(sessionId)
    lane.listeners.add(listener)
    this.ensureSubscribed(lane)
  }

  unsubscribe(sessionId: string, listener: LaneListener): void {
    const lane = this.lanes.get(sessionId)
    if (lane === undefined) return
    lane.listeners.delete(listener)
    this.releaseLane(lane)
  }

  async subscribeTaskLog(sessionId: string, taskKey: string, handler: TaskLogHandler): Promise<string[]> {
    const lane = this.lane(sessionId)
    let handlers = lane.taskLogs.get(taskKey)
    if (handlers === undefined) {
      handlers = new Set()
      lane.taskLogs.set(taskKey, handlers)
    }
    handlers.add(handler)
    // The live lines ride the session subscription, so the lane subscribes first.
    this.ensureSubscribed(lane)
    const requestId = this.nextId()
    const frame: TaskLogSubscribeFrame = {
      type: 'task_log_subscribe',
      session_id: sessionId,
      task_key: taskKey,
      request_id: requestId,
    }
    const reply = await this.exchange(requestId, frame)
    if (reply.type === 'error') throw new RequestError(reply.reason, reply.detail)
    if (reply.type !== 'task_log_backlog') throw new Error(`a ${reply.type} frame answered the task log subscribe`)
    return reply.lines
  }

  unsubscribeTaskLog(sessionId: string, taskKey: string, handler: TaskLogHandler): void {
    const lane = this.lanes.get(sessionId)
    if (lane === undefined) return
    const handlers = lane.taskLogs.get(taskKey)
    if (handlers === undefined) return
    handlers.delete(handler)
    if (handlers.size === 0) {
      lane.taskLogs.delete(taskKey)
      const frame: TaskLogSubscribeFrame = {
        type: 'task_log_unsubscribe',
        session_id: sessionId,
        task_key: taskKey,
      }
      this.sendQuietly(frame)
    }
    this.releaseLane(lane)
  }

  private ensureSubscribed(lane: Lane): void {
    if (lane.state !== 'idle') return
    lane.state = 'subscribing'
    this.sendSubscribe(lane)
  }

  private sendSubscribe(lane: Lane): void {
    const requestId = this.nextId()
    lane.subscribeId = requestId
    this.subscribes.set(requestId, lane)
    const frame: SubscribeFrame = { type: 'subscribe', session_id: lane.sessionId, request_id: requestId }
    try {
      this.send(frame)
    } catch {
      // The reconnect resubscribes every lane that is not idle.
      this.subscribes.delete(requestId)
    }
  }

  private releaseLane(lane: Lane): void {
    if (!lane.idle) return
    if (lane.state !== 'idle') {
      const frame: SubscribeFrame = { type: 'unsubscribe', session_id: lane.sessionId }
      this.sendQuietly(frame)
    }
    this.lanes.delete(lane.sessionId)
  }

  private resubscribeAll(): void {
    for (const lane of this.lanes.values()) {
      if (lane.state === 'idle' && lane.taskLogs.size === 0) continue
      lane.state = 'healing'
      lane.lastSeq = null
      this.sendSubscribe(lane)
      for (const taskKey of lane.taskLogs.keys()) {
        const frame: TaskLogSubscribeFrame = {
          type: 'task_log_subscribe',
          session_id: lane.sessionId,
          task_key: taskKey,
        }
        this.sendQuietly(frame)
      }
    }
  }

  // --- the incoming side -----------------------------------------------------------

  private receive(frame: ServerFrame): void {
    if (this.helloPending) {
      this.receiveHello(frame)
      return
    }
    switch (frame.type) {
      case 'error': {
        if (this.settle(frame)) return
        const lane = frame.request_id === null ? undefined : this.subscribes.get(frame.request_id)
        if (lane !== undefined && frame.request_id !== null) {
          this.subscribes.delete(frame.request_id)
          this.onSubscribeRefused(lane, frame.reason)
        } else {
          console.warn(`the serve process refused a frame - ${frame.reason}`)
        }
        return
      }
      case 'result':
      case 'ack':
      case 'task_log_backlog':
        this.settle(frame)
        return
      case 'snapshot':
        this.onSnapshot(frame)
        return
      default:
        if (isLaneFrame(frame)) {
          this.onLaneFrame(frame)
        } else {
          console.warn(`a ${(frame as { type: string }).type} frame has no destination - dropped`)
        }
    }
  }

  private receiveHello(frame: ServerFrame): void {
    if (frame.type === 'hello_ok') {
      this.helloPending = false
      this.everConnected = true
      this.user = frame.user
      this.service = frame.service
      this.env = frame.env
      this.envColor = frame.env_color
      this.scopes = frame.scopes
      this.credentialKind = frame.credential
      this.credentialId = frame.credential_id ?? null
      this.expiresAt = frame.expires_at
      this.refusal = null
      this.delay = RECONNECT_DELAY
      this.setState('connected')
      this.resubscribeAll()
      return
    }
    if (frame.type === 'hello_error') {
      // The close that follows carries the code (see onClose).
      this.helloRefusal = { code: null, reason: frame.reason, detail: frame.detail }
      return
    }
    console.warn(`a ${frame.type} frame arrived before hello_ok - dropped`)
  }

  private settle(frame: ReplyFrame): boolean {
    if (frame.request_id === null) return false
    const pending = this.pending.get(frame.request_id)
    if (pending === undefined) return false
    this.pending.delete(frame.request_id)
    clearTimeout(pending.timer)
    pending.resolve(frame)
    return true
  }

  private onSubscribeRefused(lane: Lane, reason: string): void {
    // The session resolves no more there, so no snapshot answers.
    lane.state = 'idle'
    for (const listener of lane.listeners) listener.onRefused(reason)
  }

  private onSnapshot(frame: SnapshotFrame): void {
    const lane = this.lanes.get(frame.session_id)
    if (lane === undefined) return
    if (lane.subscribeId !== null) this.subscribes.delete(lane.subscribeId)
    lane.lastSeq = frame.seq
    const healed = lane.state === 'healing'
    lane.state = 'streaming'
    for (const listener of lane.listeners) listener.onSnapshot(frame.snapshot, healed)
  }

  private onLaneFrame(frame: LaneFrame): void {
    const lane = this.lanes.get(frame.session_id)
    if (lane === undefined) return
    if (lane.state === 'subscribing' || lane.state === 'healing') return
    if (lane.lastSeq !== null && frame.seq !== lane.lastSeq + 1) {
      // A gap: the resubscribe resets the queue and resends the snapshot.
      lane.state = 'healing'
      this.sendSubscribe(lane)
      return
    }
    lane.lastSeq = frame.seq
    if (frame.type === 'task_log_batch') {
      const handlers = lane.taskLogs.get(frame.task_key)
      if (handlers === undefined) return
      for (const line of frame.lines) {
        for (const handler of handlers) handler(line)
      }
      return
    }
    for (const listener of lane.listeners) listener.onFrame(frame)
  }
}
