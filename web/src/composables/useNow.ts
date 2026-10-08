// The wall clock in epoch seconds, refreshed on an interval while `when` holds.

import { ref } from 'vue'

import { useInterval } from './useInterval'

export function useNow(ms = 1000, when?: () => boolean) {
  const now = ref(Date.now() / 1000)
  useInterval(
    () => {
      now.value = Date.now() / 1000
    },
    ms,
    when,
  )
  return now
}
