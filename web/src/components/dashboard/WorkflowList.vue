<script setup lang="ts">
import { computed, ref } from 'vue'

import type { WorkflowRow } from '@/protocol/workflows'

const props = defineProps<{ rows: readonly WorkflowRow[]; selected: string | null }>()
const emit = defineEmits<{ select: [name: string] }>()

const filter = ref('')
const shown = computed(() => {
  const needle = filter.value.trim().toLowerCase()
  return needle ? props.rows.filter((row) => row.name.toLowerCase().includes(needle)) : props.rows
})

function indent(row: WorkflowRow) {
  return row.depth ? { paddingLeft: `calc(var(--bs-list-group-item-padding-x) + ${row.depth * 1.25}rem)` } : {}
}
</script>

<template>
  <div class="pane h-100">
    <div class="pane-title">Workflows</div>
    <div class="px-2 pt-2">
      <BFormInput v-model="filter" size="sm" placeholder="filter workflows..." />
    </div>
    <div class="pane-body p-2">
      <div class="list-group list-group-flush selectable-list">
        <button
          v-for="row in shown"
          :key="row.name"
          type="button"
          class="list-group-item list-group-item-action py-1"
          :class="{ active: row.name === selected }"
          :style="indent(row)"
          @click="emit('select', row.name)"
        >
          <span v-if="row.depth" class="opacity-75 me-1">↳</span>{{ row.name }}
        </button>
        <div v-if="!shown.length" class="text-secondary small p-2">No workflow matches.</div>
      </div>
    </div>
  </div>
</template>
