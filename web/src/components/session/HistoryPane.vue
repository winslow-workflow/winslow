<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import FilterInput from '@/components/common/FilterInput.vue'
import BatchCard from '@/components/session/BatchCard.vue'
import BatchDetailModal from '@/components/session/BatchDetailModal.vue'
import TaskDetailModal from '@/components/session/TaskDetailModal.vue'
import { useNow } from '@/composables/useNow'
import { useStaleGuard } from '@/composables/useStaleGuard'
import type { BatchInfo, RecordDetail, TaskInfo } from '@/protocol/model'
import { ACTIVE_BATCH_STATUSES, TASK_STATUSES, statusLabel } from '@/protocol/status'
import { recordLocation, sessionLocation } from '@/router'
import { useConnectionStore } from '@/stores/connection'
import { useSessionScope } from '@/stores/inject'
import { useNoticesStore } from '@/stores/notices'

const STATUS_ALL = 'all'

// One execution record: the batch uuid and the task identity key.
interface RecordRef {
  batch: string
  key: string
}

// The info modal of one record: the served detail and the log tail.
interface OpenInfo {
  record: RecordRef
  detail: RecordDetail
  logs: string[]
}

const props = defineProps<{ active: boolean }>()
const emit = defineEmits<{ select: [info: TaskInfo] }>()

const store = useSessionScope()
const connection = useConnectionStore()
const notices = useNoticesStore()
const guard = useStaleGuard()
const route = useRoute()
const router = useRouter()

const hideCompleted = ref(false)
const statusFilter = ref<string>(STATUS_ALL)
const filterKeys = ref<Set<string> | null>(null)
const previewKeys = ref<Set<string> | null>(null)
const selected = ref<RecordRef | null>(null)
const details = ref<BatchInfo | null>(null)
const openInfo = ref<OpenInfo | null>(null)

const statusOptions = [
  { value: STATUS_ALL, text: 'all statuses' },
  ...TASK_STATUSES.map((status) => ({ value: status, text: statusLabel(status) })),
]

const cards = computed(() => [...store.batches].reverse())
const hasActiveBatch = computed(() => store.batches.some((batch) => ACTIVE_BATCH_STATUSES.has(batch.status)))
// The runtime clock ticks while a batch runs on the visible tab.
const now = useNow(1000, () => props.active && hasActiveBatch.value)

// The record the URL names, and whether it names the info modal too.
const target = computed<{ record: RecordRef | null; info: boolean }>(() => {
  const { batch, task, view } = route.params
  if (typeof batch !== 'string' || typeof task !== 'string') return { record: null, info: false }
  return { record: { batch, key: task }, info: view === 'info' }
})

function same(a: RecordRef | null, b: RecordRef | null): boolean {
  return a?.batch === b?.batch && a?.key === b?.key
}

function selectedKeyOf(batch: BatchInfo): string | null {
  return selected.value?.batch === batch.uuid ? selected.value.key : null
}

async function match(query: string): Promise<Set<string> | null> {
  // The history scope also serves an ended session (see Workflow.filter_keys).
  return new Set(await store.reads.applyFilter(query, 'history'))
}

async function open(record: RecordRef): Promise<RecordDetail | null> {
  try {
    return await store.reads.recordDetail(record.batch, record.key)
  } catch (error) {
    notices.report(error)
    return null
  }
}

// A record the server refuses leaves the URL: the notice names the reason.
function unlink() {
  selected.value = null
  openInfo.value = null
  void router.replace(sessionLocation(store.sessionId))
}

async function select(record: RecordRef) {
  selected.value = record
  // A URL that names this record keeps its info tail.
  const keepInfo = target.value.info && same(target.value.record, record)
  void router.replace(recordLocation(store.sessionId, record.batch, record.key, keepInfo))
  const token = guard.next()
  // The record info, not the roster stub: the sweep replaced the stub there.
  const opened = await open(record)
  if (!guard.isCurrent(token)) return
  if (opened === null) unlink()
  else emit('select', opened.info)
}

async function info(record: RecordRef) {
  const token = guard.next()
  const opened = await open(record)
  if (!guard.isCurrent(token)) return
  if (opened === null) {
    unlink()
    return
  }
  let logs: string[] = []
  try {
    logs = await store.reads.logTail(record.batch, record.key)
  } catch (error) {
    notices.report(error)
  }
  if (!guard.isCurrent(token)) return
  openInfo.value = { record, detail: opened, logs }
  void router.replace(recordLocation(store.sessionId, record.batch, record.key, true))
}

function closeInfo() {
  const record = openInfo.value?.record ?? null
  openInfo.value = null
  if (record !== null) void router.replace(recordLocation(store.sessionId, record.batch, record.key))
}

// A URL change selects its record and opens its modal: a shared link, or the
// back button. A replace from this pane names the state the pane holds
// already, so the watcher changes nothing.
watch(
  target,
  async ({ record, info: wantInfo }) => {
    if (record === null) return
    await connection.whenConnected()
    if (!same(selected.value, record)) await select(record)
    if (wantInfo && !same(openInfo.value?.record ?? null, record)) await info(record)
    if (!wantInfo && openInfo.value !== null) openInfo.value = null
  },
  { immediate: true },
)
</script>

<template>
  <div class="d-flex flex-column h-100">
    <div class="pane-header">
      <FilterInput
        placeholder="search records..."
        :match="match"
        help
        @preview="previewKeys = $event"
        @apply="filterKeys = $event"
      />
      <BFormSelect v-model="statusFilter" :options="statusOptions" size="sm" class="w-auto" />
      <BFormCheckbox v-model="hideCompleted" size="sm">hide completed</BFormCheckbox>
    </div>
    <div class="pane-body">
      <BatchCard
        v-for="batch in cards"
        :key="batch.uuid"
        :batch="batch"
        :now="now"
        :filter-keys="filterKeys"
        :status-filter="statusFilter === STATUS_ALL ? null : statusFilter"
        :hide-completed="hideCompleted"
        :preview="previewKeys"
        :selected-key="selectedKeyOf(batch)"
        @select="select({ batch: batch.uuid, key: $event })"
        @info="info({ batch: batch.uuid, key: $event })"
        @details="details = batch"
        @stop="store.stopBatch(batch.uuid)"
      />
      <div v-if="!cards.length" class="text-secondary text-center py-4">No batches yet.</div>
    </div>
    <BatchDetailModal v-if="details" :batch="details" @hidden="details = null" />
    <TaskDetailModal
      v-if="openInfo"
      :info="openInfo.detail.info"
      :logs="openInfo.logs"
      :transient-snapshots="openInfo.detail.transient_snapshots"
      :cache-snapshots="openInfo.detail.cache_snapshots"
      @hidden="closeInfo"
    />
  </div>
</template>
