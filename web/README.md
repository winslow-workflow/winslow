# winslow web UI

The browser UI of `winslow serve`: the dashboard and the session screens of the terminal UI, drawn
from the same snapshots and events over the websocket endpoint. Vue 3, TypeScript, Pinia, Vue Router
and Bootstrap 5 with bootstrap-vue-next.

## Develop

Node 24 (see `.nvmrc`). Start a serve process in a project, for example `winslow-sample`, then the
dev server:

```bash
cd winslow-sample && uv run winslow serve
cd winslow/web && npm install && npm run dev
```

`http://localhost:5173/` opens the UI. The dev server proxies `/ws` to `ws://127.0.0.1:8866` and
presents the serve origin on the proxied hello, so the serve process needs no `WINSLOW_ORIGINS`
entry. `WINSLOW_WS=ws://host:port npm run dev` proxies to another serve process.

```bash
npm run lint        # eslint
npm run type-check  # vue-tsc
npm test            # vitest
npm run build       # type-check, then the dist into ../src/winslow/serve/static
```

## Layout

| path | holds |
|---|---|
| `src/protocol/` | the frames, the model types, the wire (connect, hello, requests, lanes, reconnect), the typed reads, the status tables |
| `src/stores/` | Pinia: `connection` (the one wire), `sessions` (the dashboard scope), `session` (one store per session id), `notices` (toasts and the client log) |
| `src/views/` | `DashboardView` and `SessionView` |
| `src/components/dashboard/` | the workflow list, the start form, the session and restore rows, the force end modal |
| `src/components/session/` | the tasks, history and caches panes, the overview panes, the detail modals |

`src/protocol/frames.ts` lists every wire type in `FRAME_TYPES`. `tests/test_serve_web.py` compares
the list with the Python frame registry.

## The dist

`npm run build` writes `../src/winslow/serve/static/`. The serve process hands out its `index.html`
at `/` and its `assets/` under `/assets/`. The router uses hash URLs, so the same dist works at `/`
in serve mode and under any prefix of a host application. `base` is `./` for the same reason.

The page reads `window.WINSLOW` for its configuration: `wsUrl` (default: `/ws` on the page origin),
`ticket` and `ticketUrl` (see `docs/serve.md`).
