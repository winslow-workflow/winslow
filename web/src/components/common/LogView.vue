<script setup lang="ts" generic="T">
import { nextTick, ref, watch } from 'vue'

// offset counts the lines a cap removed from the front, so a key names one
// line for its whole life and a front trim patches nothing.
const props = defineProps<{ lines: readonly T[]; offset?: number; empty?: string }>()
const box = ref<HTMLElement | null>(null)

// Follow the tail while the user reads the end of the log.
watch(
  () => props.lines.length,
  async () => {
    const element = box.value
    if (element === null) return
    const atBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 40
    await nextTick()
    if (atBottom) element.scrollTop = element.scrollHeight
  },
)
</script>

<template>
  <pre ref="box" class="log-view"><template v-if="lines.length"><span
        v-for="(line, index) in lines"
        :key="(offset ?? 0) + index"
      ><slot :line="line">{{ line }}</slot>
</span></template><span v-else class="text-secondary">{{ empty ?? 'No log lines yet.' }}</span></pre>
</template>
