<script setup lang="ts">
import { NO_PERMISSION_MESSAGE } from '@/protocol/messages'
import { useConnectionStore } from '@/stores/connection'
import { computed, ref, watch } from 'vue'

import OptionField from '@/components/dashboard/OptionField.vue'
import StartConfirmModal from '@/components/dashboard/StartConfirmModal.vue'
import { initialRaw, labelOf, validateGroup, type FormValues, type RawValue } from '@/protocol/forms'
import type { OptionInfo, WorkflowDescriptor } from '@/protocol/model'

const props = defineProps<{ descriptor: WorkflowDescriptor; overrides: readonly OptionInfo[] }>()
const emit = defineEmits<{ start: [values: FormValues] }>()

const connection = useConnectionStore()
const canStart = computed(() => connection.allows(props.descriptor.permission_scope, 'start_workflow'))

function initialRaws(options: readonly OptionInfo[]): Record<string, RawValue> {
  return Object.fromEntries(options.map((option) => [option.name, initialRaw(option)]))
}

const workflowRaws = ref<Record<string, RawValue>>({})
const orchestratorRaws = ref<Record<string, RawValue>>({})
const errors = ref<Record<string, string>>({})
const confirm = ref<FormValues | null>(null)

watch(
  () => props.descriptor.workflow,
  () => {
    workflowRaws.value = initialRaws(props.descriptor.options)
    orchestratorRaws.value = initialRaws(props.overrides)
    errors.value = {}
  },
  { immediate: true },
)

const errorSummary = computed(() => Object.keys(errors.value).map(labelOf).join(', '))

function submit() {
  const workflow = validateGroup(props.descriptor.options, workflowRaws.value)
  const orchestrator = validateGroup(props.overrides, orchestratorRaws.value)
  errors.value = { ...workflow.errors, ...orchestrator.errors }
  if (Object.keys(errors.value).length) return
  confirm.value = { workflow: workflow.values, orchestrator: orchestrator.values }
}

function proceed(values: FormValues) {
  emit('start', values)
}
</script>

<template>
  <div class="d-flex flex-column h-100">
    <div class="pane-title">Parameters - {{ descriptor.workflow }}</div>
    <div class="pane-body px-3">
      <template v-if="descriptor.options.length">
        <div class="fw-semibold mt-3 mb-1">Workflow</div>
        <OptionField
          v-for="option in descriptor.options"
          :key="option.name"
          v-model="workflowRaws[option.name]!"
          :option="option"
          :error="errors[option.name]"
        />
      </template>
      <template v-if="overrides.length">
        <div class="fw-semibold mt-3 mb-1">Orchestrator</div>
        <OptionField
          v-for="option in overrides"
          :key="option.name"
          v-model="orchestratorRaws[option.name]!"
          :option="option"
          :error="errors[option.name]"
        />
      </template>
      <div class="text-center py-3">
        <BButton
          variant="success"
          :disabled="!canStart"
          :title="canStart ? undefined : NO_PERMISSION_MESSAGE"
          @click="submit"
        >
          Start Workflow
        </BButton>
        <div v-if="errorSummary" class="text-danger small mt-2">invalid fields: {{ errorSummary }}</div>
      </div>
    </div>
    <StartConfirmModal
      v-if="confirm"
      :workflow="descriptor.workflow"
      :values="confirm"
      @proceed="proceed"
      @hidden="confirm = null"
    />
  </div>
</template>
