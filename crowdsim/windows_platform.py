"""Keep Windows platform detection on CPython's local, non-WMI fallback."""
import platform
import sys


WINDOWS = sys.platform == "win32"


def _local_system_info(*args, **kwargs):
    # CPython already catches OSError here and falls back to
    # sys.getwindowsversion(), cmd /c ver and the local registry. WMI can
    # otherwise block indefinitely during imports such as aiohttp.helpers.
    raise OSError("Use local Windows platform information instead of WMI")


def configure_windows_platform():
    # Older Python releases and other systems need no compatibility change.
    # Guard this private CPython hook so an absent hook remains untouched.
    if not WINDOWS or not hasattr(platform, "_wmi_query"):
        return False
    platform._wmi_query = _local_system_info
    return True
