"""The shared rule that previews the filter matches in a list of rows.

Here a "row" is any widget with a `.w_task` attribute and the add_class and
remove_class methods of Textual. The rule for the match and dim CSS classes is in
one module, so the task list of the workflow and the history pane stay the same.
The style is thus in one place, and not copied at each call site.
"""

FILTER_MATCH_CLASS = "filter-match"
FILTER_DIM_CLASS = "filter-dim"


def clear_filter_highlight(rows):
    for row in rows:
        row.remove_class(FILTER_MATCH_CLASS, FILTER_DIM_CLASS)


def apply_filter_highlight(rows, matching):
    for row in rows:
        if row.w_task in matching:
            row.remove_class(FILTER_DIM_CLASS)
            row.add_class(FILTER_MATCH_CLASS)
        else:
            row.remove_class(FILTER_MATCH_CLASS)
            row.add_class(FILTER_DIM_CLASS)
