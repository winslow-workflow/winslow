<script setup lang="ts">
// The system resources pane: memory, cpu and host of the process that runs
// the sessions, polled over the host_stats read while the wire is connected.

import { computed, ref } from 'vue'

import { useInterval } from '@/composables/useInterval'
import { formatSize } from '@/protocol/format'
import type { HostStats } from '@/protocol/model'
import { useConnectionStore } from '@/stores/connection'

const HOST_STATS_INTERVAL = 1000

const connection = useConnectionStore()
const stats = ref<HostStats | null>(null)

// The two stats with a load bar under the value.
const bars = computed(() => {
  if (stats.value === null) return []
  const { memory_used, memory_total, memory_percent, cpu_percent } = stats.value
  return [
    {
      label: 'memory',
      text: `${formatSize(memory_used)} / ${formatSize(memory_total)} (${memory_percent}%)`,
      percent: memory_percent,
    },
    { label: 'cpu', text: `${cpu_percent.toFixed(2)} %`, percent: cpu_percent },
  ]
})

async function poll() {
  try {
    stats.value = await connection.app.hostStats()
  } catch {
    // The connection bar reports a wire outage. The pane keeps the last sample.
  }
}

useInterval(poll, HOST_STATS_INTERVAL, () => connection.connected)

// The hue of the TUI in three steps: green at a low load, red at a high one.
function loadClass(percent: number): string {
  if (percent < 50) return 'bg-success'
  if (percent < 80) return 'bg-warning'
  return 'bg-danger'
}
</script>

<template>
  <div class="pane-title">System resources</div>
  <div v-if="stats" class="host-stats pane-body px-3 py-2">
    <div v-for="bar in bars" :key="bar.label" class="stat">
      <div class="d-flex justify-content-between gap-2">
        <span class="label">{{ bar.label }}</span>
        <span>{{ bar.text }}</span>
      </div>
      <div
        class="progress"
        role="progressbar"
        :aria-label="bar.label"
        :aria-valuenow="bar.percent"
        aria-valuemin="0"
        aria-valuemax="100"
      >
        <div class="progress-bar" :class="loadClass(bar.percent)" :style="{ width: `${bar.percent}%` }" />
      </div>
    </div>
    <div class="stat d-flex justify-content-between gap-2">
      <span class="label">host</span>
      <span class="text-truncate">{{ stats.hostname }}</span>
    </div>
    <div v-if="stats.user !== null" class="stat d-flex justify-content-between gap-2">
      <span class="label">user</span>
      <span class="text-truncate">{{ stats.user }}</span>
    </div>
  </div>
  <div v-else class="text-secondary text-center py-4 small">Waiting for the first sample.</div>
</template>
