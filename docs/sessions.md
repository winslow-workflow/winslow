# Sessions and restore

A session is one live execution of a workflow. The engine persists the state of every session as
it runs, under `winslow serve` and `winslow run` alike: the inputs that rebuild it, the verified
outcome of every task, and the batches in flight. Kill the process mid-batch, start it again, and
the session comes back seeded to where it was.

Persistence is on by default. Three things are yours to decide: [where the state lives](#where-the-state-lives),
[how long a verified success stays trusted](#trust-a-verified-success-check_ttl), and
[when to restore](#restore-a-session).

## Where the state lives

The state directory is `.winslow/state` under the working directory. Move it with one setting:

```bash
export WINSLOW_STATE_DIR=/var/lib/winslow/state
```

`open/` holds the live sessions. A session that ends moves to `ended/` and a session that fails
moves to `error/`. Restore reads `open/` only, so the archives grow without a cost at startup. A
deployment that wants the records in a database registers [another backend](#another-backend) and
selects it with `WINSLOW_STATE_BACKEND`.

## Trust a verified success: check_ttl

A restored success re-probes on its first touch: the session that verified it is gone, and a
check is cheap by design. `check_ttl` keeps the trust for a number of seconds instead. Declare it
on the workflow as the default of its tasks, and on a task as the override:

```python title="workflows/etl/workflow.py"
from winslow import Workflow


class Etl(Workflow):
    check_ttl = 3600  # trust every verified success for one hour
```

```python title="workflows/etl/tasks/extract.py"
from winslow import Task


class Extract(Task):
    check_ttl = 300  # this source moves fast: trust it for five minutes

    def check(self):
        ...
```

A passing task younger than its TTL counts as verified: a run batch skips it, and a dependent task
sees its dependency met. The stamp survives the process, so the window spans a kill and a restore
of the same session. A check batch runs `check()` on every task it names, TTL or not.

## Restore a session

At start the dashboard lists every open session in a Restore pane, one row per session, with a
restore-all button when there is more than one. Pick a row. The session comes back under its
original id as an ordinary workflow screen, and every action works on it:

- A task with a trusted success shows its recorded status.
- A task with a success past its trust window shows `STALE`. Its next touch re-verifies it through
  the normal check, so a run batch probes it before it skips the run.
- A failure shows as it was.
- A batch the dead process left running shows in History as `INTERRUPTED`, with the options it ran
  under. A new batch re-verifies its tasks before any rerun.

### Restore in serve mode

Under `winslow serve` the engine restores every open session at startup, before the server accepts
a connection, and a workflow with `auto_init = True` starts its session at the same point when no
restored session runs it already. A terminal that connects sees the sessions in place, and a dashboard already connected
adopts a new session on its next poll (see [Serve and connect](serve.md)).

A headless run is its own complete lifecycle and writes no session state. The per-session log
files under `WINSLOW_LOG_DIR` are the complete log stream in every mode.

## How it works

### The records

One directory per session, written through the state store:

- **The manifest**: the workflow name, the origin of the run, and the effective workflow values,
  every declared option resolved from the form and the command line. It rebuilds the session in a
  process started with other arguments.
- **Status snapshots**: one file per task, named by the task identity key, with the latest terminal
  status and the time of the check. Each terminal transition replaces the file atomically.
  Snapshots belong to their session: a fresh session starts with zero trust, and a restore under the
  same session id inherits them.
- **Batch records**: one directory per batch with a `record.json` written on submit and stamped at
  the close, the batch options it ran under, the roster of task keys, and one log file per task
  captured at the close.

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

Writes are strict JSON and publish atomically. A file that fails to read counts as missing, so a
damaged directory restores as a cold start.

### The restore steps

1. Initialize the workflow from the manifest values, under the original session id.
2. Initialize the tasks and run the eligibility pass, like a fresh start. The real world at
   restore time decides which tasks the session holds.
3. Seed every `READY_TO_PROCESS` task from its snapshot. A failure seeds as recorded, a success
   within its TTL seeds as recorded, and a success past its TTL seeds as `STALE`.
4. Register the batches left open as `INTERRUPTED` in History.

The seeds arrive as normal store events, so every pane renders them the way it renders a live
transition.

### STALE

`STALE` is a task status. A sweeper thread of the session flips a passing status to `STALE` when
its TTL lapses while the session runs, and a restore seeds an untrusted success as `STALE`. The
snapshot keeps the real outcome and its check time, and the task detail shows both. `checked_at`
is a wall-clock epoch stamp; TTLs run minutes to hours, so clock skew between machines that share
a state volume leaves the decisions unchanged.

### Another backend

A package registers a backend the way telemetry backends register. The backend is constructed once
per process with the orchestrator config, and the workflow identity arrives in the records:

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

The natural database mapping is one row per record, with a status column where the file backend
uses the three directories.
