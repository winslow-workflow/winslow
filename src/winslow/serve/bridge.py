"""The EventBridge: one per session, the fan-out from the session bus to the
subscriber queues of the connections. The bus callbacks run on worker threads
and only enqueue scalar dicts; the drain task on the loop coalesces log lines
per task per tick, serializes each frame once, and stamps the only sequence
numbers. A subscriber holds a bounded deque: the fan-out drops the oldest
frame at the bound and counts the drop, and the sender disconnects a client
that stays behind a full window (see serve-spec, slice two)."""

import asyncio
import collections
import json
import queue
from dataclasses import asdict, dataclass, field
from functools import partial

from winslow.events import (
    BatchCompletedEvent,
    BatchCreatedEvent,
    BatchOptionsChangedEvent,
    ExecutionStatusEvent,
    LogLineEvent,
    SessionEndedEvent,
    TaskStatusEvent,
)
from winslow.logger import INLINE_FORMATTER, InteractiveLogHandler, get_task_dispatcher
from winslow.serve.wire import FrameTypes

FLUSH_TICK = 0.05

# The inbox item tags of the three lanes _drain coalesces before a frame
# reaches the wire: a log line, a session_log line, a task_log line. Not on
# the wire themselves - see FrameTypes.LOG_BATCH/SESSION_LOG_BATCH/
# TASK_LOG_BATCH for the frame type each lane fans out as.
_LOG = "log"
_SESSION_LOG = "session_log"
_TASK_LOG = "task_log"


@dataclass(eq=False)
class Subscription:
    """The outgoing queue of one connection for one session (or for the
    control frames of the connection). wake is shared per connection: one
    sender serves every subscription of its socket."""

    wake: asyncio.Event
    deque: collections.deque = None
    dropped: int = 0
    maxlen: int = field(default=10_000, repr=False)

    def __post_init__(self):
        if self.deque is None:
            self.deque = collections.deque(maxlen=self.maxlen)

    def push(self, payload):
        if len(self.deque) == self.deque.maxlen:
            self.dropped += 1
        self.deque.append(payload)
        self.wake.set()

    @property
    def behind_a_full_window(self):
        return self.dropped >= self.deque.maxlen


