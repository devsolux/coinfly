"""Launcher errors importable before loading the desktop or database modules."""


class DesktopDependencyError(RuntimeError):
    pass
