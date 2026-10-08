<script setup lang="ts">
// Label and value rows of an overview pane. The `first` slot renders under
// the first value, for the tag row of a task.

defineProps<{ rows: [string, string][] }>()
</script>

<template>
  <div class="info-rows px-3 py-2">
    <div v-for="([label, value], index) in rows" :key="label" :class="{ 'border-top pt-2 mt-2': index > 0 }">
      <div class="label">{{ label }}</div>
      <div class="value">{{ value }}</div>
      <slot v-if="index === 0" name="first" />
    </div>
  </div>
</template>
