<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'

import HostStatsPane from '@/components/common/HostStatsPane.vue'
import LogView from '@/components/common/LogView.vue'
import FailedRow from '@/components/dashboard/FailedRow.vue'
import RestorableRow from '@/components/dashboard/RestorableRow.vue'
import SessionRow from '@/components/dashboard/SessionRow.vue'
import WorkflowForm from '@/components/dashboard/WorkflowForm.vue'
import WorkflowList from '@/components/dashboard/WorkflowList.vue'
import { formatClock } from '@/protocol/format'
import type { FormValues } from '@/protocol/forms'
import { NO_PERMISSION_MESSAGE } from '@/protocol/messages'
import type { ManifestInfo } from '@/protocol/model'
import { workflowRows } from '@/protocol/workflows'
import { useConnectionStore } from '@/stores/connection'
import { useNoticesStore } from '@/stores/notices'
import { useSessionsStore } from '@/stores/sessions'

const sessions = useSessionsStore()
const notices = useNoticesStore()
const connection = useConnectionStore()
const router = useRouter()

const selected = ref<string | null>(null)
const rows = computed(() => workflowRows(sessions.descriptors?.workflows ?? []))
const descriptor = computed(
  () => sessions.descriptors?.workflows.find((row) => row.workflow === selected.value) ?? null,
)
const overrides = computed(() => sessions.descriptors?.overrides ?? [])

// A restore is a start of the manifest's workflow.
function canRestore(manifest: ManifestInfo): boolean {
  return connection.allows(manifest.permission_scope, 'start_workflow')
}
const canRestoreAll = computed(() => sessions.manifests.some(canRestore))
const tab = ref('sessions')

async function start(values: FormValues) {
  if (descriptor.value === null) return
  const info = await sessions.createSession(descriptor.value.workflow, values.orchestrator, values.workflow)
  if (info !== null) await router.push({ name: 'session', params: { id: info.session_id } })
}
</script>

<template>
  <div class="dashboard-layout">
    <div class="dashboard-top">
      <WorkflowList :rows="rows" :selected="selected" @select="selected = $event" />
      <div class="pane">
        <WorkflowForm v-if="descriptor" :descriptor="descriptor" :overrides="overrides" @start="start" />
        <div v-else class="d-flex align-items-center justify-content-center h-100 text-secondary">
          Choose a workflow to see the parameter form.
        </div>
      </div>
      <div class="pane">
        <BTabs v-model="tab" small>
          <BTab id="sessions" title="Sessions">
            <div class="pane-body">
              <div v-for="item in sessions.pending" :key="item.id" class="session-row d-flex align-items-center gap-2">
                <BSpinner small />
                <span>{{ item.workflow }}: Initializing...</span>
              </div>
              <SessionRow v-for="row in sessions.live" :key="row.session_id" :row="row" />
              <div v-if="!sessions.live.length && !sessions.pending.length" class="text-secondary text-center py-4">
                No sessions created yet.
              </div>
            </div>
          </BTab>
          <BTab id="history" title="History">
            <div class="pane-body">
              <FailedRow
                v-for="row in sessions.failed"
                :key="row.id"
                :row="row"
                @dismiss="sessions.dismissFailed(row.id)"
              />
              <SessionRow v-for="row in sessions.ended" :key="row.session_id" :row="row" />
              <div v-if="!sessions.ended.length && !sessions.failed.length" class="text-secondary text-center py-4">
                No ended sessions yet.
              </div>
            </div>
          </BTab>
          <BTab id="restore" title="Restore">
            <div class="pane-body">
              <div v-if="sessions.manifests.length > 1" class="px-3 pt-2">
                <BButton
                  size="sm"
                  variant="outline-primary"
                  :disabled="!canRestoreAll"
                  :title="canRestoreAll ? undefined : NO_PERMISSION_MESSAGE"
                  @click="sessions.restoreAll()"
                >
                  Restore all
                </BButton>
              </div>
              <RestorableRow
                v-for="manifest in sessions.manifests"
                :key="manifest.session_id"
                :manifest="manifest"
                :disabled="!canRestore(manifest)"
                @restore="sessions.restoreSession(manifest)"
              />
              <div v-if="!sessions.manifests.length" class="text-secondary text-center py-4">
                No sessions to restore.
              </div>
            </div>
          </BTab>
        </BTabs>
      </div>
    </div>
    <div class="pane-row">
      <div class="pane">
        <div class="pane-title">Logs</div>
        <LogView :lines="notices.log" :offset="notices.logTrimmed" empty="Nothing logged yet.">
          <template #default="{ line }">
            <span :class="{ 'text-warning': line.level === 'warning', 'text-danger': line.level === 'error' }">
              {{ formatClock(line.at) }} {{ line.text }}
            </span>
          </template>
        </LogView>
      </div>
      <div class="pane">
        <HostStatsPane />
      </div>
    </div>
  </div>
</template>
