<script setup lang="ts">
import { ref } from 'vue'

import ErrorDetailModal from '@/components/common/ErrorDetailModal.vue'
import { useNoticesStore, type Notice } from '@/stores/notices'

const TOAST_MS = 6000

const notices = useNoticesStore()
const detail = ref<Notice | null>(null)
</script>

<template>
  <div class="toast-stack">
    <BToast
      v-for="notice in notices.queue"
      :key="notice.id"
      :model-value="TOAST_MS"
      :variant="notice.variant"
      :title="notice.title ?? undefined"
      solid
      @hidden="notices.dismiss(notice.id)"
    >
      <div class="d-flex gap-2 align-items-start">
        <span class="flex-grow-1">{{ notice.body }}</span>
        <BButton v-if="notice.detail" size="sm" variant="outline-light" @click="detail = notice">details</BButton>
      </div>
    </BToast>
  </div>
  <ErrorDetailModal
    v-if="detail"
    :title="detail.title ?? 'Error'"
    :text="detail.detail ?? ''"
    @hidden="detail = null"
  />
</template>
