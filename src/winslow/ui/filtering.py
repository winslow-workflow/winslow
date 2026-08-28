"""The shared search behavior of the row panes.

Here a "row" is any widget with a `.search_key` attribute (see TaskRowBase) and
the add_class and remove_class methods of Textual. The rule for the match and dim
CSS classes is in one module, so the task list, the history pane and the caches
pane stay the same. The style is thus in one place, and not copied at each call
site.
"""

from functools import partial

from textual.widgets import Input

from winslow.ui.reads import READ_FAILURES

FILTER_MATCH_CLASS = "filter-match"
FILTER_DIM_CLASS = "filter-dim"

SEARCH_PREVIEW_DELAY = 0.5


def clear_filter_highlight(rows):
    for row in rows:
        row.remove_class(FILTER_MATCH_CLASS, FILTER_DIM_CLASS)


def apply_filter_highlight(rows, matching):
    for row in rows:
        if row.search_key in matching:
            row.remove_class(FILTER_DIM_CLASS)
            row.add_class(FILTER_MATCH_CLASS)
        else:
            row.remove_class(FILTER_MATCH_CLASS)
            row.add_class(FILTER_DIM_CLASS)


class SearchFlowMixin:
    """The two-step search of a pane: a typed query previews the matches
    after a delay, the submit applies the filter. The host implements
    search_rows(), search_matches(query) and apply_search(query)."""

    def _init_search(self):
        self._search_timer = None

    def preview_search(self, value):
        self._stop_search_timer()
        query = value.strip()
        if not query:
            clear_filter_highlight(self.search_rows())
            return
        self._search_timer = self.set_timer(
            SEARCH_PREVIEW_DELAY, partial(self._preview_now, query)
        )

    def submit_search(self, value):
        self._stop_search_timer()
        clear_filter_highlight(self.search_rows())
        self.apply_search(value.strip())

    def _preview_now(self, query):
        # None marks an unparseable query: the preview clears instead of
        # dimming every row.
        matching = self.search_matches(query)
        if matching is None:
            clear_filter_highlight(self.search_rows())
            return
        apply_filter_highlight(self.search_rows(), matching)

    def _stop_search_timer(self):
        if self._search_timer is not None:
            self._search_timer.stop()


class QuerySearchMixin(SearchFlowMixin):
    """A pane whose search input is a filter query. The host implements
    match_keys(query), the identity keys the query matches, and names its
    input with search_input_id; the mixin owns the search contract."""

    search_input_id = None

    def _init_search(self):
        super()._init_search()
        # The keys the active filter matches; None shows every row.
        self._filter_matching = None

    def match_keys(self, query):
        raise NotImplementedError

    def _validate_search_input(self, query):
        if self.search_input_id is not None:
            self.query_one(f"#{self.search_input_id}", Input).validate(query)

    def search_matches(self, query):
        # None marks a query with no answer, unparseable or the wire down:
        # the preview clears instead of dimming every row.
        self._validate_search_input(query)
        try:
            return self.match_keys(query)
        except READ_FAILURES:
            return None

    def apply_search(self, query):
        if not query:
            self._filter_matching = None
        else:
            try:
                matching = self.match_keys(query)
            except READ_FAILURES as exc:
                self.notify(str(exc), severity="warning")
                return
            self._filter_matching = matching
        self._apply_visibility()
