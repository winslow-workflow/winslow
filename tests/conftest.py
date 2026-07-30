import pytest

from winslow.constants import Mode

from harness import E2E_REPO, build_workflow, workflow_repo


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
