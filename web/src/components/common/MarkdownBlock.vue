<script setup lang="ts">
// The markdown of the repository (the filter syntax) and of the task
// docstrings of the serve process. Both come from the project code behind the
// same credential as the tracebacks, so the HTML renders as it is.

import { computed } from 'vue'
import { marked } from 'marked'

const props = defineProps<{ text: string }>()

const html = computed(() => marked.parse(props.text, { async: false }) as string)
</script>

<template>
  <!-- eslint-disable-next-line vue/no-v-html -->
  <div class="markdown-body" v-html="html"></div>
</template>
