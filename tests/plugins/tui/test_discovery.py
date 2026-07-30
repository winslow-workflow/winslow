"""UI plugins register through the shared BaseRegistry the same way filters do,
but via both discovery paths - discover(package) (source="builtin") and
discover_installed() (entry points). Folds ordering and candidacy into the real
discovery path: for_slot returns registered plugin classes sorted (priority,
name)."""

from winslow.ui.plugin import Slots


def _names(plugins):
    return [p.get_name() for p in plugins]


def test_builtin_discovery_registers_and_orders(make_registry, plugins_package):
    reg = make_registry()
    reg.discover(plugins_package)

    # priority 3 (beta) sorts before priority 5 (alpha)
    assert _names(reg.for_slot(Slots.TASK_OVERVIEW)) == ["beta", "alpha"]
    # two plugins in the slot -> the slot renders tabbed
    assert reg.any_tabbed(Slots.TASK_OVERVIEW) is True


def test_installed_discovery_registers(make_registry, install_entrypoint):
    install_entrypoint(dist="acme-plugins")
    reg = make_registry()
    reg.discover_installed()

    assert _names(reg.for_slot(Slots.TASK_OVERVIEW)) == ["beta", "alpha"]


def test_no_slot_plugin_is_not_a_candidate(make_registry, plugins_package):
    reg = make_registry()
    reg.discover(plugins_package)

    # NoSlotPlugin has slot=None, so _is_candidate drops it. Candidacy has no
    # public read (for_slot needs a slot), so this checks the registration list.
    registered = _names(reg._plugins)
    assert "noslot" not in registered
    # OptInPlugin (autoload=False) is likewise absent by default.
    assert "optin" not in registered
    assert set(registered) == {"alpha", "beta"}
