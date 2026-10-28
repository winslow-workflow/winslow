"""The system resources pane: memory, cpu, host and user of the process that
runs the sessions, over the host_stats read of the port. A remote terminal
shows the server this way (see winslow.client)."""

import asyncio

from textual.containers import Vertical
from textual.reactive import var

from winslow.ui.reads import port_read
from winslow.ui.widgets.common.stat import ColorStat, Stat
from winslow.util import human_readable_size

HOST_STATS_INTERVAL = 1


class MemoryStat(ColorStat):
    name = var("memory")

    def show(self, stats):
        used = human_readable_size(stats.memory_used)
        total = human_readable_size(stats.memory_total)
        self.percentage = stats.memory_percent
        self.value = f"{used} / {total} ({stats.memory_percent}%)"


class CpuStat(ColorStat):
    name = var("cpu")

    def show(self, stats):
        self.percentage = stats.cpu_percent
        self.value = f"{stats.cpu_percent:.2f} %"


class HostStat(Stat):
    name = var("host")

    def show(self, stats):
        self.value = stats.hostname


class UserStat(Stat):
    name = var("user")

    def show(self, stats):
        # A served host reports no user (see LocalAppClient).
        self.display = stats.user is not None
        self.value = stats.user


class SystemStats(Vertical):
    def compose(self):
        yield MemoryStat()
        yield CpuStat()
        yield HostStat()
        yield UserStat()

    async def on_mount(self):
        await self.refresh_stats()
        self.set_interval(HOST_STATS_INTERVAL, self.refresh_stats)

    async def refresh_stats(self):
        # The poll stays quiet: a wire outage shows in the connection bar already.
        stats = await asyncio.to_thread(
            port_read, self, self.app.client.host_stats, quiet=True
        )
        if stats is None:
            return
        for stat in self.query(Stat):
            stat.show(stats)
