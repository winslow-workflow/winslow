"""The web UI of the serve process: the routes that hand out the built page,
and the frame list the browser client mirrors (see web/src/protocol/frames.ts)."""

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from winslow.exceptions import MisconfigurationError

from winslow.protocol.frames import Frame
from winslow.protocol import lanes  # noqa: F401  - registers the lane frames
from winslow.serve import Credentials, create_app
from winslow.serve.auth import page_origins
from winslow.session import SessionRegistry

from harness import bare_orchestrator, scratch_state_store

WEB_FRAMES = (
    Path(__file__).resolve().parents[1] / "web" / "src" / "protocol" / "frames.ts"
)


def client(static_dir, endpoints=("ws", "web")):
    app = create_app(
        SessionRegistry(),
        Credentials(require_credential=False),
        orchestrator=bare_orchestrator(),
        state_store=scratch_state_store(),
        endpoints=endpoints,
        static_dir=static_dir,
    )
    return TestClient(app)


def build(tmp_path):
    (tmp_path / "index.html").write_text("<html>ui</html>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    return tmp_path


def test_the_index_and_the_assets_serve_from_the_build(tmp_path):
    web = client(build(tmp_path))
    assert web.get("/").text == "<html>ui</html>"
    assert web.get("/assets/app.js").text == "console.log(1)"
    assert web.get("/elsewhere").status_code == 404


def test_the_websocket_upgrades_beside_the_page(tmp_path):
    with client(build(tmp_path)).websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello"})
        assert ws.receive_json()["type"] == "hello_ok"


def test_a_checkout_without_a_build_serves_the_endpoints_alone(tmp_path):
    assert client(tmp_path).get("/").status_code == 404


def test_ws_alone_skips_the_page(tmp_path):
    assert client(build(tmp_path), endpoints=("ws",)).get("/").status_code == 404


def test_web_needs_ws():
    with pytest.raises(MisconfigurationError, match="needs the ws endpoint"):
        client(None, endpoints=("web",))


def test_the_page_origins_of_a_bind():
    assert page_origins("127.0.0.1", 8866) == (
        "http://127.0.0.1:8866",
        "http://[::1]:8866",
        "http://localhost:8866",
    )
    assert page_origins("etl-host", 9000) == ("http://etl-host:9000",)


def test_the_browser_client_lists_every_frame_type():
    block = re.search(r"FRAME_TYPES = \[(.*?)\] as const", WEB_FRAMES.read_text(), re.S)
    declared = set(re.findall(r"'([a-z_]+)'", block.group(1)))
    assert declared == set(Frame.types)
