from textual.widgets import OptionList


class FilterableOptionList(OptionList):
    DEFAULT_PROMPT = "(Type to filter)"

    def __init__(self, *args, **kwargs):
        self.auto_select_on_filter = kwargs.pop("auto_select_on_filter", False)
        self.title = kwargs.pop("title", None)
        initial = kwargs.pop("initial", None)

        super().__init__(*args, **kwargs)

        self._all_options = args
        self.filter = ""
        self.value = initial if initial in args else None

    @property
    def title_display(self):
        result = f"{self.title} - " if self.title else ""
        result += f"({self.filter})" if self.filter else self.DEFAULT_PROMPT
        return result

    def on_mount(self):
        self.border_title = self.title_display
        if self.value is not None:
            self.highlighted = self._all_options.index(self.value)

    def filter_options(self):
        self.clear_options()
        self.border_title = self.title_display
        self.add_options(
            [opt for opt in self._all_options if self.filter in opt.lower()]
        )

        if self.option_count >= 1 and self.auto_select_on_filter:
            self.action_first()
            self.action_select()

    def on_option_list_option_selected(self, event):
        self.value = event.option.prompt

    def on_key(self, event):
        if event.name == "backspace":
            self.filter = self.filter[:-1]
        elif event.is_printable:
            self.filter += event.character.lower()
        else:
            return

        self.filter_options()
        event.stop()

    def on_focus(self):
        self.border_title = self.title_display

    def on_blur(self):
        self.border_title = self.title_display
