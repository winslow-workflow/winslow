<script setup lang="ts">
import { ref } from 'vue'

import ParamsTable from '@/components/common/ParamsTable.vue'
import { formatClock } from '@/protocol/format'
import type { BatchInfo } from '@/protocol/model'

defineProps<{ batch: BatchInfo }>()
const emit = defineEmits<{ hidden: [] }>()
const show = ref(true)
</script>

<template>
  <BModal
    v-model="show"
    :title="`${batch.action}  ·  ${formatClock(batch.created_at)}`"
    scrollable
    ok-only
    ok-title="Close"
    @hidden="emit('hidden')"
  >
    <div class="fw-semibold mb-1">Parameters</div>
    <ParamsTable :params="batch.options" />
    <div v-if="batch.error" class="text-danger small mt-3">{{ batch.error }}</div>
  </BModal>
</template>
