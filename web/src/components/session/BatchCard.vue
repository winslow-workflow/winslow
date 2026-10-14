<script setup lang="ts">
import { computed } from 'vue'

import StatusBadge from '@/components/common/StatusBadge.vue'
import { formatClock, formatDuration, shortUuid } from '@/protocol/format'
import type { BatchInfo, TaskOutcome, TaskStatusName } from '@/protocol/model'
import {
  ACTIVE_BATCH_STATUSES,
  PASSING_STATUSES,
  PROBLEMATIC_STATUSES,
  batchVariant,
  statusLabel,
} from '@/protocol/status'
import { NO_PERMISSION_MESSAGE } from '@/protocol/messages'
import { useSessionScope } from '@/stores/inject'
import { recordKey } from '@/stores/session'

// (batch option name, pill label, css class) of the header pills.
const FLAG_PILLS: [string, string, string][] = [
  ['dry_run', 'dry run', 'dry-run'],
  ['force_run', 'force run', 'force-run'],
  ['force_success', 'force success', 'force-success'],
  ['disable_concurrency', 'disable concurrency', 'disable-concurrency'],
]

export interface RecordRowView {
  key: string
  index: string
  label: string
  status: TaskStatusName | undefined
  record: TaskOutcome | undefined
  log: string
}

const props = defineProps<{
  batch: BatchInfo
  now: number
  filterKeys: Set<string> | null
  statusFilter: string | null
  hideCompleted: boolean
  preview: Set<string> | null
  selectedKey: string | null
}>()

const emit = defineEmits<{
  select: [key: string]
  info: [key: string]
  details: []
  stop: []
}>()

const store = useSessionScope()

const outcome = computed(() => store.historyByUuid.get(props.batch.uuid))

// The user with the credential that named them: `can (token 9f86d081)`.
const submitter = computed(() => {
  const { user, credential, credential_id } = props.batch
  if (!user) return null
  return credential_id ? `${user} (${credential} ${credential_id})` : user
})

function statusOf(key: string): TaskStatusName | undefined {
  return store.executionStatuses[recordKey(props.batch.uuid, key)] ?? outcome.value?.tasks[key]?.status
}

// The runtime reads the clock only for a record without an outcome, so the
// tick repaints the running rows alone.
function runtime(row: RecordRowView): string {
  if (row.record?.duration != null) return formatDuration(row.record.duration)
  if (row.record?.started_at != null) return formatDuration(props.now - row.record.started_at)
  const since = store.recordStarted[recordKey(props.batch.uuid, row.key)]
  if (since !== undefined) return formatDuration(props.now - since)
  return ''
}

const rows = computed<RecordRowView[]>(() =>
  Object.keys(props.batch.tasks).map((key) => {
    const info = store.rosterByKey.get(key)
    const record = outcome.value?.tasks[key]
    return {
      key,
      index: info ? `${info.index + 1}.` : '',
      label: info?.label ?? props.batch.tasks[key] ?? key,
      status: statusOf(key),
      record,
      log: store.recordLogs[recordKey(props.batch.uuid, key)] ?? record?.last_log ?? '',
    }
  }),
)

function visible(row: RecordRowView): boolean {
  if (props.filterKeys !== null && !props.filterKeys.has(row.key)) return false
  if (props.statusFilter !== null && row.status !== props.statusFilter) return false
  if (props.hideCompleted && row.status !== undefined && PASSING_STATUSES.has(row.status)) return false
  return true
}

const shownRows = computed(() => rows.value.filter(visible))

const summary = computed(() => {
  const statuses = rows.value.map((row) => row.status)
  const completed = statuses.filter((status) => status !== undefined && PASSING_STATUSES.has(status)).length
  const problematic = statuses.filter((status) => status !== undefined && PROBLEMATIC_STATUSES.has(status)).length
  return { completed, problematic, total: statuses.length }
})

const pills = computed(() =>
  FLAG_PILLS.filter(([name]) => props.batch.options?.[name]).map(([, label, css]) => ({ label, css })),
)

// A stop needs the permission that could have submitted the batch.
const stoppable = computed(
  () =>
    ACTIVE_BATCH_STATUSES.has(props.batch.status) &&
    store.allows(props.batch.action === 'CHECK' ? 'check_task' : 'run_task'),
)

function rowClass(row: RecordRowView) {
  return {
    'row-selected': row.key === props.selectedKey,
    'filter-match': props.preview !== null && props.preview.has(row.key),
    'filter-dim': props.preview !== null && !props.preview.has(row.key),
    'row-clickable': true,
  }
}
</script>

<template>
  <div v-if="shownRows.length" class="batch-card">
    <div class="batch-header">
      <span class="fw-semibold">{{ batch.action }}</span>
      <span class="text-secondary">{{ formatClock(batch.created_at) }}</span>
      <span class="mono text-secondary">{{ shortUuid(batch.uuid) }}</span>
      <span v-if="submitter" class="text-secondary">{{ submitter }}</span>
      <span class="summary">
        <span :class="{ completed: summary.completed }">{{ summary.completed }}</span> /
        <span :class="{ problematic: summary.problematic }">{{ summary.problematic }}</span> / {{ summary.total }}
      </span>
      <BBadge :variant="batchVariant(batch.status)">{{ statusLabel(batch.status) }}</BBadge>
      <span v-for="pill in pills" :key="pill.css" class="tag" :class="pill.css">{{ pill.label }}</span>
      <span v-if="batch.error" class="text-danger small">{{ batch.error }}</span>
      <span class="ms-auto d-flex gap-1">
        <BButton size="sm" variant="outline-secondary" @click="emit('details')">info</BButton>
        <BButton
          v-if="batch.task_count > 1"
          size="sm"
          variant="outline-warning"
          :disabled="!stoppable"
          :title="stoppable || !ACTIVE_BATCH_STATUSES.has(batch.status) ? undefined : NO_PERMISSION_MESSAGE"
          @click="emit('stop')"
        >
          stop
        </BButton>
      </span>
    </div>
    <table class="table table-sm table-hover task-table mb-0">
      <tbody>
        <tr
          v-for="row in shownRows"
          :key="row.key"
          :class="rowClass(row)"
          tabindex="0"
          @click="emit('select', row.key)"
          @keydown.enter="emit('select', row.key)"
        >
          <td class="col-index text-secondary text-end">{{ row.index }}</td>
          <td class="col-name fw-semibold">{{ row.label }}</td>
          <td class="col-runtime mono text-secondary">{{ runtime(row) }}</td>
          <td class="col-status"><StatusBadge :status="row.status" /></td>
          <td class="col-log">
            <div class="inline-log">{{ row.log }}</div>
          </td>
          <td class="col-actions text-end" @click.stop>
            <BButton size="sm" variant="outline-secondary" @click="emit('info', row.key)">info</BButton>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
