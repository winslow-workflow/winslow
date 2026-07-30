from dataclasses import dataclass, field

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


@dataclass
class FormValues:
    """The form inputs, separated by origin. The two configs thus never share a
    namespace between the form and Orchestrator.initialize_workflow."""

    orchestrator: dict = field(default_factory=dict)
    workflow: dict = field(default_factory=dict)


class WorkflowFormValidator:
    """
    Apply the validation rules of the config options to the form inputs. Mark
    each input that fails and show the error message.
    """

    def __init__(self, logger):
        self.logger = logger

    def _convert_type(self, value, config_option):
        if value is not None:
            return config_option.type(value) if config_option.type else value

    def _collect_input_value(self, widget, config_option):
        value = widget.value if widget.value != "" else None
        return self._convert_type(value, config_option)

    def _collect_switch_value(self, widget, config_option):
        if config_option.action == "store_true":
            return widget.value
        elif config_option.action == "store_false":
            return not widget.value
        elif config_option.action == "store_const":
            return config_option.const if widget.value else None
        raise ValueError(
            f"Invalid action for Switch - {widget.name} - {config_option.action}"
        )

    def _collect_radio_set_value(self, widget, config_option):
        for radio_button in widget.query(RadioButton):
            if radio_button.value:
                return self._convert_type(radio_button.name, config_option)

    def _collect_selection_value(self, widget, config_option):
        return [self._convert_type(v, config_option) for v in widget.selected]

    def _collect_option_list_value(self, widget, config_option):
        return (
            self._convert_type(widget.value, config_option)
            if widget.value is not None
            else None
        )

    @classmethod
    def _is_missing(cls, value):
        """A collected form value is absent if the user left the field empty. An
        empty Input, OptionList or RadioSet gives None, and an empty
        SelectionList gives []."""
        return value is None or value == []

    def validate(self, workflow_form, orchestrator_meta, workflow_meta):
        orchestrator, orch_errors = self._collect_group(
            workflow_form, ORCHESTRATOR_FIELD, orchestrator_meta
        )
        workflow, wf_errors = self._collect_group(
            workflow_form, WORKFLOW_FIELD, workflow_meta
        )
        return FormValues(orchestrator=orchestrator, workflow=workflow), {
            **orch_errors,
            **wf_errors,
        }

    def _collect_group(self, workflow_form, field_class, config_meta):
        collect_map = {
            Switch: self._collect_switch_value,
            Input: self._collect_input_value,
            SelectionList: self._collect_selection_value,
            RadioSet: self._collect_radio_set_value,
            FilterableOptionList: self._collect_option_list_value,
        }

        values, errors = {}, {}

        for widget in workflow_form.query(f".{field_class}").results():
            config_option = config_meta[widget.name]
            collection_func = collect_map[type(widget)]
            try:
                value = collection_func(widget, config_option)
            except (TypeError, ValueError) as e:
                errors[widget.name] = str(e)
                continue
            values[widget.name] = value
            if config_option.required and self._is_missing(value):
                errors[widget.name] = "this field is required"

        self._check_dependencies(values, config_meta, errors)
        return values, errors

    @classmethod
    def _check_dependencies(cls, values, config_meta, errors):
        """ConfigOption(depends_on=...) on the side of the form. A field with a
        value demands that its dependencies also have a value. Only a field that
        the form shows takes part. A hidden option keeps the value of the parsed
        base, and the CLI commit points enforce it (see
        validate_option_dependencies)."""
        for name, value in values.items():
            option = config_meta[name]
            if not option.depends_on or not value:
                continue
            for dep_name in option.depends_on:
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
