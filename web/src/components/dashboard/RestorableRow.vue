<script setup lang="ts">
import type { ManifestInfo } from '@/protocol/model'

defineProps<{ manifest: ManifestInfo }>()
const emit = defineEmits<{ restore: [] }>()
</script>

<template>
  <div class="session-row d-flex justify-content-between align-items-center gap-2">
    <div>
      <div class="fw-semibold">{{ manifest.workflow_class }}</div>
      <div class="mono small text-secondary">{{ manifest.session_id }}</div>
    </div>
    <BButton size="sm" variant="primary" @click="emit('restore')">Restore</BButton>
  </div>
</template>
