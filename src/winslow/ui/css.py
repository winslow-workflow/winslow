from importlib.resources import files


def package_css(package, *names):
    """Join the .tcss files of the package for the DEFAULT_CSS of a widget."""
    return "".join(files(package).joinpath(name).read_text() for name in names)
