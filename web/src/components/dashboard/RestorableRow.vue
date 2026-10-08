<script setup lang="ts">
import { NO_PERMISSION_MESSAGE } from '@/protocol/messages'
import type { ManifestInfo } from '@/protocol/model'

defineProps<{ manifest: ManifestInfo; disabled: boolean }>()
const emit = defineEmits<{ restore: [] }>()
</script>

<template>
  <div class="session-row d-flex justify-content-between align-items-center gap-2">
    <div>
      <div class="fw-semibold">{{ manifest.workflow_class }}</div>
      <div class="mono small text-secondary">{{ manifest.session_id }}</div>
    </div>
    <BButton
      size="sm"
      variant="primary"
      :disabled="disabled"
      :title="disabled ? NO_PERMISSION_MESSAGE : undefined"
      @click="emit('restore')"
    >
      Restore
    </BButton>
  </div>
</template>
