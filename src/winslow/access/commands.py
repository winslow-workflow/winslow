"""The bodies of the `winslow auth` commands (see winslow.access.cli for
the parser): the credential file of the person for login, logout and
status, the listed token backend for the tokens of the people and service
accounts that present no other credential, and two reads of the policy an
operator checks (see winslow.access.backends). The module needs pydantic
and pyjwt, so the parser imports it when a command runs."""

import time
from datetime import datetime

from winslow.access.backends import Identity, backend_classes, token_backend
from winslow.access.credentials import checked_login_url, credential_key
from winslow.access.policy import checked_name
from winslow.access.tokens import (
    TokenRecord,
    expired,
    new_token,
    parse_duration,
    token_hash,
)
from winslow.exceptions import MisconfigurationError
from winslow.access.permissions import (
    Permission,
    PermissionContext,
    wire_scopes,
)


def token_create(tokens, user, expires=None, now=None):
    """Mint a token for the user, store its hash, and return the token. It is
    printed once and never stored."""
    now = time.time() if now is None else now
    token = new_token()
    try:
        record = TokenRecord(
            token_hash=token_hash(token),
            user=checked_name(user),
            created_at=now,
            expires_at=None if expires is None else now + parse_duration(expires),
        )
    except ValueError as exc:
        raise MisconfigurationError(str(exc)) from None
    tokens.add(record)
    return token


def token_revoke(tokens, token_id=None, user=None):
    """Remove one token by id, or every token of a user. Return the count."""
    if token_id is not None:
        return int(tokens.remove(token_id))
    return tokens.remove_where(user)


def _stamp(seconds):
    return (
        "-"
        if seconds is None
        else datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M")
    )


def token_rows(tokens, user=None, now=None):
    """(id, user, created, expires, state) per token, oldest first."""
    return tuple(
        (
            record.token_id,
            record.user,
            _stamp(record.created_at),
            _stamp(record.expires_at),
            "expired" if expired(record, now) else "live",
        )
        for record in tokens.list(user)
    )


def user_scopes(backend, user):
    """The scopes a token of the user holds under the backend."""
    return backend.scopes_for(Identity(user, backend.kind))


def show(backend, user):
    """The expanded scopes and the token count of a user."""
    return {
        "user": user,
        "scopes": list(wire_scopes(user_scopes(backend, user))),
        "tokens": len(backend.tokens.list(user)),
    }


def check(backend, user, scope_name, permission):
    """Whether the user holds the permission on the scope name, with the
    reason a server would give."""
    permission = Permission(permission)
    identity = Identity(user, backend.kind)
    context = PermissionContext.of(identity, backend.scopes_for(identity))
    reason = context.check(scope_name, (permission,))
    if reason is None:
        return True, f"{user} holds {scope_name}:{permission}"
    return False, reason


def _render(value):
    """A list as a comma separated line, `-` when empty, a scalar as it is."""
    if isinstance(value, list):
        return ", ".join(value) or "-"
    return str(value)


def create(args, credentials, out, err):
    backend = token_backend()
    out(token_create(backend.tokens, args.user, args.expires))
    if not user_scopes(backend, args.user):
        err(f"{args.user} holds no scope - the token grants nothing until one exists.")
    return 0


def list_tokens(args, credentials, out, err):
    rows = token_rows(token_backend().tokens, args.user)
    if not rows:
        out("no tokens")
    for row in rows:
        out("  ".join(row))
    return 0


def revoke(args, credentials, out, err):
    count = token_revoke(token_backend().tokens, args.token_id, args.user)
    out(f"revoked {count} token{'s' if count != 1 else ''}")
    return 0 if count else 1


def show_user(args, credentials, out, err):
    for key, value in show(token_backend(), args.user).items():
        out(f"{key}: {_render(value)}")
    return 0


def check_user(args, credentials, out, err):
    allowed, reason = check(token_backend(), args.user, args.scope, args.permission)
    out(reason)
    return 0 if allowed else 1


def _backend_class(kind):
    """The listed backend class `kind` names, the first listed when None."""
    classes = backend_classes()
    if kind is None:
        return classes[0]
    by_name = {cls.name: cls for cls in classes}
    if kind not in by_name:
        raise MisconfigurationError(
            f"{kind!r} names no listed auth backend - AUTH_BACKENDS "
            f"lists {', '.join(by_name) or 'nothing'}."
        )
    return by_name[kind]


def login(args, credentials, out, err):
    try:
        checked_login_url(args.url, insecure=args.insecure)
        entry = _backend_class(args.kind).login(
            credential_key(args.url), args.with_token
        )
    except MisconfigurationError as exc:
        err(str(exc))
        return 1
    credentials.set(args.url, entry)
    out(f"stored the credential for {credential_key(args.url)}")
    return 0


def logout(args, credentials, out, err):
    key = credential_key(args.url)
    if credentials.delete(args.url):
        out(f"removed the credential for {key}")
        return 0
    out(f"no credential stored for {key}")
    return 1


def status(args, credentials, out, err):
    entries = credentials.entries()
    if not entries:
        out("no credentials stored")
    for key, entry in entries:
        out(f"{key}  {entry.user or '-'}  {entry.kind.value}")
    return 0
