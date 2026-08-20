# Sessions and restore

A session is one live execution of a workflow: it owns the task store, the batches, the caches and
the logging of that run. When a session runs with a state store, Winslow persists everything a
future process needs to pick the run back up: the inputs that rebuild the session, the verified
outcome of every task, and the batches that were in flight. Kill the TUI mid-batch, relaunch, and
the dashboard offers the session back, seeded to where it was.

This page covers what persists and where, the [`check_ttl`](#trust-a-verified-success-check_ttl)
declaration that lets a restored success count without a new probe, how a stale success
[renders and re-probes](#stale), and the [restore flow](#restore-in-the-tui) in the TUI.

## What persists

Everything durable about one session lives in one directory, written through one adapter, the state
store. The directory holds three record kinds:

- **The manifest** — the workflow name, the configuration values the session started with, and the
  origin of the run: the inputs that rebuild the session. When you change a batch option toggle in
  the workflow screen, the manifest updates, so a restore reproduces the toggles you set.
- **Status snapshots** — one file per task, named by the task identity key, holding the latest terminal
  status (`COMPLETED`, `COMPLETED_WITH_ERROR`, `COMPLETED_PREVIOUSLY`, `FORCE_SUCCESS`, `FAILED`,
  `ERROR`) and the time of the check. Each terminal transition replaces the file atomically. The
  writes drain through a writer thread of the session, and the close of a batch waits for them, so
  a closed batch record implies that its snapshots are on disk. Snapshots are session-scoped: a
  check result is evidence gathered under one session's configuration, so a fresh session always
  starts with zero trust, and only a restore under the same session id inherits the snapshots.
- **Batch records** — one directory per batch: a `record.json` written on submit and stamped at the
  close, plus a log dump. The record carries the audit trail of the batch: the action, the batch
  option snapshot it ran under, both timestamps, and the roster of task identity keys with their
  labels. The task statuses live only in the snapshots. At the close, the captured log lines of each
  task land beside the record, one file per task.

## The state directory

The file backend is the default. It writes under `WINSLOW_STATE_DIR` (default `.winslow/state`,
relative to the working directory). `open/` holds the live sessions. The end of a session stamps
the manifest with `ended_at` and an outcome, then moves the whole session directory: a clean end
into `ended/`, the audit archive, and a session that failed into `error/`, with the same structure:

```
.winslow/state/
├── open/
│   └── etl-20260819T091502-a1b2c3d4/
│       ├── manifest.json
│       ├── tasks/extract-9f2b41c7.json
│       └── batches/6f1d2c88-.../
│           ├── record.json
│           └── logs/extract-9f2b41c7.log
├── ended/
│   └── etl-20260818T140011-99ffe012/...
└── error/
    └── etl-20260817T173045-5e6f7a8b/...
```

Restore reads only `open/`, so the archives grow without a cost to startup. Writes are strict JSON
and publish atomically. A corrupt or unreadable file reads as missing, so a damaged state directory
degrades to a cold start, never to an error.

A package can register another backend, the way telemetry backends register, and a deployment
selects it with `WINSLOW_STATE_BACKEND`. Each backend is constructed with the orchestrator config
of the run, once per process at app start; workflow identity arrives in the records it stores. The
natural database mapping is one row per record with a status column where the file backend uses the
directories:

```python
from winslow.state import StateStore, register_state_backend


class DatabaseStateStore(StateStore):
    def __init__(self, orchestrator_config):
        super().__init__(orchestrator_config)
        self.pool = connect(orchestrator_config.database_url)

    ...


register_state_backend("database", DatabaseStateStore)
```

```bash
export WINSLOW_STATE_BACKEND=database
```

## Trust a verified success: check_ttl

By default a check snapshot is trusted only while the session that produced it stays live: every
restored success seeds as [STALE](#stale) and re-probes on first touch. Checks are cheap by design,
and workflows are paranoid by design.

`check_ttl` relaxes this per declaration. It is a number of seconds, declared on the workflow as
the default for its tasks, and on a task as the override:

```python title="workflows/etl/workflow.py"
from winslow import Workflow


class Etl(Workflow):
    check_ttl = 3600  # trust every verified success for one hour
```

```python title="workflows/etl/tasks/extract.py"
from winslow import Task


class Extract(Task):
    check_ttl = 300  # this source moves fast - trust it for five minutes only

    def check(self):
        ...
```

The rule is uniform wherever a check would run: the check pass, dependency resolution, and the
pre-run completion check all consult the session's snapshots first. A passing snapshot younger than
the effective TTL counts as verified, the runner sets that status without calling `check()`, and
the snapshot keeps its original check time. Because the snapshots survive a process death, the trust
window spans a kill and a restore of the same session.

## STALE

STALE is a task status. A passing status turns STALE when its snapshot is older than the effective
TTL, or, with no TTL, when it predates the current session. A sweeper thread of the session flips
a status whose TTL lapses while the session runs, and a restore seeds an untrusted success directly
as STALE. The snapshot keeps the real outcome and its check time; the task detail modal shows both.
A STALE task is not passing: its next touch re-verifies it through the normal completion check, and
a run batch re-verifies it before it skips the run. The persistent cache tiers keep that re-check
burst cheap (see [Caching](caching.md)).

## Restore in the TUI

At app start the dashboard lists every open manifest in a Restore pane, one row per session, with
a restore-all button when there is more than one. A restore rebuilds the session in four steps:

1. Initialize the workflow from the manifest inputs, under the original session id.
2. Initialize the tasks and run the eligibility pass, exactly like a fresh start. The pass reads
   the real world at restore time, and the real world is the golden source: it decides which tasks
   the session holds, whatever they were before the death.
3. Seed every task the eligibility pass left `READY_TO_PROCESS` from the snapshots: failures seed
   as they are, a trusted success seeds with its recorded status, and an untrusted success seeds
   as `STALE`.
4. Register the batches the dead process left open as `INTERRUPTED` in history, with the option
   snapshot their records preserved. An interrupted batch never continues: a roster task that has
   no snapshot comes back as `READY_TO_PROCESS`, and a new batch re-verifies it through the normal
   pre-run check before any rerun.

The restored screen is an ordinary workflow screen: the seeds arrived as normal store events, and
every action works. A restore that cannot initialize surfaces on the dashboard exactly like a
failed start.

A headless run is its own complete lifecycle: it neither writes session state nor consults it. The
per-session log files (`WINSLOW_LOG_DIR`) remain the complete log stream in every mode; the batch
log dump is a per-batch capture for the archive, taken at the close, so an interrupted batch
archives without one.

## Clock notes

`checked_at` is a wall-clock epoch stamp. TTLs in practice are minutes to hours, so NTP-scale
clock skew between machines that share a state volume does not change the decisions.
