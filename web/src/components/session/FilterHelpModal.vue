<script setup lang="ts">
import { ref } from 'vue'

import MarkdownBlock from '@/components/common/MarkdownBlock.vue'
import syntax from '../../../../src/winslow/filter/syntax.md?raw'

const emit = defineEmits<{ hidden: [] }>()
const show = ref(true)
</script>

<template>
  <BModal v-model="show" title="Filter Syntax" size="lg" scrollable ok-only ok-title="Close" @hidden="emit('hidden')">
    <MarkdownBlock :text="syntax" />
  </BModal>
</template>
