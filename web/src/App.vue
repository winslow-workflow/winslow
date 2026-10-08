<script setup lang="ts">
import { onMounted } from 'vue'

import ConnectionBar from '@/components/common/ConnectionBar.vue'
import NoticeHost from '@/components/common/NoticeHost.vue'
import RefusalPanel from '@/components/common/RefusalPanel.vue'
import { useConnectionStore } from '@/stores/connection'
import { useSessionsStore } from '@/stores/sessions'

const connection = useConnectionStore()
// The dashboard scope follows the wire on its own (see useSessionsStore).
useSessionsStore()

onMounted(() => {
  connection.connect()
})
</script>

<template>
  <BApp>
    <div class="app-shell d-flex flex-column vh-100">
      <ConnectionBar />
      <RefusalPanel v-if="connection.state === 'refused'" class="flex-grow-1" />
      <!-- The key remounts the view per session. A record route of the same session keeps it. -->
      <RouterView v-else v-slot="{ Component, route }" class="flex-grow-1 min-h-0">
        <component :is="Component" :key="route.params.id ?? route.path" />
      </RouterView>
      <NoticeHost />
    </div>
  </BApp>
</template>
