// The session store of the screen, provided by SessionView and injected by
// its panes and modals, so a child sees one store without a prop.

import { inject, type InjectionKey } from 'vue'

import type { SessionStore } from './session'

export const SESSION_STORE: InjectionKey<SessionStore> = Symbol('session-store')

export function useSessionScope(): SessionStore {
  const store = inject(SESSION_STORE)
  if (store === undefined) {
    throw new Error('no session store is provided - render this component under SessionView.')
  }
  return store
}
