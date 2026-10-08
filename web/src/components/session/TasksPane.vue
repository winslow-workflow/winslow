<script setup lang="ts">
import { computed, ref } from 'vue'

import FilterInput from '@/components/common/FilterInput.vue'
import StatusBadge from '@/components/common/StatusBadge.vue'
import TaskDetailModal from '@/components/session/TaskDetailModal.vue'
import { useStaleGuard } from '@/composables/useStaleGuard'
import { ENDING_MESSAGE, NO_PERMISSION_MESSAGE } from '@/protocol/messages'
import type { TaskInfo, TaskStatusName } from '@/protocol/model'
import { CHECKABLE_STATUSES, NO_ACTION_STATUSES, PASSING_STATUSES, RUNNABLE_STATUSES } from '@/protocol/status'
import { useSessionScope } from '@/stores/inject'
import { useNoticesStore } from '@/stores/notices'

// The batch option toggles, (option name, label), in the two columns of the task bar.
const BATCH_OPTION_COLUMNS: [string, string][][] = [
  [
    ['force_run', 'force run'],
    ['force_success', 'force success'],
  ],
  [
    ['dry_run', 'dry run'],
    ['disable_concurrency', 'no concurrency'],
  ],
]

const props = defineProps<{ locked: boolean }>()
const emit = defineEmits<{ select: [info: TaskInfo] }>()

const store = useSessionScope()
const notices = useNoticesStore()
const guard = useStaleGuard()

const hideCompleted = ref(false)
const hideSkipped = ref(false)
const filterKeys = ref<Set<string> | null>(null)
const previewKeys = ref<Set<string> | null>(null)
const selectedKey = ref<string | null>(null)
const detail = ref<TaskInfo | null>(null)
const detailLoading = ref<string | null>(null)

function statusOf(key: string): TaskStatusName | undefined {
  return store.statuses[key]
}

function visible(task: TaskInfo): boolean {
  const status = statusOf(task.key)
  if (filterKeys.value !== null && !filterKeys.value.has(task.key)) return false
  if (hideCompleted.value && status !== undefined && PASSING_STATUSES.has(status)) return false
  if (hideSkipped.value && status === 'SKIPPED') return false
  return true
}

const rows = computed(() => store.roster.filter(visible))

function rowClass(task: TaskInfo) {
  return {
    'row-selected': task.key === selectedKey.value,
    'filter-match': previewKeys.value !== null && previewKeys.value.has(task.key),
    'filter-dim': previewKeys.value !== null && !previewKeys.value.has(task.key),
    'row-clickable': true,
  }
}

function showsActions(task: TaskInfo): boolean {
  const status = statusOf(task.key)
  return status === undefined || !NO_ACTION_STATUSES.has(status)
}

function canRun(task: TaskInfo): boolean {
  const status = statusOf(task.key)
  return !props.locked && store.allows('run_task') && status !== undefined && RUNNABLE_STATUSES.has(status)
}

function canCheck(task: TaskInfo): boolean {
  const status = statusOf(task.key)
  return !props.locked && store.allows('check_task') && status !== undefined && CHECKABLE_STATUSES.has(status)
}

async function match(query: string): Promise<Set<string> | null> {
  // The filter runs through the port: project filter code lives server-side.
  return new Set(await store.reads.applyFilter(query, 'tasks'))
}

function select(task: TaskInfo) {
  selectedKey.value = task.key
  emit('select', task)
}

async function info(task: TaskInfo) {
  select(task)
  if (props.locked || store.ended) {
    notices.warn(ENDING_MESSAGE)
    return
  }
  const token = guard.next()
  detailLoading.value = task.key
  try {
    // The on-demand capture point: the user asked, so the getters evaluate.
    const full = await store.reads.taskDetail(task.key)
    if (guard.isCurrent(token)) detail.value = full
  } catch (error) {
    notices.report(error)
  } finally {
    if (guard.isCurrent(token)) detailLoading.value = null
  }
}

