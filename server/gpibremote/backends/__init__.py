"""GPIB backends: the real bus through linux-gpib, or a simulator for development."""

from .base import Backend, GpibError, GpibTimeout, timeout_code


def open_backend(kind, board=0, **options):
    """Create a backend by name: 'linuxgpib' or 'sim'."""
    if kind == "linuxgpib":
        from .linuxgpib import LinuxGpibBackend
        return LinuxGpibBackend(board, **options)
    if kind == "sim":
        from .sim import SimBackend
        return SimBackend(board, **options)
    raise ValueError("unknown backend %r" % kind)


__all__ = ["Backend", "GpibError", "GpibTimeout", "open_backend", "timeout_code"]
