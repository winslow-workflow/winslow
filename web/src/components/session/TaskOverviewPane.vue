<script setup lang="ts">
import { computed } from 'vue'

import InfoRows from '@/components/common/InfoRows.vue'
import StatusBadge from '@/components/common/StatusBadge.vue'
import { formatVerification, safeRepr } from '@/protocol/format'
import type { TaskInfo, TaskRef, TaskStatusName } from '@/protocol/model'

const props = defineProps<{ info: TaskInfo | null; statuses: Record<string, TaskStatusName> }>()

// (tag label, TaskInfo flag): the pill row under the task name.
const TAGS = [
  ['premier', 'is_premier'],
  ['terminal', 'is_terminal'],
  ['noop', 'is_noop'],
] as const

const tags = computed(() => {
  const info = props.info
  if (!info) return []
  return TAGS.filter(([, flag]) => info[flag]).map(([name]) => name)
})

const rows = computed<[string, string][]>(() => {
  const info = props.info
  if (!info) return []
  const result: [string, string][] = [['task', info.label]]
  if (info.groups.length) result.push(['groups', info.groups.join(', ')])
  result.push(['class', info.task_class])
  const verified = formatVerification(info.checked_at, info.effective_ttl)
  if (verified) result.push(['last verified', verified])
  if (info.parameters && Object.keys(info.parameters).length) {
    result.push([
      'parameters',
      Object.entries(info.parameters)
        .map(([key, value]) => `${key}: ${safeRepr(value)}`)
        .join('\n'),
    ])
  }
  return result
})

const groups = computed<[string, TaskRef[]][]>(() => {
  const info = props.info
  if (!info) return []
  return [
    ['premier dependencies', info.premier_dependencies],
    ['dependencies', info.dependencies],
    ['terminal dependencies', info.terminal_dependencies],
  ]
})
</script>

<template>
  <div v-if="!info" class="d-flex align-items-center justify-content-center h-100 text-secondary">
    Select a task to see the details.
  </div>
  <div v-else class="pane-body">
    <div class="pane-title">task information</div>
    <InfoRows :rows="rows">
      <template #first>
        <div v-if="tags.length" class="d-flex gap-1 mt-1">
          <span v-for="tag in tags" :key="tag" class="tag" :class="tag">{{ tag }}</span>
        </div>
      </template>
    </InfoRows>
    <template v-for="[title, refs] in groups" :key="title">
      <template v-if="refs.length">
        <div class="pane-title border-top">{{ title }}</div>
        <div class="px-3 py-2">
          <div v-for="ref in refs" :key="ref.key" class="dependency-row">
            <StatusBadge :status="statuses[ref.key]" icon-only />
            <span>{{ ref.label }}</span>
          </div>
        </div>
      </template>
    </template>
  </div>
</template>
