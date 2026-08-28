from dataclasses import dataclass, field
from decimal import Decimal

from textual.widgets import (
    Switch,
    Input,
    Label,
    SelectionList,
    RadioSet,
    RadioButton,
)

from winslow.ui.widgets.common import FilterableOptionList


# The origin of a field. The collection thus separates the values by config, and
# one option name in both configs makes no collision.
ORCHESTRATOR_FIELD = "orchestrator-field"
WORKFLOW_FIELD = "workflow-field"

# The scalar types a form parses inline, by the type name of the OptionRow. A
# value of any other type travels as its string; create_session parses it with
# the declared option type (see validate_values).
_TYPE_PARSERS = {
    "int": int,
    "float": float,
    "Decimal": Decimal,
    "str": str,
    None: None,
}


@dataclass
class FormValues:
    """The form inputs, separated by origin. The two configs thus never share a
    namespace between the form and create_session."""

    orchestrator: dict = field(default_factory=dict)
    workflow: dict = field(default_factory=dict)


class WorkflowFormValidator:
    """Apply the OptionRow rules to the form inputs and mark each input that
    fails. A scalar type parses inline for an immediate field error; every
    other value stays a string for create_session to parse."""

    def __init__(self, logger):
        self.logger = logger

    def _convert_type(self, value, row):
        if value is None:
            return None
        parser = _TYPE_PARSERS.get(row.type)
        return parser(value) if parser else value

    def _collect_input_value(self, widget, row):
        value = widget.value if widget.value != "" else None
        return self._convert_type(value, row)

    def _collect_switch_value(self, widget, row):
        if row.action == "store_true":
            return widget.value
        elif row.action == "store_false":
            return not widget.value
        elif row.action == "store_const":
            return row.const if widget.value else None
        raise ValueError(f"Invalid action for Switch - {widget.name} - {row.action}")

    def _collect_radio_set_value(self, widget, row):
        for radio_button in widget.query(RadioButton):
            if radio_button.value:
                return self._convert_type(radio_button.name, row)

    def _collect_selection_value(self, widget, row):
        return [self._convert_type(v, row) for v in widget.selected]

    def _collect_option_list_value(self, widget, row):
        return (
            self._convert_type(widget.value, row)
            if widget.value is not None
            else None
        )

    @classmethod
    def _is_missing(cls, value):
        """A collected form value is absent if the user left the field empty. An
        empty Input, OptionList or RadioSet gives None, and an empty
        SelectionList gives []."""
        return value is None or value == []

    def validate(self, workflow_form, override_rows, option_rows):
        orchestrator, orch_errors = self._collect_group(
            workflow_form, ORCHESTRATOR_FIELD, {r.name: r for r in override_rows}
        )
        workflow, wf_errors = self._collect_group(
            workflow_form, WORKFLOW_FIELD, {r.name: r for r in option_rows}
        )
        return FormValues(orchestrator=orchestrator, workflow=workflow), {
            **orch_errors,
            **wf_errors,
        }

    def _collect_group(self, workflow_form, field_class, rows_by_name):
        collect_map = {
            Switch: self._collect_switch_value,
            Input: self._collect_input_value,
            SelectionList: self._collect_selection_value,
            RadioSet: self._collect_radio_set_value,
            FilterableOptionList: self._collect_option_list_value,
        }

        values, errors = {}, {}

        for widget in workflow_form.query(f".{field_class}").results():
            row = rows_by_name[widget.name]
            collection_func = collect_map[type(widget)]
            try:
                value = collection_func(widget, row)
            except (TypeError, ValueError) as e:
                errors[widget.name] = str(e)
                continue
            values[widget.name] = value
            if row.required and self._is_missing(value):
                errors[widget.name] = "this field is required"

        self._check_dependencies(values, rows_by_name, errors)
        return values, errors

    @classmethod
    def _check_dependencies(cls, values, rows_by_name, errors):
        """OptionRow.depends_on on the side of the form. A field with a value
        demands that its dependencies also have a value. Only a field that
        the form shows takes part. A hidden option keeps the value of the
        parsed base, and the CLI commit points enforce it (see
        validate_option_dependencies)."""
        for name, value in values.items():
            row = rows_by_name[name]
            if not row.depends_on or not value:
                continue
            for dep_name in row.depends_on:
                if dep_name in values and not values[dep_name]:
                    errors[name] = f"can only be used with {dep_name.replace('_', '-')}"

    def render_errors(self, workflow_form, errors):
        for label in workflow_form.query(".field-error").results():
            label.remove()
        for widget in workflow_form.query(".form-field").results():
            widget.remove_class("invalid")
            if widget.name in errors:
                widget.add_class("invalid")
                widget.parent.mount(
                    Label(errors[widget.name], classes="field-error"), after=widget
                )

        # The field that failed can be outside the view, because the form
        # scrolls. The summary in the footer is always visible to the user.
        summary = workflow_form.query_one(".form-error-summary", Label)
        if errors:
            fields = ", ".join(name.replace("_", "-") for name in errors)
            summary.update(f"invalid fields: {fields}")
            summary.remove_class("hidden")
        else:
            summary.update("")
            summary.add_class("hidden")
