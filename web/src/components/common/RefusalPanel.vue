<script setup lang="ts">
import { computed } from 'vue'

import { CREDENTIAL_REFUSED, HELLO_TIMEOUT, MALFORMED_HELLO } from '@/protocol/wire'
import { useConnectionStore } from '@/stores/connection'

const connection = useConnectionStore()

const headline = computed(() => {
  switch (connection.refusal?.code) {
    case MALFORMED_HELLO:
      return 'The server did not understand the hello.'
    case CREDENTIAL_REFUSED:
      return 'The server refused the credential or the origin.'
    case HELLO_TIMEOUT:
      return 'The server closed the socket before the hello arrived.'
    default:
      return 'The server refused the connection.'
  }
})
</script>

<template>
  <div class="d-flex align-items-center justify-content-center p-4">
    <BCard class="shadow" style="max-width: 36rem" title="Connection refused">
      <p class="mb-1">{{ headline }}</p>
      <p class="mono small mb-3">{{ connection.refusal?.reason }}</p>
      <pre v-if="connection.refusal?.detail" class="small text-secondary">{{ connection.refusal.detail }}</pre>
      <p class="small text-secondary mb-3">
        A remote server needs a ticket: the page that embeds the UI sets
        <code>window.WINSLOW = { wsUrl, ticket, ticketUrl }</code>. A local server accepts a page from its own origin,
        or from an origin listed in <code>WINSLOW_ORIGINS</code>.
      </p>
      <BButton variant="primary" @click="connection.retry()">Retry</BButton>
    </BCard>
  </div>
</template>
