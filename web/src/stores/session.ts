// One session: the snapshot, the roster, the statuses mirror, the batches,
// the caches, the logs and the actions, kept live by the session lane of the
// wire. One store per session id (see useSessionStore). An opened lane stays
// subscribed, like an installed TUI screen; the least recently opened lane
// parks once more than MAX_OPEN_LANES are open, and a return re-opens it.

import { defineStore } from 'pinia'
import { computed, markRaw, ref } from 'vue'

import type { LaneFrame } from '@/protocol/frames'
import type {
  Ack,
  BatchInfo,
  BatchOptions,
  BatchOutcome,
  CacheInfo,
  SessionSnapshot,
  SessionStatusName,
  TaskInfo,
  TaskStatusName,
} from '@/protocol/model'
import { PASSING_STATUSES } from '@/protocol/status'
import type { LaneListener, TaskLogHandler } from '@/protocol/wire'
import { useConnectionStore } from './connection'
import { useNoticesStore } from './notices'
import { useSessionsStore } from './sessions'

export const MAX_OPEN_LANES = 10
const SESSION_LOG_CAP = 5000

export function recordKey(batchUuid: string, taskKey: string): string {
  return `${batchUuid}|${taskKey}`
}

// The open lanes, least recent first, with the park action of each.
const openLanes = new Map<string, () => void>()

function noteOpen(sessionId: string, park: () => void) {
  openLanes.delete(sessionId)
  openLanes.set(sessionId, park)
  while (openLanes.size > MAX_OPEN_LANES) {
    const [oldest, parkOldest] = openLanes.entries().next().value as [string, () => void]
    openLanes.delete(oldest)
    parkOldest()
  }
}

