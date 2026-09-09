from winslow.ui.widgets.common import ParamsTable

from .common import BaseModal


class WorkflowParams(BaseModal):
    """A read-only view of the parameter context of one session: the
    SessionParams value of the port, as one table of the run settings and
    the workflow config. The confirmation popup shows the same table."""

    def __init__(self, instance_name, params, *args, **kwargs):
        self._instance_name = instance_name
        self._params = params
        super().__init__(*args, **kwargs)

    @property
    def modal_title(self):
        return f"{self._instance_name}  ·  parameters"

    def compose_content(self):
        yield ParamsTable({**self._params.settings, **self._params.workflow_config})
