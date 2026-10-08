<script setup lang="ts">
// Every tab renders from the TaskInfo value and the record payload. A live
// modal streams the task log of the port; a history modal shows the stored
// lines and the phase snapshots of the record.

import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import LogView from '@/components/common/LogView.vue'
import MarkdownBlock from '@/components/common/MarkdownBlock.vue'
import CacheValueModal from '@/components/session/CacheValueModal.vue'
import SourceView from '@/components/session/SourceView.vue'
import { safeRepr } from '@/protocol/format'
import type { CacheReadSnapshot, TaskInfo } from '@/protocol/model'
import { useSessionScope } from '@/stores/inject'
import { useNoticesStore } from '@/stores/notices'

// The declaration order of ExecutionPhase is the run order.
const PHASES = [
  'checkability',
  'check',
  'pre_run_checkability',
  'pre_run_check',
  'runnability',
  'run',
  'post_run_checkability',
  'post_run_check',
]
const NOT_MATERIALIZED = 'Not materialized'

const props = defineProps<{
  info: TaskInfo
  // The task key of a live modal: it streams the task log of the session.
  liveTaskKey?: string
  logs?: string[]
  transientSnapshots?: Record<string, Record<string, string>>
  cacheSnapshots?: Record<string, CacheReadSnapshot[]>
}>()
const emit = defineEmits<{ hidden: [] }>()

const store = useSessionScope()
const notices = useNoticesStore()
const show = ref(true)
const lines = ref<string[]>([...(props.logs ?? [])])
const snapshot = ref<CacheReadSnapshot | null>(null)

function onLine(line: string) {
  lines.value.push(line)
}

onMounted(async () => {
  if (props.liveTaskKey === undefined) return
  try {
    const backlog = await store.subscribeTaskLog(props.liveTaskKey, onLine)
    lines.value.unshift(...backlog)
  } catch (error) {
    notices.report(error)
  }
})

onBeforeUnmount(() => {
  if (props.liveTaskKey !== undefined) store.unsubscribeTaskLog(props.liveTaskKey, onLine)
})

const sections = computed(() => (props.info.attributes ?? []).filter(([, , rows]) => rows.length))

const transientRows = computed(() => {
  const snapshots = props.transientSnapshots ?? {}
  const rows: [string, string, string][] = []
  for (const name of props.info.transients ?? []) {
    for (const phase of PHASES) {
      const value = snapshots[phase]?.[name]
      if (value !== undefined && value !== NOT_MATERIALIZED) rows.push([name, phase.replace(/_/g, ' '), value])
    }
  }
  return rows
})

interface CacheReadRow {
  snapshot: CacheReadSnapshot
  entry: string
  phase: string
  value: string
}

const cacheReads = computed<CacheReadRow[]>(() => {
  const snapshots = props.cacheSnapshots ?? {}
  return PHASES.flatMap((phase) =>
    (snapshots[phase] ?? []).map((snap) => ({
      snapshot: snap,
      entry: `${snap.cache_name}.${snap.entry_name}`,
      phase: phase.replace(/_/g, ' '),
      value: readValue(snap),
    })),
  )
})

function readValue(snap: CacheReadSnapshot): string {
  if (snap.summary === null) return safeRepr(snap.rendered)
  if (snap.rendered === '') return snap.summary
  return `${safeRepr(snap.rendered)} (${snap.summary})`
}

// A JSON snapshot opens as a tree; a text one has nothing more to show.
function openSnapshot(row: CacheReadRow) {
  if (row.snapshot.encoding !== 'json') return
  snapshot.value = row.snapshot
}
</script>

<template>
  <BModal
    v-model="show"
    :title="info.label"
    dialog-class="detail-dialog"
    scrollable
    ok-only
    ok-title="Close"
    @hidden="emit('hidden')"
  >
    <BTabs small>
      <BTab title="Logs" active>
        <div class="detail-log">
          <LogView :lines="lines" />
        </div>
      </BTab>
      <BTab title="Attributes">
        <div v-for="[title, columns, rows] in sections" :key="title" class="mb-3">
          <div class="fw-semibold mb-1">{{ title }}</div>
          <table class="table table-sm table-striped mb-0">
            <thead>
              <tr>
                <th v-for="column in columns" :key="column">{{ column }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, index) in rows" :key="index">
                <td v-for="(cell, cellIndex) in row" :key="cellIndex" class="mono small">{{ cell }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="info.transients?.length" class="mb-3">
          <div class="fw-semibold mb-1">Transient Properties</div>
          <table v-if="transientRows.length" class="table table-sm table-striped mb-0">
            <thead>
              <tr>
                <th>Attribute</th>
                <th>Phase</th>
                <th>Value</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="[name, phase, value] in transientRows" :key="`${name}-${phase}`">
                <td class="mono small">{{ name }}</td>
                <td class="small">{{ phase }}</td>
                <td class="mono small">{{ value }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="small text-secondary">
            {{ info.transients.join(', ') }}<br />
            These are scoped to a batch execution. Check the execution history to see their snapshots.
          </p>
        </div>
        <div v-if="cacheReads.length" class="mb-3">
          <div class="fw-semibold mb-1">Cache Reads</div>
          <table class="table table-sm table-striped table-hover mb-0">
            <thead>
              <tr>
                <th>Entry</th>
                <th>Scope</th>
                <th>Phase</th>
                <th>Value</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="row in cacheReads"
                :key="`${row.entry}-${row.phase}`"
                :class="{ 'row-clickable': row.snapshot.encoding === 'json' }"
                :tabindex="row.snapshot.encoding === 'json' ? 0 : undefined"
                @click="openSnapshot(row)"
                @keydown.enter="openSnapshot(row)"
              >
                <td class="mono small">{{ row.entry }}</td>
                <td class="small">{{ row.snapshot.scope }}</td>
                <td class="small">{{ row.phase }}</td>
                <td class="mono small">{{ row.value }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="!sections.length && !info.transients?.length && !cacheReads.length" class="text-secondary">
          No attributes captured for this task.
        </div>
      </BTab>
      <BTab v-if="info.docs?.length" title="Documentation">
        <BTabs small pills class="mt-2">
          <BTab v-for="[title, text] in info.docs" :key="title" :title="title">
            <MarkdownBlock :text="text" class="pt-2" />
          </BTab>
        </BTabs>
      </BTab>
      <BTab v-if="info.source" title="Code">
        <SourceView :root="info.source" class="pt-2" />
      </BTab>
    </BTabs>
    <CacheValueModal
      v-if="snapshot"
      :cache-name="snapshot.cache_name"
      :entry-name="snapshot.entry_name"
      :snapshot="snapshot"
      @hidden="snapshot = null"
    />
  </BModal>
</template>
