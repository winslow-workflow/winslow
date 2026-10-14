"""The `winslow auth` parser and dispatcher. COMMANDS declares each command
with the name of its body in winslow.access.commands, imported when a
command runs: the parser builds on a bare install, the bodies need pydantic
and pyjwt."""

import functools
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class AuthCommand:
    """One `winslow auth` command: its path under auth, its help, its
    arguments as (names, options) pairs, and the name of the function in
    winslow.access.commands that runs it (see run). exclusive holds the
    arguments of which exactly one is given."""

    path: tuple
    help: str
    handler: str
    arguments: tuple = None
    exclusive: tuple = None


# The help of each intermediate segment of a command path.
GROUPS = {("token",): "The per-user tokens."}

COMMANDS = (
    AuthCommand(
        ("token", "create"),
        "Mint a token for a user, printed once.",
        "create",
        arguments=(
            (("user",), {}),
            (("--expires",), {"default": None, "help": "A lifetime, e.g. 90d or 12h."}),
        ),
    ),
    AuthCommand(
        ("token", "list"),
        "The tokens, of one user when given.",
        "list_tokens",
        arguments=((("user",), {"nargs": "?", "default": None}),),
    ),
    AuthCommand(
        ("token", "revoke"),
        "Remove one token by id, or every token of a user.",
        "revoke",
        exclusive=(
            (("--token-id",), {"default": None}),
            (("--user",), {"default": None}),
        ),
    ),
    AuthCommand(
        ("login",),
        "Store the credential of a server, prompted or from stdin.",
        "login",
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
        "logout",
        arguments=((("url",), {}),),
    ),
    AuthCommand(("status",), "The stored credentials.", "status"),
    AuthCommand(
        ("show",),
        "The scopes and token count of a user.",
        "show_user",
        arguments=((("user",), {}),),
    ),
    AuthCommand(
        ("check",),
        "Whether a user holds a permission on a scope name.",
        "check_user",
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
    from winslow.access import commands

    err = err or functools.partial(print, file=sys.stderr)
    return getattr(commands, args.handler)(args, credentials, out, err)
