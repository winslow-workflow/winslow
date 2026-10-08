// The notices of the client: toasts for refusals and outcomes, and the client
// log the dashboard shows. A store, so a Pinia store can post one without a
// component context; NoticeHost renders the queue.

import { defineStore } from 'pinia'
import { ref } from 'vue'

import { messageOf } from '@/protocol/errors'
import type { Variant } from '@/protocol/status'
import { ConnectionDown, RequestError } from '@/protocol/wire'

export interface Notice {
  id: number
  title: string | null
  body: string
  variant: Variant
  // A server traceback or a validation report, opened from the toast.
  detail: string | null
}

export interface LogLine {
  at: number
  level: 'info' | 'warning' | 'error'
  text: string
}

const LOG_CAP = 500

export const useNoticesStore = defineStore('notices', () => {
  const queue = ref<Notice[]>([])
  const log = ref<LogLine[]>([])
  // The count of lines the cap removed from the front, so a view keys by line number.
  const logTrimmed = ref(0)
  let ids = 0

  function record(level: LogLine['level'], text: string) {
    log.value.push({ at: Date.now() / 1000, level, text })
    const excess = log.value.length - LOG_CAP
    if (excess > 0) {
      log.value.splice(0, excess)
      logTrimmed.value += excess
    }
  }

  function push(
    body: string,
    variant: Variant = 'secondary',
    title: string | null = null,
    detail: string | null = null,
  ) {
    queue.value.push({ id: ++ids, title, body, variant, detail })
    record(
      variant === 'danger' ? 'error' : variant === 'warning' ? 'warning' : 'info',
      title ? `${title}: ${body}` : body,
    )
  }

  function dismiss(id: number) {
    queue.value = queue.value.filter((notice) => notice.id !== id)
  }

  const warn = (body: string, title: string | null = null) => push(body, 'warning', title)
  const error = (body: string, title: string | null = null, detail: string | null = null) =>
    push(body, 'danger', title, detail)

  // The one read failure triage: an outage is silent, the transport reconnects
  // on its own; a served refusal warns; anything else is an error.
  function report(failure: unknown, quiet = false) {
    if (failure instanceof ConnectionDown || quiet) return
    if (failure instanceof RequestError) warn(failure.message)
    else error(messageOf(failure))
  }

  return {
    queue,
    log,
    logTrimmed,
    push,
    dismiss,
    info: (body: string, title: string | null = null) => push(body, 'secondary', title),
    success: (body: string, title: string | null = null) => push(body, 'success', title),
    warn,
    error,
    report,
    record,
  }
})
