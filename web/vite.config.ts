import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import vueDevTools from 'vite-plugin-vue-devtools'
import Components from 'unplugin-vue-components/vite'
import { BootstrapVueNextResolver } from 'bootstrap-vue-next/resolvers'

// The serve process the dev server proxies /ws to.
const SERVE_URL = process.env.WINSLOW_WS ?? 'ws://127.0.0.1:8866'
const SERVE_ORIGIN = SERVE_URL.replace(/^ws/, 'http')

export default defineConfig({
  // Relative asset paths, so the same dist works at / in serve mode and under
  // a prefix in a host application.
  base: './',
  plugins: [
    vue(),
    vueDevTools(),
    // dirs: [] keeps the auto registration to bootstrap-vue-next; the project
    // components are imported explicitly, so a typo in a tag fails the build.
    Components({ dirs: [], resolvers: [BootstrapVueNextResolver()], dts: 'src/components.d.ts' }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    // The filter help imports src/winslow/filter/syntax.md from the repo.
    fs: { allow: ['..'] },
    proxy: {
      '/ws': {
        target: SERVE_URL,
        ws: true,
        changeOrigin: true,
        configure(proxy) {
          // The hello Origin rule holds on loopback. The proxy presents the
          // serve origin, so the dev server needs no WINSLOW_ORIGINS entry.
          proxy.on('proxyReqWs', (proxyReq) => {
            proxyReq.setHeader('origin', SERVE_ORIGIN)
          })
        },
      },
    },
  },
  build: {
    outDir: '../src/winslow/serve/static',
    emptyOutDir: true,
  },
})
