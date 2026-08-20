import gc
import weakref
from types import SimpleNamespace

import pytest

from winslow.constants import Mode
from winslow.exceptions import IdentityKeyCollisionError
from winslow.store import StoreListener
from winslow.task.index import TaskIndex

from harness import build_workflow, by_params, run_all


class _StaticKeyTask:
    """A minimal stand-in: the index reads identity_key and nothing else."""

    def __init__(self, key):
        self.identity_key = key


def test_identity_key_is_deterministic(params_workflow):
    """Two instances with the same class and parameters share one key: the
    key is derived from the identity, never from the instance."""
    task = by_params(params_workflow)[("Deploy", "eu")]
    clone = type(task)(task.workflow_config, SimpleNamespace(**task._parameters_dict))
    assert clone is not task
    assert clone.identity_key == task.identity_key


def test_identity_key_ignores_parameter_order(params_workflow):
    """The digest sorts the parameters, so insertion order cannot change
    the key."""
    task = by_params(params_workflow)[("Matrix", "eu", "free")]
    reordered = dict(reversed(list(task._parameters_dict.items())))
    assert len(reordered) > 1, "the fixture must have a multi-parameter task"
    clone = type(task)(task.workflow_config, SimpleNamespace(**reordered))
    assert clone.identity_key == task.identity_key


def test_identity_key_separates_value_types(params_workflow):
    """The digest reads the raw values: the int 1 and the str '1' render
    alike in a display repr, but they are two identities."""
    task = by_params(params_workflow)[("Deploy", "eu")]
    name = next(iter(task._parameters_dict))
    as_int = type(task)(task.workflow_config, SimpleNamespace(**{name: 1}))
    as_str = type(task)(task.workflow_config, SimpleNamespace(**{name: "1"}))
    assert as_int.identity_key != as_str.identity_key


def test_identity_key_reads_the_full_value(params_workflow):
    """Two long strings that differ after the display-repr limit are two
    identities: the digest must not truncate."""
    task = by_params(params_workflow)[("Deploy", "eu")]
    name = next(iter(task._parameters_dict))
    prefix = "x" * 200
    a = type(task)(task.workflow_config, SimpleNamespace(**{name: prefix + "a"}))
    b = type(task)(task.workflow_config, SimpleNamespace(**{name: prefix + "b"}))
    assert a.identity_key != b.identity_key


@pytest.mark.parametrize(
    "name", ["my-workflow", "my-params", "my-constraints", "my-depends", "my-filters"]
)
def test_identity_keys_are_unique_within_a_workflow(e2e_repo, name):
    keys = [
        task.identity_key
        for task in build_workflow(e2e_repo, name, Mode.HEADLESS).tasks
    ]
    assert keys
    assert len(keys) == len(set(keys))


def test_task_index_resolves_every_live_task(params_workflow):
    for task in params_workflow.tasks:
        assert params_workflow.task_index.resolve(task.identity_key) is task


def test_task_index_frees_released_tasks(e2e_repo):
    """The index must never keep a task alive: after release_tasks, every
    entry dies with its task (mirrors the session-end GC guarantee)."""
    workflow = build_workflow(e2e_repo, "my-workflow", Mode.TUI)
    refs = [weakref.ref(task) for task in workflow.tasks]
    keys = [task.identity_key for task in workflow.tasks]

    workflow.release_tasks()
    gc.collect()

    assert all(ref() is None for ref in refs), "the index retained a task"
    assert all(workflow.task_index.get(key) is None for key in keys)


def test_task_index_rejects_a_duplicate_key():
    index = TaskIndex()
    first = _StaticKeyTask("deploy-abc12345")
    index.add(first)
    index.add(first)  # re-adding the same task is not a collision
    with pytest.raises(IdentityKeyCollisionError, match="deploy-abc12345"):
        index.add(_StaticKeyTask("deploy-abc12345"))


def test_task_index_resolve_names_the_missing_key():
    with pytest.raises(KeyError, match="deploy-abc12345"):
        TaskIndex().resolve("deploy-abc12345")


class _StatusRecorder(StoreListener):
    def __init__(self):
        self.events = []

    def on_task_status(self, key, status):
        self.events.append((key, status))


def test_on_task_status_payload_is_the_identity_key(params_workflow):
    """The payload rule of the listener API: a status event carries the
    identity key of the task, never the task."""
    recorder = _StatusRecorder()
    params_workflow.store.add_listener(recorder)

    run_all(params_workflow)

    assert recorder.events
    keys = {task.identity_key for task in params_workflow.tasks}
    for key, _ in recorder.events:
        assert isinstance(key, str)
        assert key in keys


def test_log_key_composes_nonce_and_identity_key(params_workflow):
    """The routing key must stay process-local: the nonce prefixes every
    task's key, and the session-durable part is the bare identity key."""
    for task in params_workflow.tasks:
        assert task.log_key == f"{params_workflow.run_nonce}:{task.identity_key}"


def test_run_nonce_is_unique_per_workflow_instance(e2e_repo):
    first = build_workflow(e2e_repo, "my-workflow", Mode.HEADLESS)
    second = build_workflow(e2e_repo, "my-workflow", Mode.HEADLESS)
    assert first.run_nonce != second.run_nonce


def test_run_nonce_stays_off_the_config(params_workflow):
    """The nonce lives on the workflow. The caller owns the config namespace,
    and a stamp there would leak between instances that share the namespace."""
    assert not hasattr(params_workflow.workflow_config, "run_nonce")
