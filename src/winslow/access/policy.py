"""The policy: who holds which scopes. A Policy is a frozen snapshot of the
policy file (see FilePolicyStore), and policy_scopes is the one rule over it.
PolicyFile is the schema of the file: the codec refuses an unknown key, a
wrong shape, a bad name and a bad scope with the path of the field."""

import re
from dataclasses import dataclass
from typing import Annotated

from pydantic import AfterValidator, ConfigDict

from winslow.access.permissions import ALL, expand, parse_scopes
from winslow.protocol.codec import CODEC, ValidationError, report

# A name is one token of the token file line and one TOML key.
_NAME = re.compile(r"^[A-Za-z0-9._@-]+$")


def checked_name(name):
    """The name when it is one word of letters, digits and . _ @ -."""
    if not _NAME.match(name):
        raise ValueError(
            f"{name!r} is not a name - use letters, digits and the characters . _ @ -"
        )
    return name


def _checked_spelling(spelling):
    parse_scopes(spelling)
    return spelling


Name = Annotated[str, AfterValidator(checked_name)]
Spelling = Annotated[str, AfterValidator(_checked_spelling)]


@dataclass(frozen=True)
class GroupEntry:
    """One `[groups.<name>]` table of the policy file."""

    scopes: tuple[Spelling, ...] = None
    idp_groups: tuple[str, ...] = None
    __pydantic_config__ = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class UserEntry:
    """One `[users.<name>]` table of the policy file."""

    groups: tuple[Name, ...] = None
    scopes: tuple[Spelling, ...] = None
    __pydantic_config__ = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class PolicyFile:
    """The policy file: the `groups` and `users` tables. Every group a user
    names is defined."""

    groups: dict[Name, GroupEntry] = None
    users: dict[Name, UserEntry] = None
    __pydantic_config__ = ConfigDict(extra="forbid")

    def __post_init__(self):
        defined = self.groups or {}
        undefined = tuple(
            (user, group)
            for user, entry in (self.users or {}).items()
            for group in entry.groups or ()
            if group not in defined
        )
        if undefined:
            user, group = undefined[0]
            raise ValueError(
                f"user {user!r} names group {group!r}, which the policy "
                f"does not define - the groups are {sorted(defined) or 'none'}."
            )


@dataclass(frozen=True)
class Group:
    """A named set of scopes. idp_groups are the group names an identity
    provider may assert in a token; a match grants the scopes of this group
    to a user the policy lists nowhere. from_mapping is the one builder, so
    every field arrives complete (see Policy)."""

    name: str
    scopes: frozenset
    idp_groups: frozenset

    @property
    def grants_everything(self):
        return ALL <= self.scopes

    @property
    def grants_nothing(self):
        return not self.scopes


@dataclass(frozen=True)
class User:
    """A user's groups and direct scopes. Direct scopes serve a service
    account or a one-off."""

    name: str
    groups: tuple
    scopes: frozenset

    @property
    def grants_everything(self):
        return ALL <= self.scopes


@dataclass(frozen=True)
class Policy:
    groups: dict
    users: dict

    @classmethod
    def from_mapping(cls, data):
        """A Policy from the mapping shape of the policy file (see PolicyFile).
        A mapping the schema refuses raises ValueError with the path of the
        field and the reason."""
        try:
            file = CODEC.decode(PolicyFile, data)
        except ValidationError as exc:
            raise ValueError(report(exc)) from None
        groups = {
            name: Group(
                name=name,
                scopes=expand(entry.scopes or ()),
                idp_groups=frozenset(entry.idp_groups or ()),
            )
            for name, entry in (file.groups or {}).items()
        }
        users = {
            name: User(
                name=name,
                groups=tuple(entry.groups or ()),
                scopes=expand(entry.scopes or ()),
            )
            for name, entry in (file.users or {}).items()
        }
        return cls(groups=groups, users=users)


EMPTY = Policy(groups={}, users={})


def policy_scopes(policy, user, idp_groups=None):
    """The scopes of a caller: the scopes of the groups the user entry names,
    the direct scopes of that entry, and the scopes of every group whose
    idp_groups intersect the groups a token asserts. An entry is optional: a
    user with no entry and no match holds no scope."""

    entry = policy.users.get(user)
    # The scopes the entry grants by itself, for a service account or a one-off.
    direct = entry.scopes if entry else frozenset()

    # The groups the entry lists under `groups = [...]`.
    named = [policy.groups[name] for name in entry.groups] if entry else []

    # The group names an identity provider put in the token (the OAuth kind).
    asserted = frozenset(idp_groups or ())

    # The policy groups that admit one of those names through `idp_groups`.
    matched = [group for group in policy.groups.values() if group.idp_groups & asserted]

    return direct.union(*(group.scopes for group in named + matched))


def policy_warnings(policy):
    """The sentences the startup log warns with: every group or user that
    grants every permission on every workflow, and every group that grants
    nothing (see Authenticator.describe)."""
    everything = tuple(
        f"{kind} {entry.name!r} grants every permission on every workflow"
        for kind, entries in (("group", policy.groups), ("user", policy.users))
        for entry in entries.values()
        if entry.grants_everything
    )
    nothing = tuple(
        f"group {group.name!r} defines no scopes and grants nothing"
        for group in policy.groups.values()
        if group.grants_nothing
    )
    return everything + nothing
