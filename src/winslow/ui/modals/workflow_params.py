from winslow.ui.widgets.common import ParamsTable

from .common import BaseModal


class WorkflowParams(BaseModal):
    """A read-only view of the parameter context of a workflow session that runs.
    It is the same table as the one on the confirmation popup before the start,
    but it has no proceed button and no cancel button."""

    def __init__(self, workflow, *args, **kwargs):
        self.workflow = workflow
        super().__init__(*args, **kwargs)

    @property
    def modal_title(self):
        return f"{self.workflow.instance_name}  ·  parameters"

    def _params(self):
        # The run settings and the declared parameters of the workflow, in one
        # table. The confirmation popup shows the same view.
        params = dict(self.workflow.settings_snapshot)
        for name in self.workflow.config_option_names:
            params[name] = getattr(self.workflow.workflow_config, name, None)
        return params

    def compose_content(self):
        yield ParamsTable(self._params())
