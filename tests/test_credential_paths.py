"""Each credential kind through a door, end to end: the credential on the
wire resolves to a context, the batch carries it, and the history row names
the user, the kind and the id of the credential."""

import jwt

from winslow.access.backends import TokenBackend
from winslow.access.commands import token_create
from winslow.access.permissions import ALL
from winslow.client.websocket import RemoteAppClient

from harness import by_name, credential_authenticator, mint, wait_for_status
from test_client_websocket import ServedProcess, registered, run_to_completion
from test_credentials import authenticator
from test_serve_mcp import call_tools, mcp_app
from winslow.task.status import TaskStatus as S


def token_policy(tmp_path, **overrides):
    """A token chain whose policy names my-workflow, so can holds every
    permission on the served workflow and lucas holds check alone."""
    policy, tokens = authenticator(tmp_path, **overrides)
    path = tmp_path / "policy.toml"
    path.write_text(path.read_text().replace("etl:", "my-workflow:"))
    return policy, tokens


def jti(ticket):
    return jwt.decode(ticket, options={"verify_signature": False})["jti"]


def row_through_the_socket(e2e_repo, policy, token):
    workflow, session, registry = registered(e2e_repo)
    process = ServedProcess(registry, authenticator=policy).start()
    try:
        client = RemoteAppClient(process.url, token=token).connect()
        try:
            remote = client.session(session.session_id)
            run_to_completion(workflow, remote, by_name(workflow)["Alpha"])
            (row,) = remote.history()
            # The client holds the id the row records (see HelloOkFrame).
            assert client.permissions().credential_id == row.credential_id
        finally:
            client.close()
    finally:
        process.stop()
    return row


def test_a_per_user_token_names_its_user_and_id_on_the_row(e2e_repo, tmp_path):
    policy, tokens = token_policy(tmp_path)
    token = token_create(tokens, "can")
    row = row_through_the_socket(e2e_repo, policy, token)
    (record,) = tokens.list("can")
    assert (row.user, row.credential, row.credential_id) == (
        "can",
        "token",
        record.token_id,
    )


def test_a_ticket_names_its_user_and_jti_on_the_row(e2e_repo):
    ticket = mint(user="lucas")
    row = row_through_the_socket(e2e_repo, None, ticket)
    assert (row.user, row.credential, row.credential_id) == (
        "lucas",
        "ticket",
        jti(ticket),
    )


def test_open_mode_names_a_known_token_on_the_row(e2e_repo, tmp_path):
    # lucas holds check alone in the policy; open mode runs the task anyway.
    policy, tokens = token_policy(tmp_path, open=True)
    token = token_create(tokens, "lucas")
    row = row_through_the_socket(e2e_repo, policy, token)
    (record,) = tokens.list("lucas")
    assert (row.user, row.credential, row.credential_id) == (
        "lucas",
        "token",
        record.token_id,
    )


def test_open_mode_connects_without_a_credential_as_anonymous(e2e_repo, tmp_path):
    policy, _ = token_policy(tmp_path, open=True)
    row = row_through_the_socket(e2e_repo, policy, None)
    assert (row.user, row.credential, row.credential_id) == ("anonymous", "open", None)


def test_the_mcp_door_names_the_bearer_token_on_the_row(e2e_repo, tmp_path):
    policy, tokens = token_policy(tmp_path)
    token = token_create(tokens, "can")
    workflow, session, registry = registered(e2e_repo)
    alpha = by_name(workflow)["Alpha"]
    (ack,) = call_tools(
        mcp_app(registry, policy),
        [
            (
                "run_tasks",
                {"session_id": session.session_id, "keys": [alpha.identity_key]},
            )
        ],
        token=token,
    )
    assert ack["accepted"] is True
    wait_for_status(workflow, alpha, S.COMPLETED)
    workflow.runner.get_batch(ack["batch_uuid"]).wait()
    # The MCP session manager runs once per app: a second app serves the read.
    (history,) = call_tools(
        mcp_app(registry, policy),
        [("history", {"session_id": session.session_id})],
        token=token,
    )
    (record,) = tokens.list("can")
    (row,) = history if isinstance(history, list) else [history]
    assert (row["user"], row["credential"], row["credential_id"]) == (
        "can",
        "token",
        record.token_id,
    )


def test_a_subclass_rule_decides_the_context_through_the_socket(e2e_repo, tmp_path):
    """lucas holds check alone in the policy file; a backend whose scopes_for
    grants everything lets the token run the workflow."""

    class EveryoneRuns(TokenBackend):
        def scopes_for(self, identity):
            return ALL

    policy, _ = token_policy(tmp_path)
    (listed,) = policy.mode.chain.backends
    backend = EveryoneRuns(tokens=listed.tokens, policy=listed.policy)
    token = token_create(backend.tokens, "lucas")
    row = row_through_the_socket(e2e_repo, credential_authenticator(backend), token)
    assert (row.user, row.credential) == ("lucas", "token")
