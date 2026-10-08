import { createRouter, createWebHashHistory, type RouteLocationRaw } from 'vue-router'

import DashboardView from '@/views/DashboardView.vue'
import SessionView from '@/views/SessionView.vue'

// Hash URLs: the serve process hands out index.html at / alone, and the same
// dist works under any prefix of a host application.
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', name: 'dashboard', component: DashboardView },
    { path: '/sessions/:id', name: 'session', component: SessionView, props: true },
    // One execution record, and its info modal with the `info` tail. The batch
    // uuid and the identity key are [A-Za-z0-9_-] by construction (see
    // _NAME_PATTERN in src/winslow/_base.py), so the path needs no encoding.
    {
      path: '/sessions/:id/records/:batch/:task/:view(info)?',
      name: 'record',
      component: SessionView,
      props: true,
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

export function sessionLocation(id: string): RouteLocationRaw {
  return { name: 'session', params: { id } }
}

export function recordLocation(id: string, batch: string, task: string, info = false): RouteLocationRaw {
  return { name: 'record', params: { id, batch, task, ...(info ? { view: 'info' } : {}) } }
}

export default router
