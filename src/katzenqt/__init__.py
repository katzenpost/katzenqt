# Lazy import to avoid loading Qt at module import time
# This allows tests to import submodules like models/persistent
# without triggering Qt dependencies
def __getattr__(name):
    if name == "cli":
        from .katzen import cli
        return cli
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
