"""The SessionRegistry contract: one thread-safe map of the live sessions,
shared by the TUI app and the serve transports."""

import threading
from types import SimpleNamespace

import pytest

from winslow.session import SessionRegistry


def stub(session_id):
    return SimpleNamespace(session_id=session_id)


def test_register_resolve_remove():
    registry = SessionRegistry()
    session = stub("s-1")
    registry.register(session)
    assert registry.resolve("s-1") is session
    assert "s-1" in registry
    assert len(registry) == 1
    assert registry.remove("s-1") is session
    assert registry.remove("s-1") is None
    assert registry.get("s-1") is None


def test_resolve_raises_with_direction():
    with pytest.raises(KeyError, match="does not resolve to a live session"):
        SessionRegistry().resolve("gone")


def test_sessions_is_a_stable_tuple_under_concurrent_registration():
    registry = SessionRegistry()

    def churn():
        for n in range(2000):
            registry.register(stub(f"s-{n}"))

    writer = threading.Thread(target=churn)
    writer.start()
    while writer.is_alive():
        for session in registry.sessions():
            assert session.session_id.startswith("s-")
    writer.join()
    assert len(registry) == 2000