export function useSessionStore(sessionId: string) {
  return defineStore(`session/${sessionId}`, () => {
    const connection = useConnectionStore()
    const notices = useNoticesStore()
    const sessions = useSessionsStore()
    const reads = markRaw(connection.session(sessionId))

    const opened = ref(false)
    const status = ref<SessionStatusName | null>(null)
    const workflow = ref('')
    const statuses = ref<Record<string, TaskStatusName>>({})
    const roster = ref<TaskInfo[]>([])
    const batches = ref<BatchInfo[]>([])
    const history = ref<BatchOutcome[]>([])
    const executionStatuses = ref<Record<string, TaskStatusName>>({})
    // The moment of the first status of one record, for the runtime column
    // before the outcome exists.
    const recordStarted = ref<Record<string, number>>({})
    const recordLogs = ref<Record<string, string>>({})
    const taskLogs = ref<Record<string, string>>({})
    const sessionLog = ref<string[]>([])
    const sessionLogTrimmed = ref(0)
    const cacheNames = ref<string[] | null>(null)
    const caches = ref<CacheInfo[]>([])
    const cacheVersion = ref(0)
    const batchOptions = ref<BatchOptions>({})
    const laneRefusal = ref<string | null>(null)
    // {batch uuid: toast verb} of the bulk submits whose batch_created event,
    // which carries the admitted task count, is still to come.
    const pendingBulk = new Map<string, string>()
    let opening: Promise<void> | null = null
    let collectingCaches = false

    const info = computed(() => sessions.byId.get(sessionId) ?? null)
    const ended = computed(() => status.value === 'ENDED' || status.value === 'ERROR')
    // The scope name of the permissions on this session (see SessionInfo).
    const permissionScope = computed(() => info.value?.permission_scope ?? null)
    // False until the session row and the hello are both known, so a control is disabled by default.
    function allows(permission: string): boolean {
      return connection.allows(permissionScope.value, permission)
    }
    const ending = computed(() => status.value === 'ENDING')
    const rosterByKey = computed(() => new Map(roster.value.map((task) => [task.key, task])))
    const historyByUuid = computed(() => new Map(history.value.map((row) => [row.uuid, row])))
    const hasCaches = computed(() => (cacheNames.value?.length ?? 0) > 0)

    function applySnapshot(snapshot: SessionSnapshot, first: boolean) {
      status.value = snapshot.status
      workflow.value = snapshot.workflow
      // The store of an ended session is empty, so the mirror only updates.
      statuses.value = { ...statuses.value, ...snapshot.tasks }
      batches.value = [...snapshot.batches]
      cacheNames.value = snapshot.cache_names
      if (first) {
        sessionLog.value = [...snapshot.session_log_backlog]
        sessionLogTrimmed.value = 0
      }
    }

    function appendSessionLog(lines: string[]) {
      sessionLog.value.push(...lines)
      const excess = sessionLog.value.length - SESSION_LOG_CAP
      if (excess > 0) {
        sessionLog.value.splice(0, excess)
        sessionLogTrimmed.value += excess
      }
    }

    async function refreshHistory() {
      try {
        history.value = await reads.history()
      } catch (error) {
        notices.report(error)
      }
    }

    async function refreshCaches() {
      if (collectingCaches) return
      collectingCaches = true
      try {
        caches.value = await reads.caches()
      } catch (error) {
        notices.report(error)
      } finally {
        collectingCaches = false
      }
    }

    function upsertBatch(batch: BatchInfo) {
      const index = batches.value.findIndex((row) => row.uuid === batch.uuid)
      if (index === -1) batches.value.push(batch)
      else batches.value.splice(index, 1, batch)
    }

    function toastBulk(verb: string, count: number) {
      notices.success(`${verb} ${count} task${count === 1 ? '' : 's'}`)
    }

    function onFrame(frame: LaneFrame) {
      switch (frame.type) {
        case 'task_status':
          statuses.value[frame.key] = frame.status
          if (PASSING_STATUSES.has(frame.status)) taskLogs.value[frame.key] = ''
          return
        case 'execution_status': {
          const key = recordKey(frame.batch_uuid, frame.task_key)
          executionStatuses.value[key] = frame.status
          if (!(key in recordStarted.value)) recordStarted.value[key] = Date.now() / 1000
          return
        }
        case 'batch_created': {
          const known = batches.value.some((row) => row.uuid === frame.info.uuid)
          upsertBatch(frame.info)
          const verb = pendingBulk.get(frame.info.uuid)
          if (verb !== undefined) {
            pendingBulk.delete(frame.info.uuid)
            toastBulk(verb, frame.info.task_count)
          }
          // A healing snapshot replays known batches; a running one refreshes
          // its rows from the record store.
          if (known && frame.info.completed_at === null) void refreshHistory()
          return
        }
        case 'batch_completed':
          upsertBatch(frame.info)
          void refreshHistory()
          return
        case 'log_batch': {
          const last = frame.lines[frame.lines.length - 1]
          if (last === undefined) return
          recordLogs.value[recordKey(frame.batch_uuid, frame.task_key)] = last
          taskLogs.value[frame.task_key] = last
          return
        }
        case 'session_log_batch':
          appendSessionLog(frame.lines)
          return
        case 'session_ended':
          status.value = 'ENDED'
          return
        case 'cache_updated':
          cacheVersion.value += 1
          return
      }
    }

    const listener: LaneListener = {
      onSnapshot(snapshot, healed) {
        applySnapshot(snapshot, !healed)
        if (healed) {
          void refreshHistory()
          if (caches.value.length) void refreshCaches()
        }
      },
      onFrame,
      onRefused(reason) {
        // The session resolves no more on the server: it ended.
        laneRefusal.value = reason
        status.value = 'ENDED'
      },
    }

    // Drop the lane. The store keeps its state; the next open reads afresh.
    function park() {
      if (!opened.value) return
      connection.unsubscribeLane(sessionId, listener)
      opened.value = false
    }

    async function load() {
      await connection.whenConnected()
      const snapshot = await reads.snapshot()
      applySnapshot(snapshot, true)
      if (!ended.value) {
        const [rosterRows, options] = await Promise.all([reads.roster(), reads.batchOptions()])
        roster.value = rosterRows
        batchOptions.value = { ...options }
        connection.subscribeLane(sessionId, listener)
        noteOpen(sessionId, park)
      }
      opened.value = true
      await refreshHistory()
    }

    // Read the session and subscribe its lane, once. It waits for the wire,
    // so a deep link before the hello opens as soon as the hello lands.
    function open(): Promise<void> {
      if (opened.value) return Promise.resolve()
      if (opening === null) {
        opening = load().finally(() => {
          opening = null
        })
      }
      return opening
    }

    async function refreshSnapshot() {
      try {
        applySnapshot(await reads.snapshot(), false)
      } catch (error) {
        notices.report(error)
      }
    }

    function subscribeTaskLog(taskKey: string, handler: TaskLogHandler) {
      return reads.subscribeTaskLog(taskKey, handler)
    }

    function unsubscribeTaskLog(taskKey: string, handler: TaskLogHandler) {
      reads.unsubscribeTaskLog(taskKey, handler)
    }

    // The toggle is view state of this client alone; the next submit carries it.
    function setBatchOption(name: string, value: boolean) {
      batchOptions.value = { ...batchOptions.value, [name]: value }
    }

    // --- actions ------------------------------------------------------------

    async function submit(pending: Promise<Ack>): Promise<Ack> {
      const ack = await pending
      if (!ack.accepted) notices.warn(ack.reason ?? 'the action was refused')
      return ack
    }

    function runTasks(keys: string[]) {
      return submit(reads.runTasks(keys, { ...batchOptions.value }))
    }

    function checkTasks(keys: string[]) {
      return submit(reads.checkTasks(keys, { ...batchOptions.value }))
    }

    async function runBulk(verb: string, keys: string[], check = false) {
      const ack = await (check ? checkTasks(keys) : runTasks(keys))
      if (!ack.accepted || !ack.batch_uuid) return ack
      // The batch_created event carries the admitted count. It can arrive
      // before the ack: then the batch is known and the toast shows at once.
      const batch = batches.value.find((row) => row.uuid === ack.batch_uuid)
      if (batch) toastBulk(verb, batch.task_count)
      else pendingBulk.set(ack.batch_uuid, verb)
      return ack
    }

    function stopBatch(batchUuid: string) {
      return submit(reads.stopBatch(batchUuid))
    }

    function endSession(force = false) {
      return submit(reads.endSession(force))
    }

    function loadCacheEntries(entries: [string, string][]) {
      return submit(reads.loadCacheEntries(entries))
    }

    function clearCacheEntries(entries: [string, string][]) {
      return submit(reads.clearCacheEntries(entries))
    }

    return {
      sessionId,
      reads,
      opened,
      status,
      workflow,
      statuses,
      roster,
      rosterByKey,
      batches,
      history,
      historyByUuid,
      executionStatuses,
      recordStarted,
      recordLogs,
      taskLogs,
      sessionLog,
      sessionLogTrimmed,
      cacheNames,
      hasCaches,
      caches,
      cacheVersion,
      batchOptions,
      laneRefusal,
      info,
      ended,
      ending,
      permissionScope,
      allows,
      open,
      park,
      refreshSnapshot,
      refreshHistory,
      refreshCaches,
      subscribeTaskLog,
      unsubscribeTaskLog,
      setBatchOption,
      runTasks,
      checkTasks,
      runBulk,
      stopBatch,
      endSession,
      loadCacheEntries,
      clearCacheEntries,
    }
  })()
}

export type SessionStore = ReturnType<typeof useSessionStore>
