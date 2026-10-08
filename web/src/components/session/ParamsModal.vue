<script setup lang="ts">
import { onMounted, ref } from 'vue'

import ParamsTable from '@/components/common/ParamsTable.vue'
import { useSessionScope } from '@/stores/inject'
import { useNoticesStore } from '@/stores/notices'

defineProps<{ instanceName: string }>()
const emit = defineEmits<{ hidden: [] }>()

const store = useSessionScope()
const notices = useNoticesStore()
const show = ref(true)
const params = ref<Record<string, unknown> | null>(null)

onMounted(async () => {
  try {
    const result = await store.reads.sessionParams()
    params.value = { ...result.settings, ...result.workflow_config }
  } catch (error) {
    notices.report(error)
    show.value = false
  }
})
</script>

<template>
  <BModal
    v-model="show"
    :title="`${instanceName}  ·  parameters`"
    size="lg"
    scrollable
    ok-only
    ok-title="Close"
    @hidden="emit('hidden')"
  >
    <ParamsTable v-if="params" :params="params" />
    <div v-else class="text-secondary">loading...</div>
  </BModal>
</template>
