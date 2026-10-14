"""The permission check of the port. A read and an action declare what they
need on their declaration (see winslow.client.base). The port puts the two
wrappers here behind each read, and the action handlers apply check. The
check reads the ambient PermissionContext both doors set (see
winslow.task.context): no context means an in-process caller, which passes."""

import functools
import logging
from dataclasses import replace

from winslow.exceptions import RequestError
from winslow.task.context import current_permissions

AUDIT = logging.getLogger("winslow.audit")


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
    who = {
        "user": context.user,
        "credential": context.credential,
        "credential_id": context.credential_id,
    }
    rendered = " ".join(
        f"{k}={v}" for k, v in {**who, **fields}.items() if v is not None
    )
    AUDIT.info(f"audit {rendered}", extra={"audit": fields})


def guard(method, permissions):
    """The read behind its permission check, one of permissions on
    self.permission_scope: a refusal raises RequestError."""

    @functools.wraps(method)
    def guarded(self, *args, **kwargs):
        reason = check(permissions, self.permission_scope)
        if reason is not None:
            raise RequestError(reason)
        return method(self, *args, **kwargs)

    return guarded


def filter_rows(method, permission, attr):
    """The read behind its row filter: the rows whose permission_scope the
    caller holds the permission on. attr names the tuple field of a result
    object, None means the result is the tuple (see Read.visible)."""

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
