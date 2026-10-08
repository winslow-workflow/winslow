<script setup lang="ts">
// The Code tab: the inheritance tree on the left, the concrete task at the
// root, and the source of the selected class on the right.

import { computed, ref } from 'vue'

import type { SourceNode } from '@/protocol/model'

const props = defineProps<{ root: SourceNode }>()

const selected = ref<SourceNode>(props.root)
const lines = computed(() => selected.value.source.split('\n'))

interface TreeRow {
  node: SourceNode
  depth: number
}

function flatten(node: SourceNode, depth = 0): TreeRow[] {
  return [{ node, depth }, ...node.children.flatMap((child) => flatten(child, depth + 1))]
}

const rows = computed(() => flatten(props.root))
</script>

<template>
  <div class="source-view">
    <div class="source-tree" role="tree">
      <div
        v-for="row in rows"
        :key="`${row.node.module}.${row.node.name}`"
        :style="{ paddingLeft: `${row.depth}rem` }"
        role="treeitem"
        :aria-selected="row.node === selected"
      >
        <span
          :class="{ active: row.node === selected }"
          role="button"
          tabindex="0"
          @click="selected = row.node"
          @keydown.enter.prevent="selected = row.node"
        >
          {{ row.node.label }}
        </span>
      </div>
    </div>
    <div class="min-w-0">
      <div class="small text-secondary mb-1">{{ selected.name }} · {{ selected.location }}</div>
      <pre class="source-code"><span v-for="(line, index) in lines" :key="index" class="line">{{ line }}
</span></pre>
    </div>
  </div>
</template>
