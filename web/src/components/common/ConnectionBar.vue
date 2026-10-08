<script setup lang="ts">
import { computed } from 'vue'

import { useConnectionStore } from '@/stores/connection'

const connection = useConnectionStore()

const stateVariant = computed(() => {
  switch (connection.state) {
    case 'connected':
      return 'success'
    case 'connecting':
    case 'reconnecting':
      return 'warning'
    case 'refused':
      return 'danger'
    default:
      return 'secondary'
  }
})
</script>

<template>
  <nav
    class="navbar navbar-expand bg-body-tertiary env-bar px-3 py-1"
    :style="{ '--ws-env-color': connection.envColor ?? undefined }"
  >
    <RouterLink class="navbar-brand fw-bold" to="/">winslow</RouterLink>
    <span v-if="connection.env" class="env-badge me-3">{{ connection.env }}</span>
    <ul class="navbar-nav me-auto">
      <li class="nav-item">
        <RouterLink class="nav-link" to="/">Dashboard</RouterLink>
      </li>
    </ul>
    <span class="text-secondary small me-3 mono">{{ connection.url }}</span>
    <span v-if="connection.user" class="small me-2">{{ connection.user }}</span>
    <BBadge :variant="stateVariant">{{ connection.state }}</BBadge>
  </nav>
</template>
