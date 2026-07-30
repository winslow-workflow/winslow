"""A custom filter reaches a headless run purely by being discovered: the fake
entry point is the only registration path (FilterRegistry never calls the
builtin discover()), so a pass here exercises discover_installed -> the guard
chain -> _do_register -> resolve -> parse -> the filtered run, end to end."""


def test_discovered_filter_usable_by_long_command(
    install_filters, build_filtered, assert_only_ran
):
    install_filters()
    workflow = build_filtered("--filter", "!flavor sweet")
    workflow.headless_run()
    assert_only_ran(workflow, ran={"Sweet"})


def test_discovered_filter_usable_by_short_command(
    install_filters, build_filtered, assert_only_ran
):
    install_filters()
    workflow = build_filtered("--filter", "!f salty")
    workflow.headless_run()
    assert_only_ran(workflow, ran={"Salty"})
