"""Desktop transport selection. Native ownership is separate from live attestation."""
import sys
from pathlib import Path

if sys.platform == "win32":
    from .transport_named_pipe import NamedPipeRPCClient as LocalRPCClient
    from .transport_named_pipe import NamedPipeRPCServer as LocalRPCServer
    from .transport_named_pipe import default_pipe_path

    def default_socket_path() -> Path:
        return Path(default_pipe_path())
else:
    from .transport_unix import UnixRPCClient as LocalRPCClient
    from .transport_unix import UnixRPCServer as LocalRPCServer
    from .transport_unix import default_socket_path

__all__ = ["LocalRPCClient", "LocalRPCServer", "default_socket_path"]
