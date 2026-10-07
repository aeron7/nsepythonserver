from .rahu import *

# __version__ used to be hardcoded as "0.1" -- stale since before this
# project's versioning scheme existed. pip/PyPI always reported the real
# installed version correctly; only `import nsepythonserver;
# nsepythonserver.__version__` was wrong. Read the real installed version
# dynamically instead, falling back to a literal if the package isn't
# pip-installed (e.g. running straight from a git checkout with no installed
# dist) -- this must never raise and break importing the whole library.
try:
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("nsepythonserver")
except Exception:
    __version__ = "2.101"
