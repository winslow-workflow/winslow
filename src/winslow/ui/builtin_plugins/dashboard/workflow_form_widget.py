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

from decimal import Decimal

from textual.containers import VerticalScroll, Horizontal

from winslow.ui.widgets.common import FilterableOptionList
from winslow.ui.validation import ORCHESTRATOR_FIELD, WORKFLOW_FIELD


class FormLabel(Label):
    pass


class FormRow(Horizontal):
    pass


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

    The fields come from the ConfigOption declarations on the orchestrator and on
    the workflow. The form shows the two groups of options in different sections.
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
        return Label(help_text, classes="help-text")

    def _choose_generation_func(self, option):
        if option.action in ("store_true", "store_false", "store_const"):
            return self._generate_switch
        elif option.choices:
            if option.multiselect:
                return self._generate_selection_list
            elif len(option.choices) > self.MAX_OPTIONS_FOR_RADIO_SELECT:
                return self._generate_option_list
            return self._generate_radio_select
        return self._generate_text_input

    def _generate_switch(self, name, option, initial):
        return Switch(value=initial, name=name)

    def _generate_selection_list(self, name, option, initial):
        # TODO: add a search, as in FilterableOptionList.
        selection_ctx = [
            (str(selection_value), selection_value, selection_value in (initial or ()))
            for selection_value in option.choices
        ]
        return SelectionList(*selection_ctx, name=name)

    def _generate_option_list(self, name, option, initial):
        # The same string round-trip as the Input path and the radio path. The
        # widget shows strings, and the validator converts them back with
        # config_option.type.
        choices = [str(val) for val in option.choices]
        initial = option.format_value(initial)
        return FilterableOptionList(*choices, name=name, initial=initial)

    def _generate_radio_select(self, name, option, initial):
        radio_ctx = [
            RadioButton(str(val), value=val == initial, name=str(val))
            for val in option.choices
        ]
        return RadioSet(*radio_ctx, name=name, classes="form-field")

    def _generate_text_input(self, name, option, initial):

        def _get_type():
            if option.type in (float, Decimal):
                return "number"
            elif option.type is int:
                return "integer"
            return "text"

        return Input(type=_get_type(), name=name, value=option.format_value(initial))

    def generate_inputs_for_meta(self, config, meta, field_class):

        result = []

        for name, option in meta.items():
            if option.show_on_ui:
                field_func = self._choose_generation_func(option)

                fallback_value = tuple() if option.choices else None

                form_field = field_func(
                    name, option, initial=getattr(config, name, fallback_value)
                )
                form_field.add_class("form-field")
                form_field.add_class(field_class)

                row = FormRow(
                    self._generate_label(name),
                    Vertical(
                        form_field,
                        self._generate_help_text(option.help_text),
                        classes="form-input",
                    ),
                )
                result.append(Rule())
                result.append(row)

        return result

    def generate(
        self,
        workflow_kls,
        workflow_config_meta,
        workflow_config,
        orchestrator_config_meta,
        orchestrator_config,
    ):

        self.logger.debug(
            f"Generating form for {workflow_kls}, "
            f"orchestrator_config: {orchestrator_config}, workflow_config: {workflow_config}"
        )

        workflow_inputs = self.generate_inputs_for_meta(
            config=workflow_config,
            meta=workflow_config_meta,
            field_class=WORKFLOW_FIELD,
        )

        orchestrator_inputs = self.generate_inputs_for_meta(
            config=orchestrator_config,
            meta=orchestrator_config_meta,
            field_class=ORCHESTRATOR_FIELD,
        )

        workflow_name = workflow_kls.get_name()

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
