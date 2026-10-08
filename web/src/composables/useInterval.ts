// One interval bound to the component: it runs while `when` holds (always,
// without one) and stops on unmount.

import { onBeforeUnmount, onMounted, watch } from 'vue'

export function useInterval(fn: () => void, ms: number, when?: () => boolean, immediate = true) {
  let timer: ReturnType<typeof setInterval> | null = null

  function start() {
    if (timer !== null) return
    if (immediate) fn()
    timer = setInterval(fn, ms)
  }

  function stop() {
    if (timer === null) return
    clearInterval(timer)
    timer = null
  }

  if (when) {
    watch(when, (on) => (on ? start() : stop()), { immediate: true })
  } else {
    onMounted(start)
  }
  onBeforeUnmount(stop)

  return { start, stop }
}
