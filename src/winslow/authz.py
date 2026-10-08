"""The permission check of the port: two decorators on the endpoints the
server runs, the check the action dispatcher applies, and the helpers the two
state-dependent handlers refine with. A decorator names the permissions the
caller needs, one of them, on the scope name of the object (see
winslow.permissions). The check reads the ambient PermissionContext both doors
set (see winslow.task.context): no context means an in-process caller, which
passes."""

import functools
import logging
from dataclasses import replace

from winslow.exceptions import RequestError
from winslow.task.context import current_permissions

AUDIT = logging.getLogger("winslow.audit")

# The port reads whose guard sits in the body: their scope name comes from an
# argument, so the decorator cannot read it (see LocalAppClient.create_session).
BODY_GUARDED = frozenset({"create_session", "restore_session"})
# The port reads open to any authenticated caller.
OPEN = frozenset({"host_stats"})


def permitted(permission, scope_name):
    """Whether the ambient caller holds the permission on the scope name. An
    in-process caller holds everything."""
    context = current_permissions()
    return context is None or context.allows(scope_name, permission)


def check(permissions, scope_name):
    """The refusal reason when the ambient caller holds none of the
    permissions on the scope name, or None when the call may go ahead
    (see PermissionContext.check)."""
    context = current_permissions()
    return None if context is None else context.check(scope_name, permissions)


def audit(action, scope_name, session_id, accepted, reason=None, keys=None):
    """One line per action and per session start, on the audit logger, with
    the fields as structured data beside the message (see StructuredFormatter).
    A local caller leaves none."""
    context = current_permissions()
    if context is None:
        return
    fields = {
        "action": action,
        "scope": scope_name,
        "session": session_id,
        "keys": keys,
        "result": "accepted" if accepted else "refused",
        "reason": None if accepted else reason,
    }
    rendered = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    AUDIT.info(
        f"audit user={context.user} credential={context.credential} {rendered}",
        extra={"audit": fields},
    )


def require(permission, scope_name, action=None, session_id=None):
    """The check of a read whose scope name the body resolves. A refusal
    leaves the audit line of the named action and raises RequestError."""
    reason = check((permission,), scope_name)
    if reason is not None:
        audit(action, scope_name, session_id, False, reason)
        raise RequestError(reason)


class requires:
    """Mark a method with the permissions the caller needs, one of them, on
    self.permission_scope. A port read gets its guard from the class
    machinery (see guard, _PortMeta). The action dispatcher reads the marker
    itself and refuses through the ack (see ActionHandler.submit)."""

    def __init__(self, *permissions):
        self.permissions = permissions

    def __call__(self, method):
        method.requires = self.permissions
        return method


class visible:
    """Mark a read whose rows filter to the ones whose permission_scope the
    caller holds the permission on. attr names the tuple field of a result
    object; None means the result is the tuple (see filter_rows, _PortMeta)."""

    def __init__(self, permission, attr=None):
        self.permission = permission
        self.attr = attr

    def __call__(self, method):
        method.visible = (self.permission, self.attr)
        return method


def guard(method):
    """The read behind its permission check: a refusal raises RequestError."""

    @functools.wraps(method)
    def guarded(self, *args, **kwargs):
        reason = check(method.requires, self.permission_scope)
        if reason is not None:
            raise RequestError(reason)
        return method(self, *args, **kwargs)

    return guarded


def filter_rows(method):
    """The read behind its row filter (see visible)."""
    permission, attr = method.visible

    @functools.wraps(method)
    def filtered(self, *args, **kwargs):
        result = method(self, *args, **kwargs)
        context = current_permissions()
        if context is None:
            return result

        def keep(row):
            return context.allows(row.permission_scope, permission)

        if attr is None:
            return tuple(row for row in result if keep(row))
        rows = tuple(row for row in getattr(result, attr) if keep(row))
        return replace(result, **{attr: rows})

    return filtered
