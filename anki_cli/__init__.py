from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

__all__ = ["__version__"]

try:
    __version__ = _dist_version("anki-cli")
except PackageNotFoundError:
    __version__ = "0.0.0"
