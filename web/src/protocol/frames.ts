// The wire frames, one type per class of src/winslow/protocol/frames.py plus
// one per lane of src/winslow/protocol/lanes.py. FRAME_TYPES lists every wire
// type name; tests/test_serve_web.py compares it with the Python registry.

import type { Ack, BatchInfo, SessionSnapshot, TaskStatusName } from './model'

export const FRAME_TYPES = [
  'hello',
  'subscribe',
  'unsubscribe',
  'task_log_subscribe',
  'task_log_unsubscribe',
  'request',
  'action',
  'hello_ok',
  'hello_error',
  'error',
  'result',
  'ack',
  'task_log_backlog',
  'snapshot',
  'task_status',
  'execution_status',
  'batch_created',
  'batch_completed',
  'session_ended',
  'cache_updated',
  'log_batch',
  'session_log_batch',
  'task_log_batch',
] as const

export type FrameType = (typeof FRAME_TYPES)[number]

// --- client to server -----------------------------------------------------------

export interface HelloFrame {
  type: 'hello'
  token?: string | null
}

export interface SubscribeFrame {
  type: 'subscribe' | 'unsubscribe'
  session_id: string
  request_id?: string
}

export interface TaskLogSubscribeFrame {
  type: 'task_log_subscribe' | 'task_log_unsubscribe'
  session_id: string
  task_key: string
  request_id?: string
}

export interface RequestFrame {
  type: 'request'
  kind: string
  request_id: string
  session_id?: string
  [field: string]: unknown
}

export interface ActionFrame {
  type: 'action'
  session_id?: string
  action: string
  request_id: string
  fields: Record<string, unknown>
}

export type ClientFrame = HelloFrame | SubscribeFrame | TaskLogSubscribeFrame | RequestFrame | ActionFrame

// --- server to client: the handshake and the control replies -------------------------

export interface HelloOkFrame {
  type: 'hello_ok'
  user: string
  // The service and the environment of the serving process, and the header
  // color of the environment, a CSS color.
  service: string
  env: string
  env_color: string
  // The PermissionContext of the connection: the expanded permission set,
  // `<scope name>:<permission>` or `*:<permission>`, the credential kind, and
  // the expiry of the credential.
  scopes: string[]
  credential: string
  expires_at: number | null
  // The id of the credential the server records, the token id or the ticket id.
  credential_id?: string | null
}

export interface HelloErrorFrame {
  type: 'hello_error'
  reason: string
  detail: string | null
}

export interface ErrorFrame {
  type: 'error'
  reason: string
  request_id: string | null
  detail: string | null
}

export interface ResultFrame {
  type: 'result'
  request_id: string | null
  kind: string
  result: unknown
}

export interface AckFrame {
  type: 'ack'
  request_id: string | null
  ack: Ack
}

export interface TaskLogBacklogFrame {
  type: 'task_log_backlog'
  request_id: string | null
  session_id: string
  task_key: string
  lines: string[]
}

export type ReplyFrame = ErrorFrame | ResultFrame | AckFrame | TaskLogBacklogFrame

// --- server to client: the session subscription -------------------------------------

export interface SnapshotFrame {
  type: 'snapshot'
  session_id: string
  seq: number
  snapshot: SessionSnapshot
}

export interface TaskStatusFrame {
  type: 'task_status'
  session_id: string
  seq: number
  key: string
  status: TaskStatusName
  origin: 'run' | 'seed'
}

export interface ExecutionStatusFrame {
  type: 'execution_status'
  session_id: string
  seq: number
  task_key: string
  status: TaskStatusName
  batch_uuid: string
  origin: 'run' | 'seed'
}

export interface BatchFrame {
  type: 'batch_created' | 'batch_completed'
  session_id: string
  seq: number
  info: BatchInfo
}

export interface SessionEndedFrame {
  type: 'session_ended'
  session_id: string
  seq: number
}

export interface CacheUpdatedFrame {
  type: 'cache_updated'
  session_id: string
  seq: number
  cache_name: string
}

export interface LogBatchFrame {
  type: 'log_batch'
  session_id: string
  seq: number
  task_key: string
  batch_uuid: string
  lines: string[]
}

export interface SessionLogBatchFrame {
  type: 'session_log_batch'
  session_id: string
  seq: number
  lines: string[]
}

export interface TaskLogBatchFrame {
  type: 'task_log_batch'
  session_id: string
  seq: number
  task_key: string
  lines: string[]
}

export type LaneFrame =
  | TaskStatusFrame
  | ExecutionStatusFrame
  | BatchFrame
  | SessionEndedFrame
  | CacheUpdatedFrame
  | LogBatchFrame
  | SessionLogBatchFrame
  | TaskLogBatchFrame

export const LANE_TYPES: ReadonlySet<string> = new Set([
  'task_status',
  'execution_status',
  'batch_created',
  'batch_completed',
  'session_ended',
  'cache_updated',
  'log_batch',
  'session_log_batch',
  'task_log_batch',
])

export type ServerFrame = HelloOkFrame | HelloErrorFrame | ReplyFrame | SnapshotFrame | LaneFrame

export function isLaneFrame(frame: ServerFrame): frame is LaneFrame {
  return LANE_TYPES.has(frame.type)
}
