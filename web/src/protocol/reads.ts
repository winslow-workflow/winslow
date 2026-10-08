// One method per Read declaration of src/winslow/client/base.py, typed by its
// result, plus one method per action of src/winslow/actions.py.

import type {
  Ack,
  BatchOptions,
  BatchOutcome,
  CacheInfo,
  CacheValueView,
  Descriptors,
  HostStats,
  ManifestInfo,
  RecordDetail,
  SessionInfo,
  SessionParams,
  SessionSnapshot,
  TaskInfo,
} from './model'
import type { TaskLogHandler, Wire } from './wire'

export class AppReads {
  constructor(readonly wire: Wire) {}

  sessions(): Promise<SessionInfo[]> {
    return this.wire.request('sessions') as Promise<SessionInfo[]>
  }

  descriptors(): Promise<Descriptors> {
    return this.wire.request('descriptors') as Promise<Descriptors>
  }

  manifests(): Promise<ManifestInfo[]> {
    return this.wire.request('manifests') as Promise<ManifestInfo[]>
  }

  hostStats(): Promise<HostStats> {
    return this.wire.request('host_stats') as Promise<HostStats>
  }

  createSession(
    workflow: string,
    overrides: Record<string, unknown> | null,
    values: Record<string, unknown> | null,
  ): Promise<SessionInfo> {
    return this.wire.request('create_session', { workflow, overrides, values }) as Promise<SessionInfo>
  }

  restoreSession(sessionId: string): Promise<SessionInfo> {
    return this.wire.request('restore_session', { session_id: sessionId }) as Promise<SessionInfo>
  }
}

export class SessionReads {
  constructor(
    readonly wire: Wire,
    readonly sessionId: string,
  ) {}

  private read(kind: string, fields: Record<string, unknown> = {}): Promise<unknown> {
    return this.wire.request(kind, fields, this.sessionId)
  }

  snapshot(): Promise<SessionSnapshot> {
    return this.read('snapshot') as Promise<SessionSnapshot>
  }

  roster(): Promise<TaskInfo[]> {
    return this.read('roster') as Promise<TaskInfo[]>
  }

  taskDetail(taskKey: string): Promise<TaskInfo> {
    return this.read('task_detail', { task_key: taskKey }) as Promise<TaskInfo>
  }

  recordDetail(batchUuid: string, taskKey: string): Promise<RecordDetail> {
    return this.read('record_detail', { batch_uuid: batchUuid, task_key: taskKey }) as Promise<RecordDetail>
  }

  history(): Promise<BatchOutcome[]> {
    return this.read('history') as Promise<BatchOutcome[]>
  }

  logTail(batchUuid: string, taskKey: string, limit: number | null = null): Promise<string[]> {
    return this.read('log_tail', { batch_uuid: batchUuid, task_key: taskKey, limit }) as Promise<string[]>
  }

  caches(): Promise<CacheInfo[]> {
    return this.read('caches') as Promise<CacheInfo[]>
  }

  cacheValue(cacheName: string, entryName: string): Promise<CacheValueView> {
    return this.read('cache_value', { cache_name: cacheName, entry_name: entryName }) as Promise<CacheValueView>
  }

  applyFilter(query: string, scope: 'tasks' | 'history' = 'tasks'): Promise<string[]> {
    return this.read('apply_filter', { query, scope }) as Promise<string[]>
  }

  batchOptions(): Promise<BatchOptions> {
    return this.read('batch_options') as Promise<BatchOptions>
  }

  sessionParams(): Promise<SessionParams> {
    return this.read('session_params') as Promise<SessionParams>
  }

  // --- the task log stream -------------------------------------------------------

  // The buffered backlog of one task log; the handler receives the live lines.
  subscribeTaskLog(taskKey: string, handler: TaskLogHandler): Promise<string[]> {
    return this.wire.subscribeTaskLog(this.sessionId, taskKey, handler)
  }

  unsubscribeTaskLog(taskKey: string, handler: TaskLogHandler): void {
    this.wire.unsubscribeTaskLog(this.sessionId, taskKey, handler)
  }

  // --- actions -------------------------------------------------------------------

  private act(action: string, fields: Record<string, unknown>): Promise<Ack> {
    return this.wire.action(this.sessionId, action, fields)
  }

  runTasks(keys: string[], options: BatchOptions | null = null): Promise<Ack> {
    return this.act('run_tasks', { keys, options })
  }

  checkTasks(keys: string[], options: BatchOptions | null = null): Promise<Ack> {
    return this.act('check_tasks', { keys, options })
  }

  stopBatch(batchUuid: string): Promise<Ack> {
    return this.act('stop_batch', { batch_uuid: batchUuid })
  }

  endSession(force = false): Promise<Ack> {
    return this.act('end_session', { force })
  }

  loadCacheEntries(entries: [string, string][]): Promise<Ack> {
    return this.act('load_cache_entries', { entries })
  }

  clearCacheEntries(entries: [string, string][]): Promise<Ack> {
    return this.act('clear_cache_entries', { entries })
  }
}
