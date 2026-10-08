"""The access stores: the policy snapshot and its one rule, the token
helpers, the two file stores, and the registries that select a backend."""

import os
import re

import pytest

from winslow import settings
from winslow.access import (
    EMPTY,
    POLICY_BACKENDS,
    TOKEN_BACKENDS,
    FilePolicyStore,
    FileTokenStore,
    Policy,
    TokenRecord,
    checked_name,
    expired,
    new_token,
    parse_duration,
    policy_warnings,
    scopes_for,
    token_hash,
)
from winslow.access.cli import token_create, token_revoke
from winslow.exceptions import CredentialRefused, MisconfigurationError
from winslow.permissions import Permission, Scope, wire_scopes
from winslow.protocol.frames import HelloFrame
from winslow.serve import Credentials
from winslow.serve.auth import TokenCredential

P = Permission

POLICY = """
[groups.etl-operators]
scopes = ["etl:*"]

[groups.etl-checkers]
scopes = ["etl:check_task", "etl:start_workflow"]

[groups.viewers]
scopes = ["*:view_task"]
idp_groups = ["WinslowProdViewers"]

[users.can]
groups = ["etl-operators", "viewers"]

[users.lucas]
groups = ["etl-checkers"]

[users.ci-bot]
scopes = ["etl:run_task"]
"""


def policy_file(tmp_path, text=POLICY):
    path = tmp_path / "policy.toml"
    path.write_text(text)
    return path


# --- the policy and its rule ------------------------------------------------------


def test_the_union_of_groups_and_direct_scopes(tmp_path):
    policy = FilePolicyStore(policy_file(tmp_path)).load()
    assert scopes_for(policy, "can") == {Scope("etl", p) for p in Permission} | {
        Scope("*", P.VIEW_TASK)
    }
    assert wire_scopes(scopes_for(policy, "lucas")) == (
        "etl:check_task",
        "etl:start_workflow",
        "etl:view_task",
    )
    assert wire_scopes(scopes_for(policy, "ci-bot")) == (
        "etl:check_task",
        "etl:run_task",
        "etl:view_task",
    )


def test_a_user_the_file_does_not_list_holds_the_groups_the_token_asserts(tmp_path):
    policy = FilePolicyStore(policy_file(tmp_path)).load()
    assert scopes_for(policy, "guest") == frozenset()
    assert scopes_for(policy, "guest", idp_groups=["WinslowProdViewers"]) == {
        Scope("*", P.VIEW_TASK)
    }
    # An asserted group adds to a listed user too.
    assert Scope("*", P.VIEW_TASK) in scopes_for(
        policy, "lucas", ["WinslowProdViewers"]
    )


def test_an_undefined_group_refuses_the_file_with_both_names():
    with pytest.raises(
        ValueError, match="user 'can' names group 'ops', which the policy"
    ):
        Policy.from_mapping({"groups": {}, "users": {"can": {"groups": ["ops"]}}})


@pytest.mark.parametrize(
    "data, path, reason",
    [
        (
            {"groups": {"etl": {"scope": ["etl:*"]}}},
            "groups.etl.scope",
            "Unexpected keyword",
        ),
        ({"groups": {"etl": {"scopes": "etl:*"}}}, "groups.etl.scopes", "valid tuple"),
        ({"groups": ["etl"]}, "groups", "valid dictionary"),
        (
            {"groups": {"etl": {"scopes": ["etl:fly"]}}},
            "groups.etl.scopes.0",
            "names no permission",
        ),
        ({"users": {"ci bot": {}}}, "users.ci bot.[key]", "is not a name"),
        (
            {"groups": {"etl": {}}, "users": {"can": {"groups": ["etl ops"]}}},
            "users.can.groups.0",
            "is not a name",
        ),
        ({"tokens": {}}, "tokens", "Unexpected keyword"),
    ],
)
def test_the_schema_refuses_the_file_with_the_path_of_the_field(data, path, reason):
    with pytest.raises(ValueError, match=f"{re.escape(path)}: .*{reason}"):
        Policy.from_mapping(data)


