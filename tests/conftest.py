from pathlib import Path


def pytest_configure(config):
    # pytest creates basetemp itself but requires its parent to exist on Windows.
    (Path(config.rootpath) / "work").mkdir(exist_ok=True)
