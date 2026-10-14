<script setup lang="ts">
import { computed, ref } from 'vue'

import { CREDENTIAL_REFUSED, HELLO_TIMEOUT, MALFORMED_HELLO, ORIGIN_REFUSED } from '@/protocol/wire'
import { useConnectionStore } from '@/stores/connection'

const connection = useConnectionStore()
const token = ref('')

const headline = computed(() => {
  switch (connection.refusal?.code) {
    case MALFORMED_HELLO:
      return 'The server did not understand the connection request.'
    case CREDENTIAL_REFUSED:
      return 'The server refused the credential.'
    case ORIGIN_REFUSED:
      return 'The server refused the origin of this page.'
    case HELLO_TIMEOUT:
      return 'The server closed the socket before the connection request arrived.'
    default:
      return 'The server refused the connection.'
  }
})

function submitToken() {
  const value = token.value.trim()
  if (!value) return
  token.value = ''
  connection.setToken(value)
}
</script>

<template>
  <div class="d-flex align-items-center justify-content-center p-4">
    <BCard class="shadow" style="max-width: 36rem" title="Connection refused">
      <p class="mb-1">{{ headline }}</p>
      <p class="mono small mb-3">{{ connection.refusal?.reason }}</p>
      <pre v-if="connection.refusal?.detail" class="small text-secondary">{{ connection.refusal.detail }}</pre>
      <form v-if="connection.needsToken" class="mb-3" @submit.prevent="submitToken">
        <div class="d-flex gap-2 align-items-end">
          <label class="form-label small flex-grow-1 mb-0" for="token-field">
            Token
            <input
              id="token-field"
              v-model="token"
              class="form-control form-control-sm"
              type="password"
              autocomplete="off"
            />
          </label>
          <BButton type="submit" size="sm" variant="primary" :disabled="!token.trim()">Connect</BButton>
        </div>
        <p class="small text-secondary mt-2 mb-0">
          The token is kept for this tab and sent in the hello. It grants what its owner may do on the server.
        </p>
      </form>
      <p v-else class="small text-secondary mb-3">
        A remote server needs a credential: the page that embeds the UI sets
        <code>window.WINSLOW = { wsUrl, credential, credentialUrl }</code>. A local server accepts a page from its own origin,
        or from an origin listed in <code>SERVE_ORIGINS</code>.
      </p>
      <BButton variant="primary" @click="connection.retry()">Retry</BButton>
    </BCard>
  </div>
</template>
