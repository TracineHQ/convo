"""convo: global conversation index and query tool for Claude Code."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("convo")
except PackageNotFoundError:  # source checkout without an installed dist
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
