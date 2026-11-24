"""IOSignal client for Python.

The byte layer (``mbp``, ``protocol``, ``boho``) also runs on MicroPython; the asyncio client and the
blocking wrapper are CPython only.
"""

from . import boho, mbp, protocol
from .protocol import IOMsg, PayloadType, Signal, State

try:
    from .client import AuthError, IOSignal, PingError, RpcError, RpcResponse
except (ImportError, SyntaxError):  # MicroPython: the CPython client does not compile or import there
    pass

__all__ = [
    "boho", "mbp", "protocol", "IOSignal", "AuthError", "PingError", "RpcError", "RpcResponse",
    "IOMsg", "PayloadType", "Signal", "State",
]
__version__ = "0.2.1"
