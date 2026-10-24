// The value shapes of the session port, one interface per dataclass of
// src/winslow/model.py. Field names match the wire exactly.

// The members of TaskStatus in declaration order (see src/winslow/task/status.py).
export const TASK_STATUSES = [
  'INITIALIZED',
  'CHECKING_ELIGIBILITY',
  'SKIPPED',
  'READY_TO_PROCESS',
  'READY_TO_RUN',
  'CHECKING_RUNNABILITY',
  'RUNNING',
  'RUN_FINISHED',
  'FAILED',
  'ERROR',
  'BLOCKED',
  'CHECKING_DEPENDENCIES',
  'CHECKING_COMPLETION',
  'ACTION_REQUIRED',
  'COMPLETED',
  'COMPLETED_WITH_ERROR',
  'FORCE_SUCCESS',
  'COMPLETED_PREVIOUSLY',
  'STALE',
  'ABORTED',
  'WAITING_FOR_RELEASE',
] as const

export type TaskStatusName = (typeof TASK_STATUSES)[number]

export type SessionStatusName = 'ACTIVE' | 'ENDING' | 'ENDED' | 'ERROR'

export type BatchStatusName = 'QUEUED' | 'RUNNING' | 'FINISHED' | 'STOPPED' | 'ERRORED' | 'INTERRUPTED'

export type EntryStateName = 'cold' | 'warm' | 'stale' | 'computing' | 'errored'

export interface TaskStatusSummary {
  completed: number
  problematic: number
  total: number
}

export interface SessionInfo {
  session_id: string
  workflow: string
  status: SessionStatusName
  display_name: string
  instance_name: string
  identifier_suffix: string
  started_at: number
  elapsed: number
  task_status_summary: TaskStatusSummary
  root_dir: string | null
  user: string | null
  permission_scope: string
}

export interface BatchInfo {
  uuid: string
  action: string
  status: BatchStatusName
  task_count: number
  tasks: Record<string, string>
  options: Record<string, unknown> | null
  created_at: number
  started_at: number | null
  completed_at: number | null
  error: string | null
  user: string | null
  credential: string | null
  credential_id: string | null
}

export interface SessionSnapshot {
  session_id: string
  workflow: string
  status: SessionStatusName
  tasks: Record<string, TaskStatusName>
  session_log_backlog: string[]
  batches: BatchInfo[]
  cache_names: string[] | null
}

export interface TaskRef {
  key: string
  label: string
  is_premier: boolean
  is_terminal: boolean
  is_noop: boolean
}

// (title, columns, rows) of one attribute section of a full TaskInfo.
export type AttributeSection = [string, string[], string[][]]

export interface SourceNode {
  name: string
  module: string
  source: string
  path: string | null
  children: SourceNode[]
  label: string
  location: string
}

export interface TaskInfo {
  key: string
  label: string
  name: string
  is_premier: boolean
  is_terminal: boolean
  is_noop: boolean
  task_class: string
  index: number
  groups: string[]
  parameters: Record<string, unknown> | null
  dependencies: TaskRef[]
  premier_dependencies: TaskRef[]
  terminal_dependencies: TaskRef[]
  checked_at: number | null
  effective_ttl: number | null
  attributes: AttributeSection[] | null
  docs: [string, string][] | null
  source: SourceNode | null
  transients: string[] | null
}

export interface TaskOutcome {
  status: TaskStatusName
  started_at: number | null
  duration: number | null
  last_log: string
}

export interface BatchOutcome {
  uuid: string
  action: string
  status: BatchStatusName
  task_count: number
  created_at: number
  completed_at: number | null
  options: Record<string, unknown> | null
  tasks: Record<string, TaskOutcome>
  user: string | null
  credential: string | null
  credential_id: string | null
}

export interface PhaseInfo {
  phase: string
  started_at: number
  completed_at: number | null
  duration: number | null
}

export interface CacheReadSnapshot {
  scope: string
  cache_name: string
  entry_name: string
  written_at: number
  rendered: string
  summary: string | null
  encoding: 'text' | 'json'
}

export interface RecordDetail {
  info: TaskInfo
  phases: PhaseInfo[]
  transient_snapshots: Record<string, Record<string, string>>
  cache_snapshots: Record<string, CacheReadSnapshot[]>
}

export interface CacheEntryError {
  origin: 'delete' | 'load'
  tier: string | null
  message: string
  at: number
  traceback: string | null
}

export interface CacheEntryInfo {
  scope: string
  cache_name: string
  entry_name: string
  state: EntryStateName
  written_at: number | null
  ttl: number | null
  eager: boolean
  depends_on: string[]
  storage: string
  error: CacheEntryError | null
  written_by: string | null
}

export interface CacheInfo {
  name: string
  scope: string
  docstring: string | null
  storage: string
  entries: string[]
  info: CacheEntryInfo[]
  values: Record<string, string | null>
  error: string | null
}

export interface CacheValueView {
  cache_name: string
  entry_name: string
  state: EntryStateName
  encoding: 'text' | 'json' | null
  rendered: string | null
  summary: string | null
  written_at: number | null
  error: CacheEntryError | null
}

export interface SessionParams {
  settings: Record<string, unknown>
  workflow_config: Record<string, unknown>
}

export interface ManifestInfo {
  session_id: string
  workflow_class: string
  orchestrator_overrides: Record<string, unknown> | null
  workflow_values: Record<string, unknown> | null
  // The scope name of the workflow, or null for a class the server does not collect.
  permission_scope: string | null
}

// The resources of the host that runs the sessions: memory in bytes, the cpu
// load since the previous sample.
export interface HostStats {
  hostname: string
  user: string
  cpu_percent: number
  memory_used: number
  memory_total: number
  memory_percent: number
}

export interface OptionInfo {
  name: string
  help: string | null
  default: string | null
  initial: string | null
  required: boolean
  choices: string[] | null
  multiselect: boolean
  type: string | null
  identifier: boolean
  depends_on: string[]
  action: string | null
  const: unknown
  initial_selection: string[] | null
}

export interface WorkflowDescriptor {
  workflow: string
  options: OptionInfo[]
  auto_init: boolean
  permission_scope: string
  // The listed workflow whose directory holds this one, null at the top level.
  parent: string | null
}

export interface Descriptors {
  workflows: WorkflowDescriptor[]
  overrides: OptionInfo[]
}

export interface Ack {
  accepted: boolean
  reason: string | null
  batch_uuid?: string | null
  // A session start: the SessionInfo of the live session, or the traceback of a refusal.
  session?: SessionInfo | null
  detail?: string | null
}

// The batch option toggles a client sends with every submit (see SessionClient.run_tasks).
export type BatchOptions = Record<string, boolean>
