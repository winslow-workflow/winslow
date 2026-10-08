<script setup lang="ts">
// The caches of the session, from the caches read of the store. A cache event
// triggers a refresh; a tick covers the lazy ttl expiry while the tab shows.

import { computed, ref, watch } from 'vue'

import FilterInput from '@/components/common/FilterInput.vue'
import CacheValueModal from '@/components/session/CacheValueModal.vue'
import { useInterval } from '@/composables/useInterval'
import { plural } from '@/protocol/format'
import type { CacheEntryInfo, CacheInfo } from '@/protocol/model'
import { entryVariant } from '@/protocol/status'
import { useSessionScope } from '@/stores/inject'
import { useNoticesStore } from '@/stores/notices'

const CACHE_TICK = 2000
const SCOPE_ALL = 'all'
const SCOPE_OPTIONS = [SCOPE_ALL, 'workflow', 'global'].map((scope) => ({ value: scope, text: scope }))

const props = defineProps<{ active: boolean }>()
const emit = defineEmits<{ select: [cacheName: string] }>()

const store = useSessionScope()
const notices = useNoticesStore()

const filterKeys = ref<Set<string> | null>(null)
const previewKeys = ref<Set<string> | null>(null)
const scope = ref(SCOPE_ALL)
const selectedCache = ref<string | null>(null)
const selectedRow = ref<string | null>(null)
const viewing = ref<[string, string] | null>(null)
const unobservable = new Set<string>()

interface EntryRow {
  cache: CacheInfo
  name: string
  key: string
  info: CacheEntryInfo | undefined
  preview: string
}

function rowsOf(card: CacheInfo): EntryRow[] {
  const infos = new Map(card.info.map((info) => [info.entry_name, info]))
  return card.entries.map((name) => ({
    cache: card,
    name,
    key: `${card.name}.${name}`,
    info: infos.get(name),
    preview: card.values[name] ?? '',
  }))
}

const allRows = computed(() => store.caches.flatMap(rowsOf))

function visible(row: EntryRow): boolean {
  if (scope.value !== SCOPE_ALL && row.cache.scope !== scope.value) return false
  return filterKeys.value === null || filterKeys.value.has(row.key)
}

const shownCards = computed(() =>
  store.caches.map((card) => ({ card, rows: rowsOf(card).filter(visible) })).filter((item) => item.rows.length),
)

// The row keys whose entry name or cache name contains the query.
async function match(query: string): Promise<Set<string> | null> {
  const needle = query.toLowerCase()
  return new Set(
    allRows.value
      .filter((row) => row.name.toLowerCase().includes(needle) || row.cache.name.toLowerCase().includes(needle))
      .map((row) => row.key),
  )
}

function rowClass(row: EntryRow) {
  return {
    'row-selected': row.key === selectedRow.value,
    'filter-match': previewKeys.value !== null && previewKeys.value.has(row.key),
    'filter-dim': previewKeys.value !== null && !previewKeys.value.has(row.key),
    'row-clickable': true,
  }
}

function warmCount(card: CacheInfo): string {
  const warm = card.info.filter((info) => info.state === 'warm').length
  return `${warm}/${card.info.length} warm`
}

async function refresh() {
  await store.refreshCaches()
  for (const card of store.caches) {
    if (card.error !== null && !unobservable.has(card.name)) {
      unobservable.add(card.name)
      notices.record('error', `The cache pane cannot observe '${card.name}': ${card.error}`)
    } else if (card.error === null) {
      unobservable.delete(card.name)
    }
  }
}

watch(
  () => store.cacheVersion,
  () => void refresh(),
)
useInterval(
  () => void refresh(),
  CACHE_TICK,
  () => props.active && !store.ended,
)

function select(card: CacheInfo, row: EntryRow | null = null) {
  selectedCache.value = card.name
  selectedRow.value = row?.key ?? null
  emit('select', card.name)
}

