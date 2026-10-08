// The status vocabulary of the UI: the sets of src/winslow/task/status.py, the
// icons of src/winslow/ui/icons.py, and the badge variant per status.

import { TASK_STATUSES, type BatchStatusName, type EntryStateName, type TaskStatusName } from './model'

export { TASK_STATUSES }

export const RUNNABLE_STATUSES: ReadonlySet<TaskStatusName> = new Set<TaskStatusName>([
  'INITIALIZED',
  'READY_TO_PROCESS',
  'FAILED',
  'ERROR',
  'BLOCKED',
  'ABORTED',
  'RUN_FINISHED',
  'STALE',
])

export const PASSING_STATUSES: ReadonlySet<TaskStatusName> = new Set<TaskStatusName>([
  'COMPLETED',
  'COMPLETED_WITH_ERROR',
  'COMPLETED_PREVIOUSLY',
  'FORCE_SUCCESS',
])

export const CHECKABLE_STATUSES: ReadonlySet<TaskStatusName> = new Set<TaskStatusName>([
  ...RUNNABLE_STATUSES,
  'ACTION_REQUIRED',
  ...PASSING_STATUSES,
])

export const PROBLEMATIC_STATUSES: ReadonlySet<TaskStatusName> = new Set<TaskStatusName>([
  'FAILED',
  'ERROR',
  'BLOCKED',
  'ABORTED',
  'STALE',
])

export const ACTIVE_STATUSES: ReadonlySet<TaskStatusName> = new Set<TaskStatusName>([
  'CHECKING_ELIGIBILITY',
  'CHECKING_RUNNABILITY',
  'RUNNING',
  'CHECKING_DEPENDENCIES',
  'CHECKING_COMPLETION',
])

// The task statuses the UI hides the action buttons for.
export const NO_ACTION_STATUSES: ReadonlySet<TaskStatusName> = new Set<TaskStatusName>(['SKIPPED'])

// The batch statuses a stop request reaches.
export const ACTIVE_BATCH_STATUSES: ReadonlySet<BatchStatusName> = new Set<BatchStatusName>(['QUEUED', 'RUNNING'])

const TASK_ICONS: Record<TaskStatusName, string> = {
  INITIALIZED: '⚪',
  CHECKING_ELIGIBILITY: '🟠',
  SKIPPED: '🔵',
  READY_TO_PROCESS: '🟡',
  READY_TO_RUN: '🟡',
  CHECKING_RUNNABILITY: '🟠',
  RUNNING: '🟠',
  RUN_FINISHED: '🟠',
  FAILED: '🔴',
  ERROR: '❗',
  BLOCKED: '🚫',
  CHECKING_DEPENDENCIES: '🟠',
  CHECKING_COMPLETION: '🟠',
  ACTION_REQUIRED: '✋',
  COMPLETED: '🟢',
  COMPLETED_WITH_ERROR: '🟣',
  COMPLETED_PREVIOUSLY: '🟢',
  FORCE_SUCCESS: '🟢',
  STALE: '🔴',
  ABORTED: '⛔',
  WAITING_FOR_RELEASE: '⏳',
}

export function taskIcon(status: TaskStatusName | null | undefined): string {
  return status ? (TASK_ICONS[status] ?? '❓') : '❓'
}

export function statusLabel(status: string | null | undefined): string {
  return status ? status.replace(/_/g, ' ') : ''
}

export type Variant = 'primary' | 'secondary' | 'success' | 'danger' | 'warning' | 'info' | 'light' | 'dark'

export function taskVariant(status: TaskStatusName | null | undefined): Variant {
  if (!status) return 'secondary'
  if (PASSING_STATUSES.has(status)) return 'success'
  if (PROBLEMATIC_STATUSES.has(status)) return 'danger'
  if (ACTIVE_STATUSES.has(status) || status === 'RUN_FINISHED' || status === 'READY_TO_RUN') return 'warning'
  if (status === 'SKIPPED') return 'info'
  if (status === 'ACTION_REQUIRED') return 'warning'
  if (status === 'WAITING_FOR_RELEASE') return 'light'
  return 'secondary'
}

export function batchVariant(status: BatchStatusName | string): Variant {
  switch (status) {
    case 'FINISHED':
      return 'success'
    case 'RUNNING':
      return 'warning'
    case 'QUEUED':
      return 'info'
    case 'STOPPED':
    case 'INTERRUPTED':
      return 'secondary'
    case 'ERRORED':
      return 'danger'
    default:
      return 'secondary'
  }
}

export function entryVariant(state: EntryStateName | null | undefined): Variant {
  switch (state) {
    case 'warm':
      return 'success'
    case 'stale':
      return 'warning'
    case 'computing':
      return 'info'
    case 'errored':
      return 'danger'
    default:
      return 'secondary'
  }
}

export function sessionVariant(status: string | null | undefined): Variant {
  switch (status) {
    case 'ACTIVE':
      return 'success'
    case 'ENDING':
      return 'warning'
    case 'ERROR':
      return 'danger'
    default:
      return 'secondary'
  }
}
