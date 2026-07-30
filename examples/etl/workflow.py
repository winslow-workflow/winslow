from pathlib import Path

from winslow import Workflow, Task

RAW = Path("raw.txt")
CLEAN = Path("clean.txt")


class Etl(Workflow):
    pass  # The name defaults to "etl", the kebab-cased class name.


class DownloadData(Task):
    def run(self):
        RAW.write_text("oslo 4\nlisbon 17\n")

    def check(self):
        return RAW.exists()


class TransformData(Task):
    dependencies = DownloadData

    def run(self):
        warm = [ln for ln in RAW.read_text().splitlines() if int(ln.split()[1]) > 10]
        CLEAN.write_text("\n".join(warm))

    def check(self):
        return CLEAN.exists()
