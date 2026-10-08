// The dashboard scope: the session rows, the open manifests, the descriptors
// and the create and restore flows. A poll keeps the rows fresh while the
// wire is connected, like the session adoption tick of the TUI dashboard.

import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'

import { messageOf } from '@/protocol/errors'
import { plural } from '@/protocol/format'
import type { Descriptors, ManifestInfo, SessionInfo } from '@/protocol/model'
import { ACTIVE_BATCH_STATUSES } from '@/protocol/status'
import { RequestError } from '@/protocol/wire'
import { useConnectionStore } from './connection'
import { useNoticesStore } from './notices'

export const POLL_INTERVAL = 2000

export interface PendingSession {
  id: number
  workflow: string
}

export interface FailedSession {
  id: number
  workflow: string
  error: string
  detail: string | null
  at: number
}

export const useSessionsStore = defineStore('sessions', () => {
  const connection = useConnectionStore()
  const notices = useNoticesStore()

  const rows = ref<SessionInfo[]>([])
  const manifests = ref<ManifestInfo[]>([])
  const descriptors = ref<Descriptors | null>(null)
  const pending = ref<PendingSession[]>([])
  const failed = ref<FailedSession[]>([])
  // The "waiting for N batches" text per ending session (see poll).
  const endingText = ref<Record<string, string>>({})
  const loaded = ref(false)
  let ids = 0
  let timer: ReturnType<typeof setInterval> | null = null

  const live = computed(() => rows.value.filter((row) => row.status === 'ACTIVE' || row.status === 'ENDING'))
  const ended = computed(() => rows.value.filter((row) => row.status === 'ENDED' || row.status === 'ERROR'))
  const byId = computed(() => new Map(rows.value.map((row) => [row.session_id, row])))

  async function loadDescriptors() {
    try {
      descriptors.value = await connection.app.descriptors()
      notices.record('info', `${descriptors.value.workflows.length} workflow classes loaded.`)
    } catch (error) {
      notices.report(error)
    }
  }

  async function describeEnding(row: SessionInfo): Promise<[string, string] | null> {
    try {
      const snapshot = await connection.session(row.session_id).snapshot()
      const active = snapshot.batches.filter((batch) => ACTIVE_BATCH_STATUSES.has(batch.status))
      const tasks = active.reduce((sum, batch) => sum + batch.task_count, 0)
      return [row.session_id, `waiting for ${plural(active.length, 'batch', 'batches')} (${plural(tasks, 'task')})...`]
    } catch {
      return null
    }
  }

  async function poll(quiet = true) {
    if (!connection.connected) return
    try {
      const [sessionRows, manifestRows] = await Promise.all([connection.app.sessions(), connection.app.manifests()])
      rows.value = sessionRows
      manifests.value = manifestRows
      loaded.value = true
      const ending = sessionRows.filter((row) => row.status === 'ENDING')
      const texts = await Promise.all(ending.map(describeEnding))
      endingText.value = Object.fromEntries(texts.filter((entry) => entry !== null))
    } catch (error) {
      notices.report(error, quiet)
    }
  }

  function startPolling() {
    if (timer !== null) return
    void poll()
    timer = setInterval(() => void poll(), POLL_INTERVAL)
  }

  function stopPolling() {
    if (timer === null) return
    clearInterval(timer)
    timer = null
  }

  // The dashboard scope follows the wire: the descriptors load once, the poll
  // runs while the socket is up.
  watch(
    () => connection.connected,
    (connected) => {
      if (!connected) {
        stopPolling()
        return
      }
      if (descriptors.value === null) void loadDescriptors()
      startPolling()
    },
    { immediate: true },
  )

  // One session through the port: create answers a SessionInfo. A failure
  // lands in the history as a failed row with the traceback.
  async function start(workflow: string, create: () => Promise<SessionInfo>) {
    const entry: PendingSession = { id: ++ids, workflow }
    pending.value.push(entry)
    notices.record('info', `Initializing workflow: ${workflow}`)
    try {
      const info = await create()
      rows.value = [...rows.value.filter((row) => row.session_id !== info.session_id), info]
      return info
    } catch (error) {
      const detail = error instanceof RequestError ? error.detail : null
      const message = messageOf(error)
      failed.value.push({ id: ++ids, workflow, error: message, detail, at: Date.now() / 1000 })
      notices.error(`Could not start '${workflow}': ${message}`, 'Workflow init failed', detail)
      return null
    } finally {
      pending.value = pending.value.filter((item) => item.id !== entry.id)
    }
  }

  function createSession(workflow: string, overrides: Record<string, unknown>, values: Record<string, unknown>) {
    return start(workflow, () => connection.app.createSession(workflow, overrides, values))
  }

  function restoreSession(manifest: ManifestInfo) {
    manifests.value = manifests.value.filter((row) => row.session_id !== manifest.session_id)
    return start(manifest.workflow_class, () => connection.app.restoreSession(manifest.session_id))
  }

  async function restoreAll() {
    const targets = [...manifests.value]
    for (const manifest of targets) await restoreSession(manifest)
  }

  async function endSession(sessionId: string, force = false) {
    const ack = await connection.session(sessionId).endSession(force)
    if (!ack.accepted) notices.warn(ack.reason ?? 'the end was refused')
    return ack
  }

  function dismissFailed(id: number) {
    failed.value = failed.value.filter((item) => item.id !== id)
  }

  return {
    rows,
    manifests,
    descriptors,
    pending,
    failed,
    endingText,
    loaded,
    live,
    ended,
    byId,
    loadDescriptors,
    poll,
    createSession,
    restoreSession,
    restoreAll,
    endSession,
    dismissFailed,
  }
})
