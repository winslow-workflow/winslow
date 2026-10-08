<script setup lang="ts">
import { computed, ref } from 'vue'

const props = defineProps<{ names: readonly string[]; selected: string | null }>()
const emit = defineEmits<{ select: [name: string] }>()

const filter = ref('')
const shown = computed(() => {
  const needle = filter.value.trim().toLowerCase()
  return needle ? props.names.filter((name) => name.toLowerCase().includes(needle)) : props.names
})
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
          v-for="name in shown"
          :key="name"
          type="button"
          class="list-group-item list-group-item-action py-1"
          :class="{ active: name === selected }"
          @click="emit('select', name)"
        >
          {{ name }}
        </button>
        <div v-if="!shown.length" class="text-secondary small p-2">No workflow matches.</div>
      </div>
    </div>
  </div>
</template>
