<script setup lang="ts">
import type { TaskStatusName } from '@/protocol/model'
import { statusLabel, taskIcon, taskVariant } from '@/protocol/status'

defineProps<{ status: TaskStatusName | null | undefined; iconOnly?: boolean }>()
</script>

<template>
  <span class="text-nowrap">
    <span class="status-icon" :title="statusLabel(status)">{{ taskIcon(status) }}</span>
    <BBadge v-if="!iconOnly" :variant="taskVariant(status)" class="ms-1">{{ statusLabel(status) || '?' }}</BBadge>
  </span>
</template>
