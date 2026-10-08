<script setup lang="ts">
import { computed, onMounted, provide, ref, watch } from 'vue'

import HostStatsPane from '@/components/common/HostStatsPane.vue'
import LogView from '@/components/common/LogView.vue'
import ForceEndModal from '@/components/dashboard/ForceEndModal.vue'
import { NO_PERMISSION_MESSAGE } from '@/protocol/messages'
import CacheOverviewPane from '@/components/session/CacheOverviewPane.vue'
import CachesPane from '@/components/session/CachesPane.vue'
import HistoryPane from '@/components/session/HistoryPane.vue'
import ParamsModal from '@/components/session/ParamsModal.vue'
import TaskOverviewPane from '@/components/session/TaskOverviewPane.vue'
import TasksPane from '@/components/session/TasksPane.vue'
import { messageOf } from '@/protocol/errors'
import { formatElapsed } from '@/protocol/format'
import type { TaskInfo } from '@/protocol/model'
import { sessionVariant } from '@/protocol/status'
import { SESSION_STORE } from '@/stores/inject'
import { useSessionStore } from '@/stores/session'

// batch, task and view come from the record route (see recordLocation).
const props = defineProps<{ id: string; batch?: string; task?: string; view?: string }>()

const store = useSessionStore(props.id)
provide(SESSION_STORE, store)

const selectedTask = ref<TaskInfo | null>(null)
const selectedCacheName = ref<string | null>(null)
const leftTab = ref('tasks')
const rightTab = ref('task')
const showParams = ref(false)
const forceEnd = ref(false)
const failure = ref<string | null>(null)

// The overview follows the fresh card of the selected cache on every refresh.
const selectedCache = computed(() => store.caches.find((card) => card.name === selectedCacheName.value) ?? null)
const title = computed(() => store.info?.display_name || store.workflow || props.id)
const subtitle = computed(() => {
  const info = store.info
  if (!info) return ''
  const parts = info.instance_name !== info.display_name ? [info.instance_name] : []
  if (info.identifier_suffix) parts.push(info.identifier_suffix)
  return parts.join(' | ')
})

onMounted(async () => {
  try {
    if (store.opened) await store.refreshSnapshot()
    else await store.open()
    failure.value = null
  } catch (error) {
    failure.value = messageOf(error)
  }
  if (store.ended || props.batch) leftTab.value = 'history'
})

// A session that ended is read-only history: only the History tab stays.
watch(
  () => store.ended,
  (ended) => {
    if (ended && leftTab.value !== 'history') leftTab.value = 'history'
    if (ended && rightTab.value === 'cache') rightTab.value = 'task'
  },
)

function selectTask(task: TaskInfo) {
  selectedTask.value = task
  rightTab.value = 'task'
}

function selectCache(cacheName: string) {
  selectedCacheName.value = cacheName
  rightTab.value = 'cache'
}

async function end() {
  if (store.ending) {
    forceEnd.value = true
    return
  }
  await store.endSession()
}
</script>

<template>
  <div class="session-layout">
    <div class="session-top">
      <div class="pane">
        <div class="pane-header py-1">
          <BButton size="sm" variant="outline-secondary" title="view dashboard" to="/">&lt;</BButton>
          <span class="fw-semibold">{{ title }}</span>
          <span v-if="subtitle" class="text-secondary small">{{ subtitle }}</span>
          <BBadge :variant="sessionVariant(store.status)">{{ store.status ?? '...' }}</BBadge>
          <span v-if="store.info" class="summary small">
            <span :class="{ completed: store.info.task_status_summary.completed }">
              {{ store.info.task_status_summary.completed }}
            </span>
            /
            <span :class="{ problematic: store.info.task_status_summary.problematic }">
              {{ store.info.task_status_summary.problematic }}
            </span>
            / {{ store.info.task_status_summary.total }}
            <span class="text-secondary ms-2">{{ formatElapsed(store.info.elapsed) }}</span>
          </span>
          <span v-if="failure" class="text-danger small">{{ failure }}</span>
          <span class="ms-auto d-flex gap-1">
            <BButton size="sm" variant="outline-secondary" title="Parameters" @click="showParams = true">⚙</BButton>
            <BButton
              v-if="!store.ended"
              size="sm"
              variant="danger"
              :disabled="!store.allows('end_workflow')"
              :title="store.allows('end_workflow') ? undefined : NO_PERMISSION_MESSAGE"
              @click="end"
            >
              End
            </BButton>
          </span>
        </div>
        <BTabs v-model="leftTab" small lazy>
          <BTab v-if="!store.ended" id="tasks" title="Tasks">
            <TasksPane :locked="store.ending" @select="selectTask" />
          </BTab>
          <BTab id="history" title="History">
            <HistoryPane :active="leftTab === 'history'" @select="selectTask" />
          </BTab>
          <BTab v-if="store.hasCaches && !store.ended" id="caches" title="Caches">
            <CachesPane :active="leftTab === 'caches'" @select="selectCache" />
          </BTab>
        </BTabs>
      </div>
      <div class="pane">
        <BTabs v-model="rightTab" small lazy>
          <BTab id="task" title="Task Overview">
            <TaskOverviewPane :info="selectedTask" :statuses="store.statuses" />
          </BTab>
          <BTab v-if="store.hasCaches && !store.ended" id="cache" title="Cache">
            <CacheOverviewPane :card="selectedCache" />
          </BTab>
        </BTabs>
      </div>
    </div>
    <div class="pane-row">
      <div class="pane">
        <div class="pane-title">Logs</div>
        <LogView :lines="store.sessionLog" :offset="store.sessionLogTrimmed" empty="No session log lines yet." />
      </div>
      <div class="pane">
        <HostStatsPane />
      </div>
    </div>
    <ParamsModal v-if="showParams" :instance-name="store.info?.instance_name ?? title" @hidden="showParams = false" />
    <ForceEndModal v-if="forceEnd" :session-id="id" @hidden="forceEnd = false" />
  </div>
</template>