function visiblePairs(): [string, string][] {
  return shownCards.value.flatMap((item) => item.rows.map((row) => [item.card.name, row.name] as [string, string]))
}

async function loadAll() {
  const pairs = visiblePairs()
  if (!pairs.length) {
    notices.info('No cache entries are visible - nothing to load')
    return
  }
  const ack = await store.loadCacheEntries(pairs)
  if (ack.accepted) notices.success(`Loading ${plural(pairs.length, 'cache entry', 'cache entries')}`)
}

async function clearAll() {
  const pairs = visiblePairs()
  if (!pairs.length) {
    notices.info('No cache entries are visible - nothing to clear')
    return
  }
  const ack = await store.clearCacheEntries(pairs)
  if (ack.accepted) notices.success(`Clearing ${plural(pairs.length, 'cache entry', 'cache entries')}`)
}

function errorTitle(info: CacheEntryInfo | undefined): string | undefined {
  return info?.error ? `${info.error.origin}: ${info.error.message}` : undefined
}
</script>

<template>
  <div class="d-flex flex-column h-100">
    <div class="pane-header">
      <FilterInput
        placeholder="search entries..."
        :match="match"
        @preview="previewKeys = $event"
        @apply="filterKeys = $event"
      />
      <BFormSelect v-model="scope" :options="SCOPE_OPTIONS" size="sm" class="w-auto" />
      <div class="ms-auto d-flex gap-1">
        <BButton size="sm" variant="danger" :disabled="store.ended" @click="clearAll">clear all</BButton>
        <BButton size="sm" variant="success" :disabled="store.ended" @click="loadAll">load all</BButton>
      </div>
    </div>
    <div class="pane-body">
      <div
        v-for="{ card, rows } in shownCards"
        :key="card.name"
        class="batch-card"
        :class="{ 'border-primary': card.name === selectedCache }"
      >
        <div
          class="batch-header row-clickable"
          role="button"
          tabindex="0"
          @click="select(card)"
          @keydown.enter="select(card)"
        >
          <span class="fw-semibold">{{ card.name }}</span>
          <span class="text-secondary">{{ card.scope }}</span>
          <span v-if="card.error === null" class="text-secondary">{{ warmCount(card) }}</span>
          <span v-else class="text-danger" :title="card.error">storage error</span>
        </div>
        <table class="table table-sm table-hover task-table mb-0">
          <tbody>
            <tr
              v-for="row in rows"
              :key="row.name"
              :class="rowClass(row)"
              tabindex="0"
              @click="select(card, row)"
              @keydown.enter="select(card, row)"
            >
              <td class="col-entry fw-semibold">{{ row.name }}</td>
              <td class="col-state">
                <BBadge v-if="card.error !== null" variant="danger" :title="card.error">storage error</BBadge>
                <BBadge v-else :variant="entryVariant(row.info?.state)" :title="errorTitle(row.info)">
                  {{ row.info?.state ?? '?' }}
                </BBadge>
              </td>
              <td class="col-log">
                <div class="inline-log">{{ row.preview }}</div>
              </td>
              <td class="col-actions-wide text-end" @click.stop>
                <BButton
                  size="sm"
                  variant="outline-danger"
                  :disabled="store.ended"
                  @click="store.clearCacheEntries([[card.name, row.name]])"
                >
                  clear
                </BButton>
                <BButton
                  size="sm"
                  variant="outline-success"
                  class="ms-1"
                  :disabled="store.ended"
                  @click="store.loadCacheEntries([[card.name, row.name]])"
                >
                  load
                </BButton>
                <BButton size="sm" variant="outline-secondary" class="ms-1" @click="viewing = [card.name, row.name]">
                  view
                </BButton>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="!shownCards.length" class="text-secondary text-center py-4">
        {{ store.caches.length ? 'No cache entry matches.' : 'No caches.' }}
      </div>
    </div>
    <CacheValueModal v-if="viewing" :cache-name="viewing[0]" :entry-name="viewing[1]" @hidden="viewing = null" />
  </div>
</template>
