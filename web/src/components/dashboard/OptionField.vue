<script setup lang="ts">
import { computed } from 'vue'

import { controlOf, inputType, labelOf, type RawValue } from '@/protocol/forms'
import type { OptionInfo } from '@/protocol/model'

const props = defineProps<{ option: OptionInfo; error?: string }>()
const model = defineModel<RawValue>({ required: true })

const control = computed(() => controlOf(props.option))
const choices = computed(() => (props.option.choices ?? []).map((choice) => ({ value: choice, text: choice })))
const state = computed(() => (props.error ? false : null))
const id = computed(() => `option-${props.option.name}`)
// A group control has no single input for a label to point at.
const grouped = computed(() => control.value === 'checkboxes' || control.value === 'radio')

// One typed view per control kind over the raw value the form holds.
const boolModel = computed({
  get: () => model.value === true,
  set: (value: boolean) => (model.value = value),
})
const listModel = computed({
  get: () => (Array.isArray(model.value) ? model.value : []),
  set: (value: string[]) => (model.value = value),
})
const textModel = computed({
  get: () => (typeof model.value === 'string' ? model.value : ''),
  set: (value: string | number | null | undefined) => (model.value = value == null ? '' : String(value)),
})
</script>

<template>
  <div class="option-row" role="group" :aria-labelledby="`${id}-label`">
    <component
      :is="grouped ? 'span' : 'label'"
      :id="`${id}-label`"
      class="option-label"
      :for="grouped ? undefined : id"
    >
      {{ labelOf(option.name) }}
    </component>
    <div>
      <BFormCheckbox v-if="control === 'switch'" :id="id" v-model="boolModel" switch :state="state" />
      <BFormCheckboxGroup
        v-else-if="control === 'checkboxes'"
        :id="id"
        v-model="listModel"
        :options="choices"
        :state="state"
        stacked
      />
      <BFormRadioGroup
        v-else-if="control === 'radio'"
        :id="id"
        v-model="textModel"
        :options="choices"
        :state="state"
        stacked
      />
      <BFormSelect
        v-else-if="control === 'select'"
        :id="id"
        v-model="textModel"
        :options="choices"
        :state="state"
        size="sm"
      />
      <BFormInput
        v-else
        :id="id"
        v-model="textModel"
        size="sm"
        :type="inputType(option)"
        :step="option.type === 'int' ? 1 : 'any'"
        :state="state"
      />
      <div v-if="error" class="invalid-feedback d-block small">{{ error }}</div>
      <div v-if="option.help" class="form-help mt-1">{{ option.help }}</div>
    </div>
  </div>
</template>
