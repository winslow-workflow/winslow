import pytest

from winslow.constants import Mode
from winslow.orchestrator import OrchestratorConfig
from winslow.state import FileStateStore

from harness import E2E_REPO, build_workflow, workflow_repo


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path, monkeypatch):
    """Point every state read and write at a per-test directory. The app
    otherwise reads and mutates the real state dir under the CWD."""
    monkeypatch.setenv("WINSLOW_STATE_DIR", str(tmp_path / "state"))


@pytest.fixture
def state_store(tmp_path):
    class TmpStateStore(FileStateStore):
        base_directory = tmp_path / "state"

    return TmpStateStore(OrchestratorConfig())


@pytest.fixture(scope="session")
def e2e_repo():
    with workflow_repo(E2E_REPO) as directory:
        yield directory


@pytest.fixture(params=[Mode.HEADLESS, Mode.TUI], ids=["headless", "interactive"])
def mode(request):
    return request.param


@pytest.fixture
def workflow(e2e_repo, mode):
    return build_workflow(e2e_repo, "my-workflow", mode)


@pytest.fixture
def params_workflow(e2e_repo, mode):
    return build_workflow(e2e_repo, "my-params", mode)


@pytest.fixture
def constraints_workflow(e2e_repo, mode):
    return build_workflow(e2e_repo, "my-constraints", mode)
