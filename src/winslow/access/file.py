"""The default stores: a policy file the operator edits, and a token file
the CLI writes."""

import os
import tempfile
import tomllib
from pathlib import Path
from types import MappingProxyType

from winslow.conf import settings
from winslow.access.policy import EMPTY, Policy
from winslow.access.tokens import TokenRecord
from winslow.logger import LOGGER


def _write_text(path, text, mode=0o600):
    """Publish atomically with a private temp name, so two writers cannot
    publish each other's bytes (see FileStateStore._write_text)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = tempfile.NamedTemporaryFile(
        "w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
    )
    try:
        with temp:
            temp.write(text)
        os.chmod(temp.name, mode)
        os.replace(temp.name, path)
    except BaseException:
        Path(temp.name).unlink(missing_ok=True)
        raise


def _append_line(path, line, mode=0o600):
    """Append one line. The kernel writes an O_APPEND write of one line
    whole, so two writers keep both lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(line)


class FileSnapshot:
    """A parsed file, read again when its modification time changes, so an
    edit applies to the next connection. A file that does not parse keeps
    the previous value and logs the error, so a half-saved edit cannot empty
    the store. empty is the value of a missing file; noun names the value
    in the log line."""

    empty = None
    noun = "value"

    def __init__(self, path):
        self.path = Path(path)
        self._stamp = None
        self._value = self.empty

    def parse(self, text):
        raise NotImplementedError

    def load(self):
        try:
            stamp = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return self.empty
        if stamp == self._stamp:
            return self._value
        try:
            value = self.parse(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOGGER.error(f"{self.path}: {exc} - keeping the previous {self.noun}.")
            return self._value
        self._stamp, self._value = stamp, value
        return value


class FilePolicyStore(FileSnapshot):
    """The policy file, `groups` and `users` tables in TOML (see FileSnapshot
    for the reload rule)."""

    empty = EMPTY
    noun = "policy"

    def __init__(self, path=None):
        super().__init__(path or settings.AUTH_POLICY_FILE)

    def parse(self, text):
        return Policy.from_mapping(tomllib.loads(text))


class FileTokenStore(FileSnapshot):
    """The token file: one record per line, `sha256:<hex> <user> <created>
    <expires or ->` (see FileSnapshot for the reload rule). A create appends
    its line, so two concurrent creates keep both tokens. A revoke publishes
    a whole new file."""

    empty = MappingProxyType({})
    noun = "tokens"

    def __init__(self, path=None):
        super().__init__(path or settings.AUTH_TOKENS_FILE)

    def parse(self, text):
        """The records by hash. A line that is not a record, such as the tail
        of a write that did not finish, is skipped with a WARNING."""
        records = (self._decode(line) for line in text.splitlines() if line.strip())
        return {r.token_hash: r for r in records if r is not None}

    def _decode(self, line):
        try:
            token_hash, user, created, expires = line.split()
            return TokenRecord(
                token_hash=token_hash,
                user=user,
                created_at=float(created),
                expires_at=None if expires == "-" else float(expires),
            )
        except ValueError:
            LOGGER.warning(f"{self.path}: skipping a line that is not a token record.")
            return None

    @classmethod
    def _encode(cls, record):
        expires = "-" if record.expires_at is None else repr(record.expires_at)
        return f"{record.token_hash} {record.user} {record.created_at!r} {expires}"

    def _write(self, records):
        ordered = sorted(records.values(), key=lambda r: r.created_at)
        _write_text(self.path, "".join(f"{self._encode(r)}\n" for r in ordered))
        self._stamp = self.path.stat().st_mtime_ns
        self._value = records

    def get(self, token_hash):
        return self.load().get(token_hash)

    def add(self, record):
        _append_line(self.path, f"{self._encode(record)}\n")
        # The file may hold lines of another writer too: the next read parses it.
        self._stamp = None

    def _drop(self, matches):
        """Publish the file without the records that match. Return the count."""
        records = self.load()
        kept = {h: r for h, r in records.items() if not matches(r)}
        if len(kept) < len(records):
            self._write(kept)
        return len(records) - len(kept)

    def remove(self, token_id):
        return self._drop(lambda r: r.token_id == token_id) == 1

    def remove_where(self, user):
        return self._drop(lambda r: r.user == user)

    def list(self, user=None):
        records = sorted(self.load().values(), key=lambda r: r.created_at)
        return tuple(r for r in records if user is None or r.user == user)
