from textual.containers import Center, Container, Vertical
from textual.widgets import (
    Label,
    Switch,
    Input,
    SelectionList,
    RadioSet,
    RadioButton,
    Rule,
    Button,
)

from textual.containers import VerticalScroll, Horizontal

from winslow.ui.widgets.common import FilterableOptionList
from winslow.ui.validation import ORCHESTRATOR_FIELD, WORKFLOW_FIELD


class FormLabel(Label):
    pass


class FormRow(Horizontal):
    pass


def switch_checked(row):
    """The checkbox state an OptionRow implies. A store_const value shows as
    checked when the parsed initial holds any value (see OptionRow.initial)."""
    if row.action == "store_const":
        return row.initial is not None
    return row.initial == "True"


class WorkflowFormGenerator:
    # If a single-select field has more options than this number, the form uses
    # an option list with a search instead of a radio set.
    MAX_OPTIONS_FOR_RADIO_SELECT = 10

    """
    In the interactive mode the UI shows a form that it generates. The form
    collects the initial workflow arguments, and it can also override the meta
    parameters that come from the orchestrator.

    A field is filled if the user gave the value as a command-line argument. For
    example, force-run is checked if the user started the UI app with
    'winslow run --force-run'.

    The fields come from the OptionRow values of the Descriptors read: the
    collected workflow options and the orchestrator overrides, in two sections
    (see winslow.model.Descriptors).
    """

    def __init__(self, logger):
        self.logger = logger

    def _name_to_label(self, name):
        # A form label uses kebab-case.
        return name.replace("_", "-")

    def _name_to_label_id(self, name):
        return f"{name}_label"

    def _generate_label(self, name):
        return FormLabel(self._name_to_label(name), id=self._name_to_label_id(name))

    def _generate_help_text(self, help_text):
        return Label(help_text or "", classes="help-text")

    def _choose_generation_func(self, row):
        if row.action in ("store_true", "store_false", "store_const"):
            return self._generate_switch
        elif row.choices:
            if row.multiselect:
                return self._generate_selection_list
            elif len(row.choices) > self.MAX_OPTIONS_FOR_RADIO_SELECT:
                return self._generate_option_list
            return self._generate_radio_select
        return self._generate_text_input

    def _generate_switch(self, row):
        return Switch(value=switch_checked(row), name=row.name)

    def _generate_selection_list(self, row):
        selected = row.initial_selection or ()
        selection_ctx = [
            (choice, choice, choice in selected) for choice in row.choices
        ]
        return SelectionList(*selection_ctx, name=row.name)

    def _generate_option_list(self, row):
        # The widget shows the choice strings, and the validator parses the
        # picked one back with the option type (see WorkflowFormValidator).
        return FilterableOptionList(*row.choices, name=row.name, initial=row.initial)

    def _generate_radio_select(self, row):
        radio_ctx = [
            RadioButton(choice, value=choice == row.initial, name=choice)
            for choice in row.choices
        ]
        return RadioSet(*radio_ctx, name=row.name, classes="form-field")

    def _generate_text_input(self, row):

        def _get_type():
            if row.type in ("float", "Decimal"):
                return "number"
            elif row.type == "int":
                return "integer"
            return "text"

        return Input(type=_get_type(), name=row.name, value=row.initial or "")

    def generate_inputs_for_rows(self, rows, field_class):

        result = []

        for row in rows:
            field_func = self._choose_generation_func(row)

            form_field = field_func(row)
            form_field.add_class("form-field")
            form_field.add_class(field_class)

            form_row = FormRow(
                self._generate_label(row.name),
                Vertical(
                    form_field,
                    self._generate_help_text(row.help),
                    classes="form-input",
                ),
            )
            result.append(Rule())
            result.append(form_row)

        return result

    def generate(self, descriptor, overrides):

        workflow_name = descriptor.workflow
        self.logger.debug(f"Generating form for {workflow_name}")

        workflow_inputs = self.generate_inputs_for_rows(
            descriptor.options, field_class=WORKFLOW_FIELD
        )

        orchestrator_inputs = self.generate_inputs_for_rows(
            overrides, field_class=ORCHESTRATOR_FIELD
        )

        with Container(
            id=f"workflow-form-{workflow_name}",
            name=workflow_name,
            classes="workflow-form hidden",
        ):
            yield Container(
                Label(f"Parameters - {workflow_name}"), classes="form-header round"
            )
            with VerticalScroll(classes="form-rows"):
                if workflow_inputs:
                    yield Label("Workflow", classes="section-label")
                    yield from workflow_inputs
                if orchestrator_inputs:
                    yield Label("Orchestrator", classes="section-label")
                    yield from orchestrator_inputs
                yield Rule()
                yield Vertical(
                    Center(
                        Button.success(
                            "Start Workflow",
                            name=workflow_name,
                            classes="workflow-start",
                        )
                    ),
                    Center(Label("", classes="form-error-summary hidden")),
                    classes="form-footer centered",
                )
