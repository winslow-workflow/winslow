"""The shared surface of the session port, defined once. A transport module
implements both classes with these signatures (see winslow.client). Every
method takes values and returns values (see winslow.model).

The refusal contract: a read the server or the session refuses raises
RequestError with the served reason (see winslow.exceptions) - an unknown
key, an ended session, a bad query. Actions never raise; a refused action
answers an ack (see submit). A connection outage of the wire transport
raises ConnectionError or TimeoutError instead: the request never reached
a server, so there is no served reason (see winslow.client.websocket).
MisconfigurationError stays a composition error of the local process, for
example a LocalAppClient built without an orchestrator."""


class AppClient:
    """The dashboard scope: session rows, descriptors, open manifests,
    create and restore. It hands out SessionClients (see session)."""

    def sessions(self):
        """Return a SessionRow per live session."""
        raise NotImplementedError

    def descriptors(self):
        """Return the Descriptors of the process: the collected workflows
        with their form options, plus the orchestrator overrides."""
        raise NotImplementedError

    def manifests(self):
        """Return a ManifestRow per restorable session: the open manifests
        that name no live session."""
        raise NotImplementedError

    def create_session(self, workflow, overrides=None, values=None):
        """Build, initialize, persist and register one session. Return its
        SessionRow."""
        raise NotImplementedError

    def restore_session(self, session_id):
        """Re-create the session of an open manifest under its stored id,
        seeded from state. Return its SessionRow."""
        raise NotImplementedError

    def session(self, session_id):
        """Return the SessionClient of one live session."""
        raise NotImplementedError


class SessionClient:
    """One session: the reads, the subscriptions, and the actions."""

    # --- reads ---------------------------------------------------------------

    def snapshot(self):
        """Return the SessionSnapshot: statuses, batch rows, session log
        backlog, session meta."""
        raise NotImplementedError

    def roster(self):
        """Return a stub TaskInfo per task, in launch-filter order."""
        raise NotImplementedError

    def task_detail(self, key):
        """Return the full TaskInfo of one task, evaluated and with the
        trust fields filled (see Workflow.task_info)."""
        raise NotImplementedError

    def record_detail(self, batch_uuid, key):
        """Return the RecordDetail of one execution record."""
        raise NotImplementedError

    def history(self):
        """Return a HistoryRow per batch, with the per-task outcomes."""
        raise NotImplementedError

    def log_tail(self, batch_uuid, key, limit=200):
        """Return the last `limit` stored log lines of one record."""
        raise NotImplementedError

    def caches(self):
        """Return a CacheCard per cache of the session."""
        raise NotImplementedError

    def cache_value(self, cache_name, entry_name):
        """Return the CacheValueView of one entry: the rendered value, its
        encoding, and the error context."""
        raise NotImplementedError

    def apply_filter(self, query, builtin_only=False, scope="tasks"):
        """Return the identity keys the query matches over the named corpus,
        'tasks' or 'history' (see Workflow.filter_keys). A bad query raises
        RequestError with the parse error."""
        raise NotImplementedError

    def batch_options(self):
        """Return the baseline batch option values of the session as a dict.
        A fresh client prefills its toggles from it; each submit then carries
        the client's own values (see RunTasks.options)."""
        raise NotImplementedError

    def session_params(self):
        """Return the SessionParams: settings_snapshot plus the resolved
        workflow_config values."""
        raise NotImplementedError

    # --- subscriptions ---------------------------------------------------------

    def subscribe(self, topic, handler):
        """Connect the handler to one event topic of the session. The topics
        are the session bus event classes (see winslow.events) plus
        CacheUpdatedEvent and SessionLogEvent (see winslow.model). A handler
        can run on any thread. The dispatch order between handlers is
        undefined."""
        raise NotImplementedError

    def unsubscribe(self, topic, handler):
        """Disconnect the handler (see subscribe). An unknown pair is a
        no-op, so a teardown path can run twice."""
        raise NotImplementedError

    def subscribe_task_log(self, task_key, handler):
        """Connect the handler to the live log stream of one task, outside
        any batch, and return the buffered backlog lines. The handler
        receives TaskLogEvent values."""
        raise NotImplementedError

    def unsubscribe_task_log(self, task_key, handler):
        """Disconnect the task log handler (see subscribe_task_log)."""
        raise NotImplementedError

    def close(self):
        """Disconnect every subscription of this client."""
        raise NotImplementedError

    # --- actions ----------------------------------------------------------------

    def submit(self, action):
        """Submit one action dataclass (see winslow.actions) and return its
        ack. A refusal answers an ack with the reason, never a raise."""
        raise NotImplementedError