async function run(task: TaskInfo) {
  select(task)
  await store.runTasks([task.key])
}

async function check(task: TaskInfo) {
  select(task)
  await store.checkTasks([task.key])
}

function visibleKeys(): string[] {
  return rows.value.map((task) => task.key)
}
</script>

<template>
  <div class="d-flex flex-column flex-grow-1 min-h-0">
    <div class="pane-header">
      <FilterInput
        placeholder="filter tasks..."
        :match="match"
        help
        @preview="previewKeys = $event"
        @apply="filterKeys = $event"
      />
      <div class="d-flex flex-wrap gap-3 small">
        <div>
          <BFormCheckbox v-model="hideCompleted" size="sm">hide completed</BFormCheckbox>
          <BFormCheckbox v-model="hideSkipped" size="sm">hide skipped</BFormCheckbox>
        </div>
        <div v-for="(column, columnIndex) in BATCH_OPTION_COLUMNS" :key="columnIndex">
          <BFormCheckbox
            v-for="[name, label] in column"
            :key="name"
            size="sm"
            :model-value="store.batchOptions[name] ?? false"
            @update:model-value="store.setBatchOption(name, $event === true)"
          >
            {{ label }}
          </BFormCheckbox>
        </div>
      </div>
      <div class="ms-auto d-flex gap-1">
        <BButton
          size="sm"
          variant="danger"
          :disabled="locked || !rows.length || !store.allows('run_task')"
          :title="locked ? ENDING_MESSAGE : store.allows('run_task') ? undefined : NO_PERMISSION_MESSAGE"
          @click="store.runBulk('Running', visibleKeys())"
        >
          run all
        </BButton>
        <BButton
          size="sm"
          variant="success"
          :disabled="locked || !rows.length || !store.allows('check_task')"
          :title="locked ? ENDING_MESSAGE : store.allows('check_task') ? undefined : NO_PERMISSION_MESSAGE"
          @click="store.runBulk('Checking', visibleKeys(), true)"
        >
          check all
        </BButton>
      </div>
    </div>
    <div class="pane-body">
      <table class="table table-sm table-hover task-table mb-0">
        <tbody>
          <tr
            v-for="task in rows"
            :key="task.key"
            :class="rowClass(task)"
            tabindex="0"
            @click="select(task)"
            @keydown.enter="select(task)"
          >
            <td class="col-index text-secondary text-end">{{ task.index + 1 }}.</td>
            <td class="col-name">
              <span class="fw-semibold">{{ task.label }}</span>
              <span v-if="task.groups.length" class="text-secondary small ms-2">{{ task.groups.join(', ') }}</span>
            </td>
            <td class="col-status"><StatusBadge :status="statusOf(task.key)" /></td>
            <td class="col-log">
              <div class="inline-log">{{ store.taskLogs[task.key] ?? '' }}</div>
            </td>
            <td class="col-actions-wide text-end" @click.stop>
              <BButton
                v-if="!task.is_noop && showsActions(task)"
                size="sm"
                variant="outline-danger"
                :disabled="!canRun(task)"
                :title="locked ? ENDING_MESSAGE : store.allows('run_task') ? undefined : NO_PERMISSION_MESSAGE"
                @click="run(task)"
              >
                run
              </BButton>
              <BButton
                v-if="showsActions(task)"
                size="sm"
                variant="outline-success"
                class="ms-1"
                :disabled="!canCheck(task)"
                :title="locked ? ENDING_MESSAGE : store.allows('check_task') ? undefined : NO_PERMISSION_MESSAGE"
                @click="check(task)"
              >
                check
              </BButton>
              <BButton
                size="sm"
                variant="outline-secondary"
                class="ms-1"
                :disabled="detailLoading === task.key"
                @click="info(task)"
              >
                info
              </BButton>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="!rows.length" class="text-secondary text-center py-4">No task is visible.</div>
    </div>
    <TaskDetailModal v-if="detail" :info="detail" :live-task-key="detail.key" @hidden="detail = null" />
  </div>
</template>
