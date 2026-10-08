"""The permission model of a session port: the permissions on a scope name,
carried by a PermissionContext. expand is the only place the implication
table lives, so a client tests membership and holds no table (see
winslow.serve.auth for the credential checks that build a context, and
winslow.authz for what each action and read requires)."""

import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import NamedTuple


class Permission(StrEnum):
    START_WORKFLOW = "start_workflow"
    END_WORKFLOW = "end_workflow"
    RUN_TASK = "run_task"
    CHECK_TASK = "check_task"
    VIEW_TASK = "view_task"


# Each permission and the permissions it implies, one step down.
IMPLIES = {
    Permission.START_WORKFLOW: frozenset({Permission.VIEW_TASK}),
    Permission.END_WORKFLOW: frozenset({Permission.VIEW_TASK}),
    Permission.RUN_TASK: frozenset({Permission.CHECK_TASK}),
    Permission.CHECK_TASK: frozenset({Permission.VIEW_TASK}),
    Permission.VIEW_TASK: frozenset(),
}

# The scope name that covers every workflow, and the permission spelling that
# covers every permission.
ANY = "*"


class Scope(NamedTuple):
    """One permission on one scope name. The wire and the policy files spell
    it `<scope name>:<permission>` (see parse_scopes)."""

    name: str
    permission: Permission

    def __str__(self):
        return f"{self.name}:{self.permission}"


def _closure(permission):
    return {permission}.union(*(_closure(implied) for implied in IMPLIES[permission]))


def parse_scopes(scope):
    """The Scopes of one scope string: one, or every permission for `<name>:*`."""
    name, separator, permission = scope.partition(":")
    if not separator or not name or not permission:
        raise ValueError(
            f"{scope!r} is not a scope - the shape is <scope name>:<permission>, "
            f"for example etl:run_task."
        )
    if permission == ANY:
        return tuple(Scope(name, p) for p in Permission)
    try:
        return (Scope(name, Permission(permission)),)
    except ValueError:
        raise ValueError(
            f"{scope!r} names no permission - the permissions are "
            f"{', '.join(Permission)}, or * for all of them."
        ) from None


def expand(scopes):
    """The closed set of Scopes: every implied permission is present. A scope
    on the name `*` stays as it is and covers every scope name (see
    PermissionContext.allows)."""
    return frozenset(
        Scope(granted.name, implied)
        for scope in scopes
        for granted in parse_scopes(scope)
        for implied in _closure(granted.permission)
    )


def wire_scopes(scopes):
    """The sorted spellings of a scope set, for a frame."""
    return tuple(sorted(str(scope) for scope in scopes))


ALL = expand({f"{ANY}:{ANY}"})


class CredentialKind(StrEnum):
    """The kinds of credential a context comes from (see winslow.serve.auth).
    LOCAL is the in-process caller and the loopback bind, OPEN the open mode."""

    LOCAL = "local"
    OPEN = "open"
    TOKEN = "token"
    TICKET = "ticket"


@dataclass(frozen=True)
class PermissionContext:
    """Who a request comes from and what it may do. scopes is the expanded
    set (see expand). credential names the kind of credential. expires_at is
    the expiry of the credential, an epoch second, or None for a credential
    without one."""

    user: str | None
    scopes: frozenset
    # None for a context with no credential: the wire client before its hello.
    credential: CredentialKind | None
    expires_at: float | None = None

    def allows(self, scope_name, permission):
        return (
            Scope(scope_name, permission) in self.scopes
            or Scope(ANY, permission) in self.scopes
        )

    def check(self, scope_name, permissions):
        """The refusal reason when the context holds none of the permissions
        on the scope name: what it lacks, and what it holds there. None when
        the call may go ahead."""
        if any(self.allows(scope_name, p) for p in permissions):
            return None
        needed = " or ".join(f"{scope_name}:{p}" for p in permissions)
        held = sorted(str(s) for s in self.scopes if s.name in (scope_name, ANY))
        return f"{self.user} lacks {needed} - the scopes cover {', '.join(held) or 'nothing'}"

    def remaining(self, now=None):
        """The seconds until the credential expires, or None for a credential
        without an expiry. A past expiry gives zero."""
        if self.expires_at is None:
            return None
        return max(0.0, self.expires_at - (time.time() if now is None else now))

    def until(self, deadline):
        """The same context with its expiry capped at deadline. A credential
        with an earlier expiry keeps its own."""
        bound = min(t for t in (self.expires_at, deadline) if t is not None)
        return replace(self, expires_at=bound)

    @classmethod
    def full(cls, user, credential, expires_at=None):
        """A context that holds every permission on every scope name."""
        return cls(user=user, scopes=ALL, credential=credential, expires_at=expires_at)


# The context of the local caller and the loopback bind, and the context of a
# hello without a credential in open mode.
LOCAL = PermissionContext.full("local", CredentialKind.LOCAL)
ANONYMOUS = PermissionContext.full("anonymous", CredentialKind.OPEN)
