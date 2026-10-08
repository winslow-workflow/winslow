<script setup lang="ts">
import { ref } from 'vue'

import ErrorDetailModal from '@/components/common/ErrorDetailModal.vue'
import type { FailedSession } from '@/stores/sessions'

defineProps<{ row: FailedSession }>()
const emit = defineEmits<{ dismiss: [] }>()
const show = ref(false)
</script>

<template>
  <div class="session-row failed d-flex justify-content-between align-items-center gap-2">
    <div>
      <div class="fw-semibold">{{ row.workflow }}</div>
      <div class="small text-danger">Failed to initialize</div>
    </div>
    <div class="d-flex gap-1">
      <BButton size="sm" variant="danger" @click="show = true">Error</BButton>
      <BButton size="sm" variant="outline-secondary" @click="emit('dismiss')">Dismiss</BButton>
    </div>
    <ErrorDetailModal v-if="show" title="Workflow init failed" :text="row.detail ?? row.error" @hidden="show = false" />
  </div>
</template>
