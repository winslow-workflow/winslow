<script setup lang="ts">
import { ref } from 'vue'

defineProps<{ title: string; text: string }>()
const emit = defineEmits<{ hidden: [] }>()
const show = ref(true)
</script>

<template>
  <BModal v-model="show" :title="title" size="xl" scrollable ok-only ok-title="Close" @hidden="emit('hidden')">
    <pre class="mono small mb-0">{{ text || 'No details available.' }}</pre>
  </BModal>
</template>
