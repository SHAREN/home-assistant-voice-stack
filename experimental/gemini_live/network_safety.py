"""Deterministic classification of initial Gemini transport failures."""

from __future__ import annotations

import errno


_INITIAL_NETWORK_EXCEPTION_NAMES = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "ConnectionAbortedError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "InvalidProxyMessage",
        "InvalidProxyStatus",
        "ProxyError",
        "ProxyTimeout",
        "SSLError",
        "TimeoutError",
        "WebSocketProxyException",
        "gaierror",
    }
)
_INITIAL_NETWORK_ERRNOS = frozenset(
    {
        errno.ECONNABORTED,
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.EHOSTDOWN,
        errno.EHOSTUNREACH,
        errno.ENETDOWN,
        errno.ENETUNREACH,
        errno.ETIMEDOUT,
    }
)


def initial_network_error_type(exc: BaseException) -> str | None:
    """Return a transport type from an exception chain, or fail closed as unknown."""
    pending: list[BaseException] = [exc]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        class_name = current.__class__.__name__
        if class_name in _INITIAL_NETWORK_EXCEPTION_NAMES:
            return class_name
        if isinstance(current, OSError) and current.errno in _INITIAL_NETWORK_ERRNOS:
            return class_name
        nested = getattr(current, "exceptions", None)
        if nested:
            pending.extend(item for item in nested if isinstance(item, BaseException))
        for linked in (current.__cause__, current.__context__):
            if isinstance(linked, BaseException):
                pending.append(linked)
    return None
