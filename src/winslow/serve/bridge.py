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

from winslow.events import (
    BatchCompletedEvent,
    BatchCreatedEvent,
    ExecutionStatusEvent,
    LogLineEvent,
    SessionEndedEvent,
    TaskStatusEvent,
)

FLUSH_TICK = 0.05


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

    def __init__(self, session, qsize=10_000):
        self.session = session
        self.session_id = session.session_id
        self.qsize = qsize
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
        )

    def attach(self):
        for event, handler in self._pairs:
            self.session.workflow.subscribe(event, handler)

    def start(self):
        self._task = asyncio.get_running_loop().create_task(self._drain())

    def close(self):
        for event, handler in self._pairs:
            # The session-end sweep can beat this call; unsubscribe is a no-op
            # then (see SessionBus).
            self.session.workflow.unsubscribe(event, handler)
        if self._task is not None:
            self._task.cancel()

    # --- the bus side: worker threads, scalars only ---------------------------

    def _on_task_status(self, event):
        self._inbox.put(
            {
                "type": "task_status",
                "key": event.key,
                "status": event.status.name,
                "origin": event.origin.value,
            }
        )

    def _on_execution_status(self, event):
        self._inbox.put(
            {
                "type": "execution_status",
                "task_key": event.task_key,
                "status": event.status.name,
                "batch_uuid": event.batch_uuid,
                "origin": event.origin.value,
            }
        )

    def _on_batch_created(self, event):
        self._inbox.put({"type": "batch_created", "batch": asdict(event.info)})

    def _on_batch_completed(self, event):
        self._inbox.put({"type": "batch_completed", "batch": asdict(event.info)})

    def _on_log_line(self, event):
        self._inbox.put(
            {
                "type": "log",
                "task_key": event.task_key,
                "batch_uuid": event.batch_uuid,
                "line": event.line,
            }
        )

    def _on_session_ended(self, event):
        self._inbox.put({"type": "session_ended"})

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
            "type": "snapshot",
            "session_id": self.session_id,
            "seq": self.seq,
            "workflow": str(workflow),
            "status": self.session.status.name,
            "tasks": {key: status.name for key, status in workflow.store.current.items()},
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
            logs, others = {}, []
            for item in batch:
                if item["type"] == "log":
                    logs.setdefault((item["task_key"], item["batch_uuid"]), []).append(
                        item["line"]
                    )
                else:
                    others.append(item)
            for (task_key, batch_uuid), lines in logs.items():
                self._fan_out(
                    {
                        "type": "log_batch",
                        "task_key": task_key,
                        "batch_uuid": batch_uuid,
                        "lines": lines,
                    }
                )
            # After the logs of the same pass: a control event must not
            # overtake the lines that precede it.
            for item in others:
                self._fan_out(item)
            await asyncio.sleep(FLUSH_TICK)
