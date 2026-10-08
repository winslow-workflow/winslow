<script setup lang="ts">
import { computed, ref } from 'vue'

import ForceEndModal from '@/components/dashboard/ForceEndModal.vue'
import { formatElapsed } from '@/protocol/format'
import type { SessionInfo } from '@/protocol/model'
import { sessionVariant } from '@/protocol/status'
import { useSessionsStore } from '@/stores/sessions'

const MAX_TITLE_LENGTH = 20

const props = defineProps<{ row: SessionInfo }>()

const sessions = useSessionsStore()
const forceEnd = ref(false)

const ended = computed(() => props.row.status === 'ENDED' || props.row.status === 'ERROR')
const title = computed(() => {
  const name = props.row.instance_name
  return name.length < MAX_TITLE_LENGTH ? name : `${name.slice(0, MAX_TITLE_LENGTH)}...`
})
const waiting = computed(() => sessions.endingText[props.row.session_id] ?? null)

async function end() {
  if (props.row.status === 'ENDING') {
    forceEnd.value = true
    return
  }
  await sessions.endSession(props.row.session_id)
}
</script>

<template>
  <div class="session-row">
    <div class="d-flex justify-content-between align-items-center gap-2">
      <div>
        <span class="fw-semibold">{{ title }}</span>
        <span v-if="row.identifier_suffix" class="text-secondary small ms-2">{{ row.identifier_suffix }}</span>
      </div>
      <BBadge :variant="sessionVariant(row.status)">{{ row.status }}</BBadge>
    </div>
    <div class="d-flex justify-content-between align-items-center gap-2 mt-1">
      <div class="summary small">
        <span :class="{ completed: row.task_status_summary.completed }">{{ row.task_status_summary.completed }}</span>
        /
        <span :class="{ problematic: row.task_status_summary.problematic }">
          {{ row.task_status_summary.problematic }}
        </span>
        / {{ row.task_status_summary.total }}
        <span class="text-secondary ms-2">{{ formatElapsed(row.elapsed) }}</span>
        <span v-if="waiting" class="text-warning ms-2">{{ waiting }}</span>
      </div>
      <div class="d-flex gap-1">
        <BButton size="sm" variant="primary" :to="{ name: 'session', params: { id: row.session_id } }">View</BButton>
        <BButton v-if="!ended" size="sm" variant="danger" @click="end">End</BButton>
      </div>
    </div>
    <ForceEndModal v-if="forceEnd" :session-id="row.session_id" @hidden="forceEnd = false" />
  </div>
</template>
