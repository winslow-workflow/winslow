<script setup lang="ts">
import { ref } from 'vue'

import ParamsTable from '@/components/common/ParamsTable.vue'
import type { FormValues } from '@/protocol/forms'

const props = defineProps<{ workflow: string; values: FormValues }>()
const emit = defineEmits<{ proceed: [values: FormValues]; hidden: [] }>()
const show = ref(true)
</script>

<template>
  <BModal
    v-model="show"
    :title="`Start workflow: ${workflow}`"
    size="lg"
    scrollable
    ok-title="Proceed"
    ok-variant="success"
    cancel-variant="outline-danger"
    @ok="emit('proceed', props.values)"
    @hidden="emit('hidden')"
  >
    <div class="fw-semibold mb-1">Workflow</div>
    <ParamsTable :params="values.workflow" />
    <div class="fw-semibold mt-3 mb-1">Orchestrator</div>
    <ParamsTable :params="values.orchestrator" />
  </BModal>
</template>
