<script setup lang="ts">
import { computed, ref } from 'vue'

import { safeRepr } from '@/protocol/format'

// One level renders per expand, at most this many children per node, so a
// large container stays browsable.
const NODE_CHILD_CAP = 200

const props = defineProps<{ label: string; value: unknown; depth?: number }>()

const children = computed<[string, unknown][]>(() => {
  const value = props.value
  if (Array.isArray(value)) return value.map((child, index) => [String(index), child])
  if (value !== null && typeof value === 'object') return Object.entries(value as Record<string, unknown>)
  return []
})

const expanded = ref((props.depth ?? 0) === 0)

const kind = computed(() => (Array.isArray(props.value) ? 'list' : 'dict'))

const shown = computed(() => children.value.slice(0, NODE_CHILD_CAP))
const rest = computed(() => children.value.length - shown.value.length)

function toggle() {
  expanded.value = !expanded.value
}
</script>

<template>
  <div class="json-tree">
    <div>
      <span
        v-if="children.length"
        class="toggle"
        role="button"
        tabindex="0"
        :aria-expanded="expanded"
        @click="toggle"
        @keydown.enter.prevent="toggle"
        @keydown.space.prevent="toggle"
      >
        {{ expanded ? '▾' : '▸' }}
      </span>
      <span v-else class="toggle"></span>
      <span class="mono">{{ label }}: </span>
      <span v-if="children.length" class="text-secondary">{{ kind }}, len {{ children.length }}</span>
      <span v-else class="mono">{{ safeRepr(value) }}</span>
    </div>
    <ul v-if="expanded && children.length">
      <li v-for="[childLabel, child] in shown" :key="childLabel">
        <JsonTree :label="childLabel" :value="child" :depth="(depth ?? 0) + 1" />
      </li>
      <li v-if="rest > 0" class="text-secondary">... {{ rest }} more</li>
    </ul>
  </div>
</template>
