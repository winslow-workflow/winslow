<script setup lang="ts">
// The value view of one cache entry: the live CacheValueView of the port, or
// a stored CacheReadSnapshot of the history. JSON renders as a tree, text as
// it is, and an errored entry shows its context.

import { computed, onMounted, ref } from 'vue'

import JsonTree from '@/components/common/JsonTree.vue'
import { messageOf } from '@/protocol/errors'
import type { CacheReadSnapshot, CacheValueView } from '@/protocol/model'
import { useSessionScope } from '@/stores/inject'

const props = defineProps<{ cacheName: string; entryName: string; snapshot?: CacheReadSnapshot | null }>()
const emit = defineEmits<{ hidden: [] }>()

type View =
  | { kind: 'loading' }
  | { kind: 'tree'; value: unknown; note: string | null }
  | { kind: 'text'; text: string; note: string | null }
  | { kind: 'note'; text: string }
  | { kind: 'traceback'; text: string; note: string | null }

const store = useSessionScope()
const show = ref(true)
const view = ref<View>({ kind: 'loading' })

const title = computed(() =>
  props.snapshot ? `${props.cacheName}.${props.entryName} (snapshot)` : `${props.cacheName}.${props.entryName}`,
)

function fromValue(value: CacheValueView): View {
  const notes: string[] = []
  if (value.error) {
    const tier = value.error.tier ? ` on ${value.error.tier}` : ''
    notes.push(`ERRORED - the ${value.error.origin} failed${tier}: ${value.error.message}`)
  }
  if (value.summary) notes.push(`bounded rendering - ${value.summary}`)
  const note = notes.length ? notes.join('\n') : null
  if (value.rendered === null) {
    if (value.error?.traceback) return { kind: 'traceback', text: value.error.traceback, note }
    if (value.state === 'computing') return { kind: 'note', text: '<computing - the loader runs right now>' }
    return { kind: 'note', text: note ?? '<cold - no value>' }
  }
  if (value.encoding === 'json') return fromRendered(value.rendered, note)
  return { kind: 'text', text: value.rendered, note }
}

function fromRendered(rendered: string, note: string | null): View {
  try {
    return { kind: 'tree', value: JSON.parse(rendered), note }
  } catch (error) {
    return {
      kind: 'text',
      text: rendered,
      note: [note, `JSON decode failed: ${messageOf(error)}`].filter(Boolean).join('\n'),
    }
  }
}

onMounted(async () => {
  if (props.snapshot) {
    view.value = fromRendered(props.snapshot.rendered, props.snapshot.summary)
    return
  }
  try {
    view.value = fromValue(await store.reads.cacheValue(props.cacheName, props.entryName))
  } catch (error) {
    view.value = { kind: 'traceback', text: messageOf(error), note: null }
  }
})
</script>

<template>
  <BModal
    v-model="show"
    :title="title"
    dialog-class="detail-dialog"
    scrollable
    ok-only
    ok-title="Close"
    @hidden="emit('hidden')"
  >
    <div v-if="view.kind === 'loading'" class="text-secondary">loading...</div>
    <template v-else>
      <pre v-if="'note' in view && view.note" class="text-warning small">{{ view.note }}</pre>
      <JsonTree v-if="view.kind === 'tree'" :label="entryName" :value="view.value" />
      <pre v-else-if="view.kind === 'text'" class="mono small mb-0">{{ view.text }}</pre>
      <pre v-else-if="view.kind === 'traceback'" class="mono small text-danger mb-0">{{ view.text }}</pre>
      <div v-else class="text-secondary">{{ view.text }}</div>
    </template>
  </BModal>
</template>
