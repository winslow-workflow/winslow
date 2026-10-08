<script setup lang="ts">
// The confirmation before a force end: the active batches and their rosters,
// from the live store of the session. The session can drain by itself while
// the modal is open; the modal closes once no batch is active.

import { computed, onMounted, ref, watch } from 'vue'

import { plural } from '@/protocol/format'
import { ACTIVE_BATCH_STATUSES } from '@/protocol/status'
import { useNoticesStore } from '@/stores/notices'
import { useSessionStore } from '@/stores/session'

const props = defineProps<{ sessionId: string }>()
const emit = defineEmits<{ hidden: [] }>()

const notices = useNoticesStore()
const store = useSessionStore(props.sessionId)
const show = ref(false)

const active = computed(() => store.batches.filter((batch) => ACTIVE_BATCH_STATUSES.has(batch.status)))
const title = computed(
  () => `Force end - ${plural(active.value.length, 'active batch', 'active batches')} still running`,
)

function roster(batchUuid: string, fallback: Record<string, string>): [string, string][] {
  const outcome = store.historyByUuid.get(batchUuid)
  const keys = outcome ? Object.keys(outcome.tasks) : Object.keys(fallback)
  return keys.map((key) => [key, store.rosterByKey.get(key)?.label ?? fallback[key] ?? key])
}

onMounted(async () => {
  try {
    await store.open()
    show.value = true
  } catch (error) {
    notices.report(error)
    emit('hidden')
  }
})

watch(
  () => store.ended || (show.value && active.value.length === 0),
  (drained) => {
    if (drained) show.value = false
  },
)

async function confirm() {
  await store.endSession(true)
}
</script>

<template>
  <BModal
    v-model="show"
    :title="title"
    scrollable
    ok-title="Force end"
    ok-variant="danger"
    cancel-variant="outline-secondary"
    @ok="confirm"
    @hidden="emit('hidden')"
  >
    <div v-for="batch in active" :key="batch.uuid" class="mb-2">
      <div class="fw-semibold">{{ batch.action }} · {{ batch.task_count }} task(s)</div>
      <div v-for="[key, label] in roster(batch.uuid, batch.tasks)" :key="key" class="ps-3 small">{{ label }}</div>
    </div>
  </BModal>
</template>
