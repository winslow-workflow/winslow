import asyncio
import getpass
import socket

import psutil

from textual.containers import Vertical
from textual.reactive import var

from winslow.ui.widgets.common.stat import ColorStat, Stat
from winslow.util import human_readable_size


class MemoryStat(ColorStat):
    name = var("memory")
    periodic_update = var(True)

    async def update_value(self):
        mem = await asyncio.to_thread(psutil.virtual_memory)
        used = human_readable_size(mem.used)
        total = human_readable_size(mem.total)

        self.percentage = mem.percent
        self.value = f"{used} / {total} ({mem.percent}%)"


class CpuStat(ColorStat):
    name = var("cpu")
    periodic_update = var(True)

    async def update_value(self):
        value = await asyncio.to_thread(psutil.cpu_percent, interval=1)
        self.percentage = value
        self.value = f"{value:.2f} %"


class HostStat(Stat):
    name = var("host")

    async def update_value(self):
        self.value = socket.gethostname()


class UserStat(Stat):
    name = var("user")

    async def update_value(self):
        self.value = getpass.getuser()


class SystemStats(Vertical):
    def compose(self):
        yield MemoryStat()
        yield CpuStat()
        yield HostStat()
        yield UserStat()
