"""One EventBridge per session fans the session events out to the FrameQueues
of the connections. The bridge subscribes through the local port, so the wire
hears every topic the way the local TUI does (see LocalSessionClient)."""

import asyncio
import collections
import queue
from dataclasses import dataclass, field

from winslow.client.local import LocalSessionClient
from winslow.model import SessionSnapshot
from winslow.protocol.frames import SnapshotFrame, encode_frame
from winslow.protocol.lanes import Lane, SessionEndedLane, TaskLogBatchLane, encode_tick

FLUSH_TICK = 0.05


@dataclass(eq=False)
class FrameQueue:
    """The outgoing frame queue of one connection, for one session or for the
    control frames. The connection shares one wake event across its queues, so
    one sender task serves them all. A full deque drops its oldest frame and
    counts the drop (see behind_a_full_window)."""

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
    """The event path of one session toward its connections. The port handlers
    run on worker threads and only enqueue. The drain task runs on the loop: it
    stamps the sequence number, encodes each frame once, and pushes it to every
    queue."""

    def __init__(self, session, qsize=10_000, owner=None):
        self.session = session
        self.session_id = session.session_id
        self.qsize = qsize
        # The Bridges container. A retired bridge removes its own entry (see _retire).
        self.owner = owner
        self.seq = 0
        self._inbox = queue.SimpleQueue()
        self._queues = []
        self._task = None
        self._client = LocalSessionClient(session)
        # Task key -> the queues that stream its log. The client streams a key
        # exactly while the key has an entry (see _release_task_log).
        self._task_log_queues = {}

    def attach(self):
        # Every lane enqueues its event as it is. The drain encodes (see
        # winslow.protocol.lanes). The task log lane subscribes per task.
        for lane in Lane.by_type.values():
            if lane is not TaskLogBatchLane:
                self._client.subscribe(lane.event, self._inbox.put)

    def start(self):
        self._task = asyncio.get_running_loop().create_task(self._drain())

    def detach(self):
        # The session end sweep can run before this call. A bus unsubscribe is
        # then a no-op (see SessionBus).
        self._client.close()
        self._task_log_queues.clear()

    def close(self):
        self.detach()
        if self._task is not None:
            self._task.cancel()

    # --- the task_log lane: the streams follow the attached queues ------------

    def subscribe_task_log(self, frame_queue, task_key):
        """Stream one task log to the queue and return its backlog lines. The
        client subscribes a key once and answers the backlog on every call."""
        backlog = self._client.subscribe_task_log(task_key, self._inbox.put)
        self._task_log_queues.setdefault(task_key, set()).add(frame_queue)
        return backlog

    def unsubscribe_task_log(self, frame_queue, task_key):
        self._release_task_log(frame_queue, task_key)

    def _release_task_log(self, frame_queue, task_key):
        """Drop one queue from one task log. The last queue releases the stream."""
        queues = self._task_log_queues.get(task_key)
        if queues is None or frame_queue not in queues:
            return
        queues.discard(frame_queue)
        if not queues:
            del self._task_log_queues[task_key]
            self._client.unsubscribe_task_log(task_key, self._inbox.put)

    def _retire(self):
        """Release the port subscriptions and the Bridges entry once the session
        end frame is out. Without this, a long serve process keeps one drain task per
        ended session."""
        self.detach()
        if self.owner is not None:
            self.owner.discard(self.session_id)

    # --- the loop side ---------------------------------------------------------

    def add_queue(self, frame_queue):
        if frame_queue not in self._queues:
            self._queues.append(frame_queue)

    def remove_queue(self, frame_queue):
        """A queue that leaves releases its task logs with it, so no teardown
        path has to remember the task logs of a session."""
        if frame_queue in self._queues:
            self._queues.remove(frame_queue)
            for task_key in tuple(self._task_log_queues):
                self._release_task_log(frame_queue, task_key)

    def snapshot(self):
        """The current session state, stamped with the sequence the events
        continue from. This runs on the loop between drain passes, so the stamp
        and the state match (see SessionSnapshot)."""
        return SnapshotFrame(
            session_id=self.session_id,
            seq=self.seq,
            snapshot=SessionSnapshot.from_session(self.session),
        )

    def _fan_out(self, lane, fields):
        self.seq += 1
        frame_class = lane.get_frame()
        frame = frame_class(session_id=self.session_id, seq=self.seq, **fields)
        payload = encode_frame(frame)
        for frame_queue in self._queues:
            frame_queue.push(payload)

    async def _drain(self):
        while True:
            batch = []
            while True:
                try:
                    batch.append(self._inbox.get_nowait())
                except queue.Empty:
                    break
            pairs = encode_tick(batch)
            for lane, fields in pairs:
                self._fan_out(lane, fields)
            if any(lane is SessionEndedLane for lane, _ in pairs):
                self._retire()
                return
            await asyncio.sleep(FLUSH_TICK)