def test_a_name_is_one_word():
    assert checked_name("ci-bot@corp.example") == "ci-bot@corp.example"
    with pytest.raises(ValueError, match="'ci bot' is not a name"):
        checked_name("ci bot")


def test_the_warnings_name_the_entries_that_grant_everything_or_nothing(tmp_path):
    policy = Policy.from_mapping(
        {
            "groups": {
                "admins": {"scopes": ["*:*"]},
                "stub": {},
                "etl": {"scopes": ["etl:*"]},
            },
            "users": {"root": {"scopes": ["*:*"]}, "can": {"groups": ["etl"]}},
        }
    )
    assert policy_warnings(policy) == (
        "group 'admins' grants every permission on every workflow",
        "user 'root' grants every permission on every workflow",
        "group 'stub' defines no scopes and grants nothing",
    )
    assert policy_warnings(FilePolicyStore(policy_file(tmp_path)).load()) == ()


def test_the_empty_policy_grants_nothing():
    assert scopes_for(EMPTY, "can") == frozenset()


# --- the file policy store --------------------------------------------------------


def test_a_missing_file_is_the_empty_policy(tmp_path):
    assert FilePolicyStore(tmp_path / "none.toml").load() is EMPTY


def test_an_edit_applies_on_the_next_load(tmp_path):
    path = policy_file(tmp_path)
    store = FilePolicyStore(path)
    assert "ci-bot" in store.load().users
    path.write_text(POLICY.replace('[users.ci-bot]\nscopes = ["etl:run_task"]', ""))
    # A new modification time, on a file system with a coarse clock too.
    stamp = path.stat().st_mtime + 2
    os.utime(path, (stamp, stamp))
    assert "ci-bot" not in store.load().users


def test_a_broken_edit_keeps_the_previous_policy(tmp_path, caplog):
    path = policy_file(tmp_path)
    store = FilePolicyStore(path)
    before = store.load()
    path.write_text("[groups.etl\nscopes = [")
    stamp = path.stat().st_mtime + 2
    os.utime(path, (stamp, stamp))
    assert store.load() is before
    assert "keeping the previous policy" in caplog.text


# --- the tokens -------------------------------------------------------------------


def test_a_token_has_the_prefix_and_hashes_to_its_id():
    token = new_token()
    assert token.startswith("wsl_")
    assert len(token) > 40
    digest = token_hash(token)
    assert digest.startswith("sha256:")
    assert TokenRecord(digest, "can", 0.0).token_id == digest[7:15]
    assert new_token() != token


def test_durations_parse_and_refuse_with_the_shape():
    assert parse_duration("90d") == 90 * 86400
    assert parse_duration("12h") == 12 * 3600
    assert parse_duration("30m") == 1800
    with pytest.raises(ValueError, match="for example 90d"):
        parse_duration("soon")


def test_expiry_is_read_against_now():
    record = TokenRecord("sha256:x", "can", 0.0, expires_at=100.0)
    assert not expired(record, now=99.0)
    assert expired(record, now=100.0)
    assert not expired(TokenRecord("sha256:y", "can", 0.0), now=1e12)


def test_the_file_token_store_round_trips_and_revokes(tmp_path):
    store = FileTokenStore(tmp_path / "tokens")
    first = TokenRecord(token_hash(new_token()), "can", 1.0)
    second = TokenRecord(token_hash(new_token()), "can", 2.0, expires_at=3.0)
    third = TokenRecord(token_hash(new_token()), "ci-bot", 3.0)
    for record in (first, second, third):
        store.add(record)
    assert store.get(first.token_hash) == first
    assert store.get("sha256:nope") is None
    assert store.list("can") == (first, second)
    assert store.list() == (first, second, third)
    assert store.remove(second.token_id) is True
    assert store.remove(second.token_id) is False
    assert store.list("can") == (first,)
    assert (tmp_path / "tokens").stat().st_mode & 0o777 == 0o600
    # The file holds hashes and names alone.
    assert "wsl_" not in (tmp_path / "tokens").read_text()


