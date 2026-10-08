<script setup lang="ts">
// The two-step search of a pane: a typed query previews the matches after a
// delay, the submit applies the filter (see winslow.ui.filtering). The host
// answers match(query) with the matching keys, or null for a query it cannot
// parse.

import { onBeforeUnmount, ref } from 'vue'

import FilterHelpModal from '@/components/session/FilterHelpModal.vue'
import { useStaleGuard } from '@/composables/useStaleGuard'
import { messageOf } from '@/protocol/errors'

const PREVIEW_DELAY = 500

const props = defineProps<{
  placeholder: string
  match: (query: string) => Promise<Set<string> | null>
  help?: boolean
}>()

const emit = defineEmits<{
  preview: [keys: Set<string> | null]
  apply: [keys: Set<string> | null]
}>()

const text = ref('')
const state = ref<boolean | null>(null)
const error = ref<string | null>(null)
const showHelp = ref(false)
const guard = useStaleGuard()
let timer: ReturnType<typeof setTimeout> | null = null

function stopTimer() {
  if (timer !== null) {
    clearTimeout(timer)
    timer = null
  }
}

async function resolve(query: string): Promise<Set<string> | null> {
  try {
    const keys = await props.match(query)
    state.value = keys === null ? false : null
    error.value = null
    return keys
  } catch (failure) {
    state.value = false
    error.value = messageOf(failure)
    return null
  }
}

function clear() {
  state.value = null
  error.value = null
}

function onInput() {
  stopTimer()
  const token = guard.next()
  const query = text.value.trim()
  if (!query) {
    clear()
    emit('preview', null)
    return
  }
  timer = setTimeout(async () => {
    const keys = await resolve(query)
    // A submit or a newer keystroke during the read owns the outcome.
    if (guard.isCurrent(token)) emit('preview', keys)
  }, PREVIEW_DELAY)
}

async function onSubmit() {
  stopTimer()
  const token = guard.next()
  const query = text.value.trim()
  emit('preview', null)
  if (!query) {
    clear()
    emit('apply', null)
    return
  }
  const keys = await resolve(query)
  if (keys !== null && guard.isCurrent(token)) emit('apply', keys)
}

onBeforeUnmount(stopTimer)
</script>

<template>
  <div class="d-flex align-items-start gap-1 filter-input">
    <div class="flex-grow-1">
      <BFormInput
        v-model="text"
        size="sm"
        :placeholder="placeholder"
        :state="state"
        @input="onInput"
        @keydown.enter.prevent="onSubmit"
      />
      <div v-if="error" class="invalid-feedback d-block small">{{ error }}</div>
    </div>
    <BButton v-if="help" size="sm" variant="outline-secondary" title="filter syntax" @click="showHelp = true">
      ?
    </BButton>
    <FilterHelpModal v-if="showHelp" @hidden="showHelp = false" />
  </div>
</template>
