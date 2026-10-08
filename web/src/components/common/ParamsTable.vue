<script setup lang="ts">
import { safeRepr } from '@/protocol/format'

defineProps<{ params: Record<string, unknown> | null | undefined; empty?: string }>()
</script>

<template>
  <table v-if="params && Object.keys(params).length" class="table table-sm table-striped mb-0">
    <thead>
      <tr>
        <th>Parameter</th>
        <th>Value</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="(value, key) in params" :key="key">
        <td class="text-nowrap">{{ String(key).replace(/_/g, ' ') }}</td>
        <td class="mono">{{ safeRepr(value) }}</td>
      </tr>
    </tbody>
  </table>
  <p v-else class="text-secondary small mb-0 p-2">{{ empty ?? 'No parameters.' }}</p>
</template>