def test_a_line_that_is_not_a_record_is_skipped_with_a_warning(tmp_path, caplog):
    path = tmp_path / "tokens"
    store = FileTokenStore(path)
    whole = TokenRecord(token_hash(new_token()), "can", 1.0)
    store.add(whole)
    with path.open("a") as handle:
        handle.write("sha256:abc can\n")
    assert store.list() == (whole,)
    assert "skipping a line that is not a token record" in caplog.text


def test_two_concurrent_creates_keep_both_tokens(tmp_path):
    path = tmp_path / "tokens"
    first, second = FileTokenStore(path), FileTokenStore(path)
    assert first.list() == second.list() == ()
    alice = TokenRecord(token_hash(new_token()), "alice", 1.0)
    bob = TokenRecord(token_hash(new_token()), "bob", 2.0)
    first.add(alice)
    second.add(bob)
    assert first.list() == second.list() == (alice, bob)
    assert path.stat().st_mode & 0o777 == 0o600


class DictTokenStore:
    """A token store over a dict: the five methods of the TokenStore port."""

    def __init__(self):
        self.records = {}

    def get(self, token_hash):
        return self.records.get(token_hash)

    def add(self, record):
        self.records = {**self.records, record.token_hash: record}

    def remove(self, token_id):
        kept = {h: r for h, r in self.records.items() if r.token_id != token_id}
        removed = len(kept) < len(self.records)
        self.records = kept
        return removed

    def remove_where(self, user):
        kept = {h: r for h, r in self.records.items() if r.user != user}
        count = len(self.records) - len(kept)
        self.records = kept
        return count

    def list(self, user=None):
        return tuple(r for r in self.records.values() if user is None or r.user == user)


def test_a_registered_token_store_pairs_with_the_file_policy(tmp_path, monkeypatch):
    TOKEN_BACKENDS.register("dict", DictTokenStore)
    monkeypatch.setattr(settings, "TOKEN_BACKEND", "dict")
    try:
        tokens = TOKEN_BACKENDS.create()
    finally:
        TOKEN_BACKENDS.backends.pop("dict")
    assert isinstance(tokens, DictTokenStore)
    policy = Credentials(
        chain=(TokenCredential(tokens, FilePolicyStore(policy_file(tmp_path))),)
    )
    token = token_create(tokens, "lucas")
    context = policy.verify_hello(HelloFrame(token=token))
    assert context.user == "lucas"
    assert context.allows("etl", P.CHECK_TASK) and not context.allows("etl", P.RUN_TASK)
    assert token_revoke(tokens, user="lucas") == 1
    with pytest.raises(CredentialRefused, match="unknown token"):
        policy.verify_hello(HelloFrame(token=token))


def test_the_registries_select_a_backend_and_refuse_an_unknown_one(monkeypatch):
    assert isinstance(POLICY_BACKENDS.create(), FilePolicyStore)
    assert isinstance(TOKEN_BACKENDS.create(), FileTokenStore)

    class StubPolicy:
        def load(self):
            return EMPTY

    POLICY_BACKENDS.register("stub", StubPolicy)
    monkeypatch.setattr(settings, "POLICY_BACKEND", "stub")
    try:
        assert isinstance(POLICY_BACKENDS.create(), StubPolicy)
    finally:
        POLICY_BACKENDS.backends.pop("stub")
    monkeypatch.setattr(settings, "TOKEN_BACKEND", "redis")
    with pytest.raises(MisconfigurationError, match="TOKEN_BACKENDS.register"):
        TOKEN_BACKENDS.create()
    assert "redis" not in TOKEN_BACKENDS.backends
