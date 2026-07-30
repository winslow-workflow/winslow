from textual.widgets import DataTable

from winslow.util import safe_repr


class ParamsTable(DataTable):
    """A Parameter/Value table for the parameter context of a workflow. The user
    cannot edit it.

    The confirmation popup before the start and the parameters modal of a running
    workflow both use it. It is filled at mount, because a DataTable accepts no
    column and no row during compose."""

    def __init__(self, params, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._params = params or {}
        self.show_cursor = False
        self.zebra_stripes = True

    def on_mount(self):
        self.add_columns("Parameter", "Value")
        for key, value in self._params.items():
            self.add_row(str(key).replace("_", " "), safe_repr(value))