class EventBridge:
    """The event path of one session toward the connections. attach() wires
    the bus subscribers; the app starts one drain task per bridge on the
    loop; close() disconnects and cancels."""

    def __init__(self, session, qsize=10_000, owner=None):
        self.session = session
        self.session_id = session.session_id
        self.qsize = qsize
        # The Bridges container, so a retired bridge can drop its own entry.
        self.owner = owner
        self.seq = 0
        self._inbox = queue.SimpleQueue()
        self._subscriptions = []
        self._task = None
        self._pairs = (
            (TaskStatusEvent, self._on_task_status),
            (ExecutionStatusEvent, self._on_execution_status),
            (BatchCreatedEvent, self._on_batch_created),
            (BatchCompletedEvent, self._on_batch_completed),
            (LogLineEvent, self._on_log_line),
            (SessionEndedEvent, self._on_session_ended),
            (BatchOptionsChangedEvent, self._on_batch_options_changed),
        )
        # The session_log lane (see _on_session_log_line): a second handler,
        # not a bus subscription, because workflow.logger is a plain logger.
        self._session_log_handler = InteractiveLogHandler(
            self._on_session_log_line, formatter=INLINE_FORMATTER
        )
        # task_key -> {"log_key": str, "handler": InteractiveLogHandler,
        # "refcount": int}. More than one connection can subscribe to the
        # same task; the bridge keeps one dispatcher listener per task and
        # ref-counts it (see subscribe_task_log).
        self._task_log_listeners = {}

    def attach(self):
        for event, handler in self._pairs:
            self.session.workflow.subscribe(event, handler)
        self.session.workflow.add_cache_listener(self)
        self.session.workflow.logger.addHandler(self._session_log_handler)

    def start(self):
        self._task = asyncio.get_running_loop().create_task(self._drain())

    def detach(self):
        for event, handler in self._pairs:
            # The session-end sweep can beat this call; unsubscribe is a no-op
            # then (see SessionBus).
            self.session.workflow.unsubscribe(event, handler)
        self.session.workflow.remove_cache_listener(self)
        self.session.workflow.logger.removeHandler(self._session_log_handler)
        dispatcher = get_task_dispatcher()
        for entry in self._task_log_listeners.values():
            dispatcher.remove_listener(entry["log_key"], entry["handler"])
        self._task_log_listeners.clear()

    def close(self):
        self.detach()
        if self._task is not None:
            self._task.cancel()

    # --- the task_log lane: one dispatcher listener per subscribed task ------

    def subscribe_task_log(self, task_key, log_key):
        entry = self._task_log_listeners.get(task_key)
        if entry is not None:
            entry["refcount"] += 1
            return
        handler = InteractiveLogHandler(
            partial(self._on_task_log_line, task_key), formatter=INLINE_FORMATTER
        )
        get_task_dispatcher().add_listener(log_key, handler)
        self._task_log_listeners[task_key] = {
            "log_key": log_key,
            "handler": handler,
            "refcount": 1,
        }

    def unsubscribe_task_log(self, task_key):
        entry = self._task_log_listeners.get(task_key)
        if entry is None:
            return
        entry["refcount"] -= 1
        if entry["refcount"] <= 0:
            get_task_dispatcher().remove_listener(entry["log_key"], entry["handler"])
            del self._task_log_listeners[task_key]

    # --- the cache lane: duck-typed CacheListener callbacks -------------------
    # (see winslow.cache.CacheListener; ListenerMixin dispatches by method
    # name, so no explicit base class is necessary).

    def on_entry_computed(self, info, previous_state):
        self._inbox.put(
            {"type": FrameTypes.CACHE_UPDATED, "cache_name": info.cache_name}
        )

    def on_entries_invalidated(self, scope, dropped, trigger):
        for name in dropped:
            self._inbox.put({"type": FrameTypes.CACHE_UPDATED, "cache_name": name})

    def on_eager_population_started(self, scope, entries):
        for name in entries:
            self._inbox.put({"type": FrameTypes.CACHE_UPDATED, "cache_name": name})

    def on_eager_population_finished(self, scope, entries):
        for name in entries:
            self._inbox.put({"type": FrameTypes.CACHE_UPDATED, "cache_name": name})

    def on_entry_error(self, scope, cache_name, entry_name, error):
        self._inbox.put({"type": FrameTypes.CACHE_UPDATED, "cache_name": cache_name})

    def _retire(self):
        """The session ended and the frame is fanned out: release the bus
        handlers and the Bridges entry, so a long serve process does not
        accumulate one live drain task per ended session."""
        self.detach()
        if self.owner is not None:
            self.owner.discard(self.session_id)

    # --- the bus side: worker threads, scalars only ---------------------------

    def _on_task_status(self, event):
        self._inbox.put(
            {
                "type": FrameTypes.TASK_STATUS,
                "key": event.key,
                "status": event.status.name,
                "origin": event.origin.value,
            }
        )

    def _on_execution_status(self, event):
        self._inbox.put(
            {
                "type": FrameTypes.EXECUTION_STATUS,
                "task_key": event.task_key,
                "status": event.status.name,
                "batch_uuid": event.batch_uuid,
                "origin": event.origin.value,
            }
        )

    def _on_batch_created(self, event):
        self._inbox.put(
            {"type": FrameTypes.BATCH_CREATED, "batch": asdict(event.info)}
        )

    def _on_batch_completed(self, event):
        self._inbox.put(
            {"type": FrameTypes.BATCH_COMPLETED, "batch": asdict(event.info)}
        )

    def _on_log_line(self, event):
        self._inbox.put(
            {
                "type": _LOG,
                "task_key": event.task_key,
                "batch_uuid": event.batch_uuid,
                "line": event.line,
            }
        )

    def _on_session_ended(self, event):
        self._inbox.put({"type": FrameTypes.SESSION_ENDED})

    def _on_batch_options_changed(self, event):
        self._inbox.put(
            {"type": FrameTypes.BATCH_OPTIONS_CHANGED, "options": event.options}
        )

    def _on_session_log_line(self, line):
        self._inbox.put({"type": _SESSION_LOG, "line": line})

    def _on_task_log_line(self, task_key, line):
        self._inbox.put({"type": _TASK_LOG, "task_key": task_key, "line": line})

    # --- the loop side ---------------------------------------------------------

    def subscribe(self, subscription):
        if subscription not in self._subscriptions:
            self._subscriptions.append(subscription)

    def unsubscribe(self, subscription):
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)

    def snapshot(self):
        """The current state of the session, stamped with the sequence the
        events continue from. Runs on the loop between drain passes, so the
        stamp and the state cannot separate."""
        workflow = self.session.workflow
        return {
            "type": FrameTypes.SNAPSHOT,
            "session_id": self.session_id,
            "seq": self.seq,
            "workflow": str(workflow),
            "status": self.session.status.name,
            "tasks": {key: status.name for key, status in workflow.store.items()},
            "batches": [
                {
                    "uuid": batch.uuid,
                    "action": batch.action.name,
                    "status": batch.status.name,
                    "task_count": batch.task_count,
                    "created_at": batch.created_at.timestamp(),
                    "completed_at": (
                        batch.completed_at.timestamp() if batch.completed_at else None
                    ),
                }
                for batch in workflow.runner.batches
            ],
        }

    def _fan_out(self, frame):
        self.seq += 1
        frame["session_id"] = self.session_id
        frame["seq"] = self.seq
        payload = json.dumps(frame)
        for subscription in self._subscriptions:
            subscription.push(payload)

    async def _drain(self):
        while True:
            batch = []
            while True:
                try:
                    batch.append(self._inbox.get_nowait())
                except queue.Empty:
                    break
            logs = {}
            session_log_lines = []
            task_logs = {}
            # A dict, not a set: iteration must keep the first-seen order,
            # and a plain dict does that for free.
            cache_names = {}
            others = []
            for item in batch:
                if item["type"] == _LOG:
                    logs.setdefault((item["task_key"], item["batch_uuid"]), []).append(
                        item["line"]
                    )
                elif item["type"] == _SESSION_LOG:
                    session_log_lines.append(item["line"])
                elif item["type"] == _TASK_LOG:
                    task_logs.setdefault(item["task_key"], []).append(item["line"])
                elif item["type"] == FrameTypes.CACHE_UPDATED:
                    cache_names[item["cache_name"]] = None
                else:
                    others.append(item)
            for (task_key, batch_uuid), lines in logs.items():
                self._fan_out(
                    {
                        "type": FrameTypes.LOG_BATCH,
                        "task_key": task_key,
                        "batch_uuid": batch_uuid,
                        "lines": lines,
                    }
                )
            if session_log_lines:
                self._fan_out(
                    {"type": FrameTypes.SESSION_LOG_BATCH, "lines": session_log_lines}
                )
            for task_key, lines in task_logs.items():
                self._fan_out(
                    {
                        "type": FrameTypes.TASK_LOG_BATCH,
                        "task_key": task_key,
                        "lines": lines,
                    }
                )
            for cache_name in cache_names:
                self._fan_out(
                    {"type": FrameTypes.CACHE_UPDATED, "cache_name": cache_name}
                )
            # After the logs of the same pass: a control event must not
            # overtake the lines that precede it.
            for item in others:
                self._fan_out(item)
            if any(item["type"] == FrameTypes.SESSION_ENDED for item in others):
                self._retire()
                return
            await asyncio.sleep(FLUSH_TICK)
