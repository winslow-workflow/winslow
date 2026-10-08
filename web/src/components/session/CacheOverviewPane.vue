<script setup lang="ts">
import { computed } from 'vue'

import InfoRows from '@/components/common/InfoRows.vue'
import type { CacheInfo } from '@/protocol/model'
import { entryVariant } from '@/protocol/status'

const props = defineProps<{ card: CacheInfo | null }>()

function cleanDoc(text: string | null): string {
  if (!text) return ''
  const lines = text.replace(/\t/g, '    ').split('\n')
  const indents = lines
    .slice(1)
    .filter((line) => line.trim())
    .map((line) => line.length - line.trimStart().length)
  const margin = indents.length ? Math.min(...indents) : 0
  return [lines[0]?.trim() ?? '', ...lines.slice(1).map((line) => line.slice(margin))].join('\n').trim()
}

const rows = computed<[string, string][]>(() => {
  const card = props.card
  if (!card) return []
  return [
    ['cache', card.name],
    ['scope', card.scope],
    ['description', cleanDoc(card.docstring) || '(no docstring)'],
    ['storage layers', card.storage.replace(' over ', '\nover ')],
    ['entries', [...card.entries].sort().join('\n') || '(none)'],
  ]
})

const states = computed(() => new Map((props.card?.info ?? []).map((info) => [info.entry_name, info.state])))

const edges = computed(() =>
  [...(props.card?.info ?? [])]
    .filter((info) => info.depends_on.length)
    .sort((a, b) => a.entry_name.localeCompare(b.entry_name)),
)
</script>

<template>
  <div v-if="!card" class="d-flex align-items-center justify-content-center h-100 text-secondary">
    Select a cache to see the details.
  </div>
  <div v-else class="pane-body">
    <div class="pane-title">cache information</div>
    <InfoRows :rows="rows" />
    <template v-if="edges.length">
      <div class="pane-title border-top">dependencies</div>
      <div class="px-3 py-2">
        <div v-for="info in edges" :key="info.entry_name" class="mb-2">
          <div class="fw-semibold small">{{ info.entry_name }}</div>
          <div v-for="name in info.depends_on" :key="name" class="dependency-row ps-2">
            <span :class="`text-${entryVariant(states.get(name))}`">●</span>
            <span>{{ name }}</span>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
