"""Convert a userdebug Meta Quest 3 back to user firmware."""
from importlib.metadata import PackageNotFoundError, version

DISTRIBUTION = "quest-user-firmware-installer"

try:
    __version__ = version(DISTRIBUTION)
except PackageNotFoundError:  # Running from a source tree without installation.
    __version__ = "0.0.0+unknown"
