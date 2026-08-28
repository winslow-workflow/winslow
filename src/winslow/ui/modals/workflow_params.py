from winslow.ui.widgets.common import ParamsTable

from .common import BaseModal


class WorkflowParams(BaseModal):
    """A read-only view of the parameter context of a workflow session that
    runs: the SessionParams value of the port. It is the same table as the one
    on the confirmation popup before the start, but it has no proceed button
    and no cancel button."""

    def __init__(self, instance_name, params, *args, **kwargs):
        self._instance_name = instance_name
        self._params = params
        super().__init__(*args, **kwargs)

    @property
    def modal_title(self):
        return f"{self._instance_name}  ·  parameters"

    def compose_content(self):
        # The run settings and the declared parameters of the workflow, in one
        # table. The confirmation popup shows the same view.
        yield ParamsTable({**self._params.settings, **self._params.workflow_config})
