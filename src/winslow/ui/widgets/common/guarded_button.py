from textual.reactive import reactive
from textual.widgets import Button

from winslow.ui.actions import NO_PERMISSION_MESSAGE, SESSION_ENDING_MESSAGE


class GuardedButton(Button):
    """A control a permission guards. permitted starts False, so the control
    is disabled until the hello grants it: the button asks at its mount, and
    the screen asks again for every button on a connection change (see
    WorkflowScreen.apply_permissions). The ending lock of a session comes
    second, and the state of the widget third (see _enabled)."""

    permitted = reactive(False)
    session_ending = reactive(False)

    def __init__(
        self, *args, permission, blocks_on_ending=False, scope_name=None, **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.permission = permission
        # True if this button starts a batch. Such a button locks while the
        # session drains.
        self.blocks_on_ending = blocks_on_ending
        # The scope name this button asks about. None reads the one of the
        # screen (see SlottedScreen.permission_scope).
        self.scope_name = scope_name

    def on_mount(self):
        self.refresh_permission(self.app, self.screen.permission_scope)

    def refresh_permission(self, app, default_scope=None):
        scope = self.scope_name if self.scope_name is not None else default_scope
        self.permitted = scope is not None and app.allows(scope, self.permission)

    def watch_permitted(self, permitted):
        self._refresh_disabled()

    def watch_session_ending(self, ending):
        self._refresh_disabled()

    def _enabled(self):
        """The state of the widget, once the permission and the lock allow."""
        return True

    def _refresh_disabled(self):
        if not self.permitted:
            self.disabled = True
            # A control its state disables anyway names no missing permission.
            self.tooltip = NO_PERMISSION_MESSAGE if self._enabled() else None
            return
        if self.blocks_on_ending and self.session_ending:
            self.disabled = True
            self.tooltip = SESSION_ENDING_MESSAGE
            return
        self.disabled = not self._enabled()
        self.tooltip = None
