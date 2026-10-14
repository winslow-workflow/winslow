"""The `winslow auth` commands: the credential file of the person for
login, logout and status, and the listed token backend for the tokens of
the people and service accounts that present no other credential, and for
two reads of the policy an operator checks (see winslow.access.backends)."""

import functools
import sys
from dataclasses import dataclass
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


def _create(args, credentials, out, err):
    backend = token_backend()
    out(token_create(backend.tokens, args.user, args.expires))
    if not user_scopes(backend, args.user):
        err(f"{args.user} holds no scope - the token grants nothing until one exists.")
    return 0


def _list(args, credentials, out, err):
    rows = token_rows(token_backend().tokens, args.user)
    if not rows:
        out("no tokens")
    for row in rows:
        out("  ".join(row))
    return 0


def _revoke(args, credentials, out, err):
    count = token_revoke(token_backend().tokens, args.token_id, args.user)
    out(f"revoked {count} token{'s' if count != 1 else ''}")
    return 0 if count else 1


def _show(args, credentials, out, err):
    for key, value in show(token_backend(), args.user).items():
        out(f"{key}: {_render(value)}")
    return 0


def _check(args, credentials, out, err):
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


def _login(args, credentials, out, err):
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


def _logout(args, credentials, out, err):
    key = credential_key(args.url)
    if credentials.delete(args.url):
        out(f"removed the credential for {key}")
        return 0
    out(f"no credential stored for {key}")
    return 1


def _status(args, credentials, out, err):
    entries = credentials.entries()
    if not entries:
        out("no credentials stored")
    for key, entry in entries:
        out(f"{key}  {entry.user or '-'}  {entry.kind.value}")
    return 0


@dataclass(frozen=True)
class AuthCommand:
    """One `winslow auth` command: its path under auth, its help, its
    arguments as (names, options) pairs, and the handler that runs it
    (see run). exclusive holds the arguments of which exactly one is given."""

    path: tuple
    help: str
    handler: object
    arguments: tuple = None
    exclusive: tuple = None


# The help of each intermediate segment of a command path.
GROUPS = {("token",): "The per-user tokens."}

COMMANDS = (
    AuthCommand(
        ("token", "create"),
        "Mint a token for a user, printed once.",
        _create,
        arguments=(
            (("user",), {}),
            (("--expires",), {"default": None, "help": "A lifetime, e.g. 90d or 12h."}),
        ),
    ),
    AuthCommand(
        ("token", "list"),
        "The tokens, of one user when given.",
        _list,
        arguments=((("user",), {"nargs": "?", "default": None}),),
    ),
    AuthCommand(
        ("token", "revoke"),
        "Remove one token by id, or every token of a user.",
        _revoke,
        exclusive=(
            (("--token-id",), {"default": None}),
            (("--user",), {"default": None}),
        ),
    ),
    AuthCommand(
        ("login",),
        "Store the credential of a server, prompted or from stdin.",
        _login,
        arguments=(
            (("url",), {}),
            (
                ("--with-token",),
                {"action": "store_true", "help": "Read the credential from stdin."},
            ),
            (
                ("--kind",),
                {"default": None, "help": "The backend: token, ticket, or one listed."},
            ),
            (
                ("--insecure",),
                {"action": "store_true", "help": "Store one for a ws:// URL."},
            ),
        ),
    ),
    AuthCommand(
        ("logout",),
        "Remove the stored credential of a server.",
        _logout,
        arguments=((("url",), {}),),
    ),
    AuthCommand(("status",), "The stored credentials.", _status),
    AuthCommand(
        ("show",),
        "The scopes and token count of a user.",
        _show,
        arguments=((("user",), {}),),
    ),
    AuthCommand(
        ("check",),
        "Whether a user holds a permission on a scope name.",
        _check,
        arguments=((("user",), {}), (("scope",), {}), (("permission",), {})),
    ),
)


def add_parser(subparsers):
    """The `winslow auth` parser, built from COMMANDS. A parsed command
    carries its handler, so the parser and the dispatcher agree by
    construction. Positional arguments, so the ConfigOption machinery stays
    out of it."""
    auth = subparsers.add_parser("auth", help="Manage tokens and inspect the policy.")
    levels = {(): auth.add_subparsers(dest="auth_command", required=True)}
    for command in COMMANDS:
        for depth in range(1, len(command.path)):
            prefix = command.path[:depth]
            if prefix not in levels:
                group = levels[prefix[:-1]].add_parser(prefix[-1], help=GROUPS[prefix])
                levels[prefix] = group.add_subparsers(
                    dest=f"{prefix[-1]}_command", required=True
                )
        parser = levels[command.path[:-1]].add_parser(
            command.path[-1], help=command.help
        )
        for names, options in command.arguments or ():
            parser.add_argument(*names, **options)
        if command.exclusive:
            group = parser.add_mutually_exclusive_group(required=True)
            for names, options in command.exclusive:
                group.add_argument(*names, **options)
        parser.set_defaults(handler=command.handler)
    return auth


def run(args, credentials, out, err=None):
    """Run the parsed `winslow auth` command over the credential file of the
    person (see CredentialFile). Return the exit code. out prints an answer,
    err a note beside it, so a captured token stays one line."""
    err = err or functools.partial(print, file=sys.stderr)
    return args.handler(args, credentials, out, err)
